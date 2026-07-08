"""
数据采集模块 - 使用 akshare 获取 A 股每日行情数据
改进版：Session 保持连接 + 自动重试 + 备用接口
独立运行: python3 data_fetcher.py（打印采集结果）
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any

# --- requests session 保持连接，减少 Connection aborted ---
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = None

def _get_session() -> requests.Session:
    """创建带重试策略的 requests session"""
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
    return _session

# --- akshare 导入 ---
try:
    import akshare as ak
except ImportError:
    print("❌ 请先安装 akshare: pip3 install akshare")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("❌ 请先安装 pandas: pip3 install pandas")
    sys.exit(1)


def _safe_float(val) -> float:
    """安全转换为 float，处理 '-' 等特殊值"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def safe_call(func, **kwargs) -> Any:
    """安全调用 akshare 函数，自动重试 2 次"""
    last_error = None
    for attempt in range(3):
        try:
            return func(**kwargs)
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(1.5 ** attempt)
    print(f"⚠️  akshare.{func.__name__} 调用失败: {last_error}")
    return None


def _fetch_index_http(target_indices: dict) -> dict:
    """HTTP 备用方案：从东方财富获取指数行情"""
    result = {}
    # 构建代码→名称的反向映射，用于按实际返回的代码准确匹配
    code_to_name = {code: name for name, code in target_indices.items()}
    try:
        codes = ",".join([f"1.{code}" for code in target_indices.values()])
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {"fltt": "2", "invt": "2", "fields": "f2,f3,f4,f5,f6,f12", "secids": codes}
        resp = _get_session().get(url, params=params, timeout=15)
        if resp.status_code == 200:
            items = resp.json().get("data", {}).get("diff", [])
            for item in items:
                code = str(item.get("f12", ""))
                name = code_to_name.get(code)
                if name:
                    result[name] = {
                        "代码": code,
                        "最新价": _safe_float(item.get("f2", 0)),
                        "涨跌幅": _safe_float(item.get("f3", 0)),
                        "涨跌额": _safe_float(item.get("f4", 0)),
                        "成交额": _safe_float(item.get("f6", 0)),
                    }
            if result:
                print(f"   ✅ 指数数据(HTTP): {len(result)} 项")
    except Exception as e:
        print(f"   ⚠️  HTTP 指数备用方案失败: {e}")
    return result


def _fetch_index_tencent(target_indices: dict) -> dict:
    """腾讯行情 API 备用方案（24小时可用）"""
    result = {}
    # 指数代码 → 腾讯前缀映射
    code_to_tencent = {}
    for name, code in target_indices.items():
        if code.startswith("3"):
            code_to_tencent[name] = f"sz{code}"
        elif code.startswith("8"):
            code_to_tencent[name] = f"bj{code}"
        else:
            code_to_tencent[name] = f"sh{code}"

    codes_str = ",".join(code_to_tencent.values())
    try:
        resp = _get_session().get(f"http://qt.gtimg.cn/q={codes_str}", timeout=15)
        if resp.status_code != 200:
            return result
        # 腾讯格式: v_sh000001="1~名称~代码~最新价~昨收~开盘~...~涨跌额~涨跌幅~最高~最低~..."
        for name, tc_code in code_to_tencent.items():
            key = f'v_{tc_code}='
            if key not in resp.text:
                continue
            start = resp.text.index(key) + len(key) + 1  # 跳过开头的引号
            end = resp.text.index('"', start)
            fields = resp.text[start:end].split("~")
            if len(fields) >= 36:
                # 腾讯指数字段: 3=最新价, 31=涨跌额, 32=涨跌幅
                # fields[35] 格式: "最新价/成交量(手)/成交额(元)"
                price = _safe_float(fields[3])
                chg_amt = _safe_float(fields[31])
                chg_pct = _safe_float(fields[32])
                amount = 0.0
                parts = fields[35].split("/")
                if len(parts) >= 3:
                    amount = _safe_float(parts[2])
                result[name] = {
                    "代码": target_indices[name],
                    "最新价": price,
                    "涨跌幅": chg_pct,
                    "涨跌额": chg_amt,
                    "成交额": amount,
                }
        if result:
            print(f"   ✅ 指数数据(腾讯): {len(result)} 项")
    except Exception as e:
        print(f"   ⚠️  腾讯指数备用方案失败: {e}")
    return result


def _try_fetch_index_akshare(target_indices: dict, result: dict) -> int:
    """方案 A：akshare 实时行情接口，返回获取到的数量"""
    df = safe_call(ak.stock_zh_index_spot_em)
    if df is None or df.empty:
        return 0
    added = 0
    for name, code in target_indices.items():
        if name in result:
            continue
        row = df[df["代码"] == code]
        if not row.empty:
            result[name] = _parse_index_row(row)
            added += 1
    return added


def _try_fetch_index_history(target_indices: dict, result: dict) -> int:
    """方案 D：日线历史（逐个指数抓取），返回获取到的数量"""
    added = 0
    for name, code in target_indices.items():
        if name in result:
            continue
        prefix = "sh" if code.startswith("6") or code.startswith("0") else "sz"
        try:
            hist = safe_call(ak.stock_zh_index_daily_em, symbol=f"{prefix}{code}")
            if hist is not None and not hist.empty:
                row = hist.iloc[-1]
                result[name] = {
                    "代码": code,
                    "最新价": float(row["close"]),
                    "涨跌幅": float(row.get("pct_chg", 0)),
                    "涨跌额": float(row.get("change", 0)),
                    "成交额": float(row.get("amount", 0)),
                }
                added += 1
        except Exception:
            pass
    return added


def fetch_index_data() -> dict:
    """采集主要指数行情 — 多源互补，确保 9 项指数全部覆盖"""
    target_indices = {
        "上证指数": "000001", "深证成指": "399001", "创业板指": "399006",
        "科创50": "000688", "北证50": "899050", "沪深300": "000300",
        "上证50": "000016", "中证500": "000905", "中证1000": "000852",
    }
    total = len(target_indices)
    result = {}

    # 方案 A：akshare 实时行情接口
    added = _try_fetch_index_akshare(target_indices, result)
    if added:
        print(f"   ✅ 指数(akshare): +{added}, 已覆盖 {len(result)}/{total}")
    if len(result) == total:
        return result

    # 方案 B：HTTP 东方财富 ulist 接口
    http_result = _fetch_index_http(target_indices)
    if http_result:
        for name, data in http_result.items():
            if name not in result:
                result[name] = data
        print(f"   ✅ 指数(HTTP): 已覆盖 {len(result)}/{total}")
    if len(result) == total:
        return result

    # 方案 C：腾讯行情 API
    tencent_result = _fetch_index_tencent(target_indices)
    if tencent_result:
        for name, data in tencent_result.items():
            if name not in result:
                result[name] = data
        print(f"   ✅ 指数(腾讯): 已覆盖 {len(result)}/{total}")
    if len(result) == total:
        return result

    # 方案 D：日线历史（逐个指数请求，最后手段）
    added = _try_fetch_index_history(target_indices, result)
    if added:
        print(f"   ⚠️  指数(历史): +{added}, 已覆盖 {len(result)}/{total}")

    if len(result) < total:
        missing = [n for n in target_indices if n not in result]
        print(f"   ⚠️  指数缺失 {len(missing)} 项: {', '.join(missing)}")
    return result


def _parse_index_row(row) -> dict:
    return {
        "代码": str(row["代码"].values[0]),
        "最新价": float(row["最新价"].values[0]),
        "涨跌幅": float(row["涨跌幅"].values[0]),
        "涨跌额": float(row["涨跌额"].values[0]),
        "成交量": int(row["成交量"].values[0]) if "成交量" in row.columns else 0,
        "成交额": float(row["成交额"].values[0]) if "成交额" in row.columns else 0,
    }


def _fetch_breadth_from_sina() -> dict:
    """从新浪财经 API 分页获取全市场涨跌统计（24小时可用，不依赖东方财富 clist/get）"""
    result = {}
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple"
    all_items = []

    for page in range(1, 13):  # 最多12页 × 500 = 6000条，覆盖全A股
        try:
            resp = _get_session().get(url, params={
                "page": str(page), "num": "500",
                "sort": "symbol", "asc": "1", "node": "hs_a",
            }, timeout=30)
            if resp.status_code != 200:
                break
            items = json.loads(resp.text)
            if not isinstance(items, list) or not items:
                break
            all_items.extend(items)
            if len(items) < 500:
                break  # 最后一页
        except Exception as e:
            print(f"   ⚠️  新浪API第{page}页失败: {e}")
            break

    if all_items:
        total = len(all_items)
        up = sum(1 for i in all_items if _safe_float(i.get("changepercent", 0)) > 0)
        down = sum(1 for i in all_items if _safe_float(i.get("changepercent", 0)) < 0)
        amount = sum(_safe_float(i.get("amount", 0)) for i in all_items)
        result["上涨家数"] = up
        result["下跌家数"] = down
        result["平盘家数"] = total - up - down
        result["总家数"] = total
        result["红盘率"] = round(up / total * 100, 2) if total > 0 else 0
        result["总成交额"] = round(amount / 1e8, 2)  # 转亿元
        print(f"   ✅ 市场宽度(新浪API): {total}只, 红盘率{result['红盘率']}%, 成交额{result['总成交额']}亿")
    return result


def fetch_market_breadth() -> dict:
    """采集全市场涨跌家数、成交额（多级降级方案）"""
    result = {"上涨家数": 0, "下跌家数": 0, "平盘家数": 0, "总家数": 0, "红盘率": 0.0, "总成交额": 0}

    # 方案 A：akshare 实时接口（仅交易时段可用）
    df = safe_call(ak.stock_zh_a_spot_em)
    if df is not None and not df.empty:
        total = len(df)
        up_count = len(df[df["涨跌幅"] > 0])
        down_count = len(df[df["涨跌幅"] < 0])
        result["上涨家数"] = up_count
        result["下跌家数"] = down_count
        result["平盘家数"] = total - up_count - down_count
        result["总家数"] = total
        result["红盘率"] = round(up_count / total * 100, 2) if total > 0 else 0
        if "成交额" in df.columns:
            result["总成交额"] = round(df["成交额"].sum() / 1e8, 2)
        print(f"   ✅ 市场宽度(akshare): {total}只, 红盘率{result['红盘率']}%, 成交额{result['总成交额']}亿")
        return result

    # 方案 B：新浪财经 API 分页抓取（24小时可用）
    print("   ⚠️  akshare 全市场接口失败，尝试新浪财经 API...")
    result = _fetch_breadth_from_sina()
    if result and result.get("总家数", 0) > 0:
        return result

    print("   ⚠️  全市场行情获取失败（所有方案）")
    return result


def fetch_limit_up_data() -> dict:
    """采集涨停板相关数据（收盘后数据可用）

    修正字段映射：封板时间→首次封板时间/最后封板时间，封单金额→封板资金
    保留跌停池/强势股池个股明细，新增板块涨停家数统计
    """
    result = {
        "涨停家数": 0, "跌停家数": 0,
        "涨停股列表": [], "跌停股列表": [], "强势股列表": [],
        "炸板率": 0.0, "连板家数": 0, "最高连板高度": 0,
        "板块涨停统计": {},  # {板块名: 涨停家数}
    }
    date_str = datetime.now().strftime("%Y%m%d")

    # ---- 涨停池 ----
    zt_df = safe_call(ak.stock_zt_pool_em, date=date_str)
    if zt_df is not None and not zt_df.empty:
        zt_df = zt_df[~zt_df["名称"].str.contains("ST", na=False)]
        result["涨停家数"] = len(zt_df)
        # 获取列名（兼容不同版本）
        cols = set(zt_df.columns)
        for _, row in zt_df.iterrows():
            stock = {
                "代码": str(row.get("代码", "")),
                "名称": str(row.get("名称", "")),
                "涨跌幅": round(float(row.get("涨跌幅", 0)), 2),
                "最新价": float(row.get("最新价", 0)),
                "成交额": round(float(row.get("成交额", 0)) / 1e8, 2) if "成交额" in row else 0,
                "流通市值": round(float(row.get("流通市值", 0)) / 1e8, 2) if "流通市值" in row else 0,
                "总市值": round(float(row.get("总市值", 0)) / 1e8, 2) if "总市值" in row else 0,
                "换手率": round(float(row.get("换手率", 0)), 2) if "换手率" in row else 0,
                "封板资金": round(float(row.get("封板资金", 0)) / 1e8, 4) if "封板资金" in row else 0,
                "首次封板时间": str(row.get("首次封板时间", "")),
                "最后封板时间": str(row.get("最后封板时间", "")),
                "炸板次数": int(row.get("炸板次数", 0)) if "炸板次数" in row else 0,
                "涨停统计": str(row.get("涨停统计", "")),
                "连板数": int(row.get("连板数", 1)) if "连板数" in row else 1,
                "所属行业": str(row.get("所属行业", "")),
            }
            result["涨停股列表"].append(stock)
            # 板块涨停统计
            sector = stock["所属行业"]
            if sector:
                result["板块涨停统计"][sector] = result["板块涨停统计"].get(sector, 0) + 1

        if "连板数" in cols:
            result["连板家数"] = int(len(zt_df[zt_df["连板数"] >= 2]))
            result["最高连板高度"] = int(zt_df["连板数"].max())
        # 首板封板率（需要知道尝试封板的首板总数，这里用涨停股中的首板数）
        first_boards = len(zt_df[zt_df.get("连板数", 1) == 1]) if "连板数" in cols else 0
        result["首板数"] = first_boards
        print(f"   ✅ 涨停池: {result['涨停家数']}家, 连板{result['连板家数']}家, 首板{first_boards}家")

    # ---- 跌停池（保留个股明细）----
    dt_df = safe_call(ak.stock_zt_pool_dtgc_em, date=date_str)
    if dt_df is not None and not dt_df.empty:
        # 过滤 ST/退市股
        dt_df = dt_df[~dt_df["名称"].str.contains("ST|退", na=False)]
        result["跌停家数"] = len(dt_df)
        for _, row in dt_df.iterrows():
            result["跌停股列表"].append({
                "代码": str(row.get("代码", "")),
                "名称": str(row.get("名称", "")),
                "涨跌幅": round(float(row.get("涨跌幅", 0)), 2),
                "最新价": float(row.get("最新价", 0)),
                "成交额": round(float(row.get("成交额", 0)) / 1e8, 2) if "成交额" in row else 0,
                "换手率": round(float(row.get("换手率", 0)), 2) if "换手率" in row else 0,
                "连续跌停": int(row.get("连续跌停", 1)) if "连续跌停" in row else 1,
                "开板次数": int(row.get("开板次数", 0)) if "开板次数" in row else 0,
                "流通市值": round(float(row.get("流通市值", 0)) / 1e8, 2) if "流通市值" in row else 0,
                "所属行业": str(row.get("所属行业", "")),
            })
        print(f"   ✅ 跌停池: {result['跌停家数']}家（已含明细）")

    # ---- 炸板/强势股池 ----
    strong_df = safe_call(ak.stock_zt_pool_strong_em, date=date_str)
    if strong_df is not None and not strong_df.empty:
        strong_df = strong_df[~strong_df["名称"].str.contains("ST", na=False)]
        for _, row in strong_df.iterrows():
            result["强势股列表"].append({
                "代码": str(row.get("代码", "")),
                "名称": str(row.get("名称", "")),
                "涨跌幅": round(float(row.get("涨跌幅", 0)), 2),
                "成交额": round(float(row.get("成交额", 0)) / 1e8, 2) if "成交额" in row else 0,
                "换手率": round(float(row.get("换手率", 0)), 2) if "换手率" in row else 0,
                "量比": round(float(row.get("量比", 0)), 2) if "量比" in row else 0,
                "涨停统计": str(row.get("涨停统计", "")),
                "入选理由": str(row.get("入选理由", "")),
                "所属行业": str(row.get("所属行业", "")),
            })
        # 炸板率估算（强势股中未出现在涨停池的为炸板股）
        zt_codes = {s["代码"] for s in result["涨停股列表"]}
        zha_ban = [s for s in result["强势股列表"] if s["代码"] not in zt_codes]
        result["炸板股列表"] = zha_ban
        total_attempts = result["涨停家数"] + len(zha_ban)
        result["炸板率"] = round(len(zha_ban) / total_attempts * 100, 2) if total_attempts > 0 else 0
        print(f"   ✅ 强势股池: {len(result['强势股列表'])}家, 炸板{len(zha_ban)}家, 炸板率{result['炸板率']}%")
    else:
        print("   ⚠️  强势股池获取失败")

    return result


def _fetch_north_south_http() -> float:
    """东方财富 HTTP 接口获取北向资金净流入（akshare 备用）"""
    try:
        # 沪股通+深股通 北向资金日线
        url = "https://push2.eastmoney.com/api/qt/kamt.kline/get"
        params = {
            "fields1": "f1,f3",
            "fields2": "f2,f4,f5,f6,f8",
            "klt": "101",  # 日线
            "lmt": "1",    # 只取最新一天
        }
        total = 0.0
        for code in ["1", "3"]:  # 1=沪股通, 3=深股通
            params["secid"] = f"1.{code}"
            resp = _get_session().get(url, params=params, timeout=15)
            if resp.status_code == 200:
                klines = resp.json().get("data", {}).get("klines", [])
                if klines:
                    # 格式: 日期,净流入(万),...
                    parts = klines[-1].split(",")
                    if len(parts) >= 2:
                        total += _safe_float(parts[1]) / 10000  # 万→亿
        if total != 0:
            print(f"      ✅ 北向资金(HTTP): {total:.1f}亿")
        return total
    except Exception:
        return 0.0


def fetch_fund_flow() -> dict:
    """采集资金流向（akshare 优先，HTTP 备用）"""
    result = {"北向资金净流入": 0.0, "南向资金净流入": 0.0, "主力资金净流入": 0.0, "板块主力流入TOP3": [], "板块主力流出TOP3": []}

    # --- 北向/南向资金 ---
    for direction, key in [("北向资金", "北向资金净流入"), ("南向资金", "南向资金净流入")]:
        try:
            df = safe_call(ak.stock_hsgt_hist_em, symbol=direction)
            if df is not None and not df.empty:
                result[key] = float(df.iloc[-1]["净流入"])
        except Exception:
            pass

    # 北向资金 HTTP 备用方案（akshare 失败时启用）
    if result["北向资金净流入"] == 0.0:
        result["北向资金净流入"] = _fetch_north_south_http()

    # --- 全市场主力资金 ---
    try:
        df = safe_call(ak.stock_market_fund_flow)
        if df is not None and not df.empty:
            result["主力资金净流入"] = float(df.iloc[-1].get("主力净流入", 0))
    except Exception:
        pass

    # --- 板块资金排名 ---
    try:
        df = safe_call(ak.stock_sector_fund_flow_rank, indicator="今日", sector_type="行业资金流")
        if df is not None and not df.empty:
            sorted_df = df.sort_values("主力净流入", ascending=False)
            for _, row in sorted_df.head(3).iterrows():
                result["板块主力流入TOP3"].append({"板块": row.get("名称", ""), "净流入": float(row.get("主力净流入", 0))})
            for _, row in sorted_df.tail(3).iterrows():
                result["板块主力流出TOP3"].append({"板块": row.get("名称", ""), "净流出": float(row.get("主力净流入", 0))})
    except Exception:
        pass

    # 板块资金排名备用：从板块排名数据中提取（stock_board_industry_summary_ths 已有净流入字段）
    if not result["板块主力流入TOP3"]:
        try:
            df = safe_call(ak.stock_board_industry_summary_ths)
            if df is not None and not df.empty and "净流入" in df.columns:
                sorted_df = df.sort_values("净流入", ascending=False)
                name_col = "板块" if "板块" in df.columns else "板块名称"
                for _, row in sorted_df.head(3).iterrows():
                    result["板块主力流入TOP3"].append({
                        "板块": str(row.get(name_col, "")),
                        "净流入": float(row.get("净流入", 0)),
                    })
                for _, row in sorted_df.tail(3).iterrows():
                    result["板块主力流出TOP3"].append({
                        "板块": str(row.get(name_col, "")),
                        "净流出": float(row.get("净流入", 0)),
                    })
        except Exception:
            pass

    print(f"   ✅ 资金流向: 北向{result['北向资金净流入']:.1f}亿, 主力{result['主力资金净流入']:.1f}亿")
    return result


def fetch_sector_ranking() -> dict:
    """采集行业板块排名"""
    result = {"领涨板块": [], "领跌板块": [], "板块列表": []}
    try:
        df = safe_call(ak.stock_board_industry_summary_ths)
        if df is not None and not df.empty:
            # akshare 返回的列名是"板块"而非"板块名称"
            name_col = "板块" if "板块" in df.columns else "板块名称"
            sorted_df = df.sort_values("涨跌幅", ascending=False)
            for _, row in sorted_df.head(5).iterrows():
                result["领涨板块"].append({
                    "名称": str(row.get(name_col, "")),
                    "涨跌幅": float(row.get("涨跌幅", 0)),
                    "净流入": float(row.get("净流入", 0)) if "净流入" in row else 0,
                })
            for _, row in sorted_df.tail(5).iterrows():
                result["领跌板块"].append({
                    "名称": str(row.get(name_col, "")),
                    "涨跌幅": float(row.get("涨跌幅", 0)),
                    "净流入": float(row.get("净流入", 0)) if "净流入" in row else 0,
                })
            for _, row in sorted_df.iterrows():
                result["板块列表"].append({
                    "名称": str(row.get(name_col, "")),
                    "涨跌幅": float(row.get("涨跌幅", 0)),
                    "净流入": float(row.get("净流入", 0)) if "净流入" in row else 0,
                    "上涨家数": int(row.get("上涨家数", 0)) if "上涨家数" in row else 0,
                    "下跌家数": int(row.get("下跌家数", 0)) if "下跌家数" in row else 0,
                    "领涨股": str(row.get("领涨股", "")) if "领涨股" in row else "",
                })
    except Exception:
        pass
    return result


def fetch_dragon_tiger() -> dict:
    """采集龙虎榜"""
    result = {"龙虎榜个股": []}
    date_str = datetime.now().strftime("%Y%m%d")
    try:
        lhb_df = safe_call(ak.stock_lhb_detail_em, start_date=date_str, end_date=date_str)
        if lhb_df is not None and not lhb_df.empty:
            for code in lhb_df["代码"].unique()[:30]:
                stock_data = lhb_df[lhb_df["代码"] == code]
                buy = stock_data[stock_data["买卖方向"] == "买入"]["净买入额"].sum() if "净买入额" in stock_data.columns else 0
                sell = stock_data[stock_data["买卖方向"] == "卖出"]["净卖出额"].sum() if "净卖出额" in stock_data.columns else 0
                result["龙虎榜个股"].append({"代码": code, "名称": stock_data.iloc[0].get("名称", ""), "净买入": float(buy - sell), "上榜原因": stock_data.iloc[0].get("上榜原因", "")})
    except Exception:
        pass
    return result


def fetch_consecutive_board_stocks() -> list:
    """采集连板股详细列表（2板及以上）

    修正字段映射：封板时间→首次封板时间，封单金额→封板资金，
    新增所属行业/炸板次数/涨停统计/市值等字段
    """
    stocks = []
    date_str = datetime.now().strftime("%Y%m%d")
    try:
        zt_df = safe_call(ak.stock_zt_pool_em, date=date_str)
        if zt_df is not None and not zt_df.empty:
            zt_df = zt_df[~zt_df["名称"].str.contains("ST", na=False)]
            multi = zt_df[zt_df.get("连板数", 1) >= 2]
            for _, row in multi.iterrows():
                stocks.append({
                    "代码": str(row.get("代码", "")),
                    "名称": str(row.get("名称", "")),
                    "连板数": int(row.get("连板数", 2)),
                    "首次封板时间": str(row.get("首次封板时间", "")),
                    "最后封板时间": str(row.get("最后封板时间", "")),
                    "换手率": round(float(row.get("换手率", 0)), 2) if "换手率" in row else 0,
                    "封板资金": round(float(row.get("封板资金", 0)) / 1e8, 4) if "封板资金" in row else 0,
                    "炸板次数": int(row.get("炸板次数", 0)) if "炸板次数" in row else 0,
                    "涨停统计": str(row.get("涨停统计", "")),
                    "所属行业": str(row.get("所属行业", "")),
                    "成交额": round(float(row.get("成交额", 0)) / 1e8, 2) if "成交额" in row else 0,
                    "流通市值": round(float(row.get("流通市值", 0)) / 1e8, 2) if "流通市值" in row else 0,
                    "总市值": round(float(row.get("总市值", 0)) / 1e8, 2) if "总市值" in row else 0,
                })
    except Exception:
        pass
    return stocks


def _get_board_type(code: str) -> str:
    """从股票代码推断板块涨跌幅限制类型"""
    code = str(code).zfill(6)
    if code.startswith(("300", "301")):
        return "创业板(20cm)"
    elif code.startswith(("688", "689")):
        return "科创板(20cm)"
    elif code.startswith(("8", "4")):
        return "北交所(30cm)"
    else:
        return "主板(10cm)"


def fetch_premium_gene(symbols: list) -> dict:
    """为指定股票列表采集溢价基因数据（近200个交易日）

    对每只股票获取历史日线，计算：
    - 近200日涨停次数
    - 涨停次日溢价≥5%次数
    - 涨停次日红盘率
    - 连板率

    Returns: {代码: {溢价基因字典}}
    """
    result = {}
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=300)).strftime("%Y%m%d")

    for stock in symbols:
        code = str(stock.get("代码", ""))
        name = str(stock.get("名称", ""))
        if not code:
            continue
        try:
            # 判断涨停阈值
            board_type = _get_board_type(code)
            if "30cm" in board_type:
                limit_pct = 29.5
            elif "20cm" in board_type:
                limit_pct = 19.5
            else:
                limit_pct = 9.5

            hist = safe_call(ak.stock_zh_a_hist, symbol=code, period="daily",
                             start_date=start_date, end_date=end_date, adjust="qfq")
            if hist is None or hist.empty:
                continue

            hist = hist.sort_values("日期").reset_index(drop=True)
            # 标记涨停日（涨跌幅 >= 涨停阈值）
            is_limit = hist["涨跌幅"] >= limit_pct
            limit_count = int(is_limit.sum())

            if limit_count == 0:
                result[code] = {
                    "名称": name,
                    "近200日涨停次数": 0, "溢价5%次数": 0,
                    "次日红盘率": 0, "连板率": 0,
                }
                continue

            # 涨停次日溢价统计
            premium_5 = 0   # 次日开盘溢价≥5%
            red_next = 0    # 次日收红
            consecutive = 0  # 连续涨停（连板）
            limit_indices = hist.index[is_limit].tolist()

            for idx in limit_indices:
                if idx + 1 < len(hist):
                    next_row = hist.iloc[idx + 1]
                    if next_row["涨跌幅"] > 0:
                        red_next += 1
                    open_chg = (next_row["开盘"] - next_row["昨收"]) / next_row["昨收"] * 100
                    if open_chg >= 5:
                        premium_5 += 1
                # 连板判定：前一天也是涨停
                if idx > 0 and is_limit.iloc[idx - 1]:
                    consecutive += 1

            result[code] = {
                "名称": name,
                "近200日涨停次数": limit_count,
                "溢价5%次数": premium_5,
                "次日红盘率": round(red_next / limit_count * 100, 1),
                "连板率": round(consecutive / limit_count * 100, 1),
            }
            time.sleep(0.3)  # 避免请求过快触发限流
        except Exception as e:
            print(f"   ⚠️  溢价基因采集失败 {name}({code}): {e}")
            continue

    if result:
        print(f"   ✅ 溢价基因: {len(result)} 只")
    return result


def fetch_reduction_risk() -> list:
    """采集当日减持/风险公告个股

    通过东方财富股市日历-公司动态接口获取当日公告，
    筛选出含"减持"关键词的个股。
    Returns: [{"代码": ..., "名称": ..., "事件类型": ..., "具体事项": ...}]
    """
    reductions = []
    try:
        date_str = datetime.now().strftime("%Y%m%d")
        df = safe_call(ak.stock_gsrl_gsdt_em, date=date_str)
        if df is not None and not df.empty and "具体事项" in df.columns:
            for _, row in df.iterrows():
                detail = str(row.get("具体事项", ""))
                if "减持" in detail:
                    reductions.append({
                        "代码": str(row.get("代码", "")),
                        "名称": str(row.get("简称", "")),
                        "事件类型": str(row.get("事件类型", "")),
                        "具体事项": detail[:200],  # 截断过长文本
                    })
        if reductions:
            print(f"   ⚠️  减持风险: {len(reductions)} 只个股有减持相关公告")
    except Exception as e:
        print(f"   ⚠️  减持信息采集失败: {e}")
    return reductions


def _enrich_consecutive_board(stocks: list, stock_map: dict, reduction_codes: set) -> list:
    """为连板股补充板块类型/PE/减持标记等数据（不覆盖涨停池已有的市值数据）

    Args:
        stocks: 连板股原始列表（已含涨停池的市值/行业/封板时间等）
        stock_map: {代码: {个股行情字段}} 从 fetch_stock_list() 的结果构建
        reduction_codes: 处于减持窗口期的股票代码集合
    Returns: 增强后的连板股列表
    """
    enriched = []
    for s in stocks:
        code = s.get("代码", "")
        detail = stock_map.get(code, {})
        # 优先用涨停池已有数据，stock_map 仅补充缺失字段
        entry = {
            **s,
            "板块类型": _get_board_type(code),
            "总市值": s.get("总市值") or detail.get("总市值", 0),
            "流通市值": s.get("流通市值") or detail.get("流通市值", 0),
            "市盈率-动态": detail.get("市盈率-动态", 0),
            "市净率": detail.get("市净率", 0),
            "60日涨跌幅": detail.get("60日涨跌幅", 0),
            "有减持风险": code in reduction_codes,
        }
        enriched.append(entry)
    return enriched


def fetch_stock_list() -> list:
    """采集全市场个股行情明细（含成交额/换手率/振幅等，供放量筛选使用）

    优先 akshare，失败时降级到新浪 API。
    为控制缓存文件大小，每只股票只保留关键字段。
    """
    stocks = []

    # 方案 A：akshare 全市场行情（字段最全）
    df = safe_call(ak.stock_zh_a_spot_em)
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            try:
                code = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                # 跳过 ST/新股
                if "ST" in name or "N" == name[:1] or "C" == name[:1]:
                    continue
                change_pct = float(row.get("涨跌幅", 0))
                high = float(row.get("最高", 0)) if "最高" in row else 0
                low = float(row.get("最低", 0)) if "最低" in row else 0
                pre_close = float(row.get("昨收", 0)) if "昨收" in row else 0
                amplitude = round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0
                stocks.append({
                    "代码": code,
                    "名称": name,
                    "最新价": float(row.get("最新价", 0)),
                    "涨跌幅": round(change_pct, 2),
                    "成交额": round(float(row.get("成交额", 0)) / 1e8, 2),  # 转亿元
                    "换手率": round(float(row.get("换手率", 0)), 2) if "换手率" in row else 0,
                    "振幅": amplitude,
                    "量比": round(float(row.get("量比", 0)), 2) if "量比" in row else 0,
                    "总市值": round(float(row.get("总市值", 0)) / 1e8, 2) if "总市值" in row else 0,  # 转亿元
                    "流通市值": round(float(row.get("流通市值", 0)) / 1e8, 2) if "流通市值" in row else 0,
                    "市盈率-动态": round(float(row.get("市盈率-动态", 0)), 2) if "市盈率-动态" in row else 0,
                    "市净率": round(float(row.get("市净率", 0)), 2) if "市净率" in row else 0,
                    "60日涨跌幅": round(float(row.get("60日涨跌幅", 0)), 2) if "60日涨跌幅" in row else 0,
                    "年初至今涨跌幅": round(float(row.get("年初至今涨跌幅", 0)), 2) if "年初至今涨跌幅" in row else 0,
                })
            except (ValueError, TypeError):
                continue
        print(f"   ✅ 个股行情(akshare): {len(stocks)} 只")
        return stocks

    # 方案 B：新浪 API 分页采集（24h 可用，但缺换手率/量比）
    print("   ⚠️  akshare 全市场接口失败，尝试新浪财经 API...")
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple"
    for page in range(1, 13):
        try:
            resp = _get_session().get(url, params={
                "page": str(page), "num": "500",
                "sort": "symbol", "asc": "1", "node": "hs_a",
            }, timeout=30)
            if resp.status_code != 200:
                break
            items = json.loads(resp.text)
            if not isinstance(items, list) or not items:
                break
            for item in items:
                name = str(item.get("name", ""))
                if "ST" in name or "N" == name[:1] or "C" == name[:1]:
                    continue
                high = _safe_float(item.get("high", 0))
                low = _safe_float(item.get("low", 0))
                settlement = _safe_float(item.get("settlement", 0))
                amplitude = round((high - low) / settlement * 100, 2) if settlement > 0 else 0
                stocks.append({
                    "代码": str(item.get("code", "")),
                    "名称": name,
                    "最新价": _safe_float(item.get("trade", 0)),
                    "涨跌幅": round(_safe_float(item.get("changepercent", 0)), 2),
                    "成交额": round(_safe_float(item.get("amount", 0)) / 1e8, 2),
                    "换手率": 0,   # 新浪简易接口不含换手率
                    "振幅": amplitude,
                    "量比": 0,    # 新浪简易接口不含量比
                    "总市值": 0,  # 新浪简易接口不含市值
                    "流通市值": 0,
                    "市盈率-动态": 0,
                    "市净率": 0,
                    "60日涨跌幅": 0,
                    "年初至今涨跌幅": 0,
                })
            if len(items) < 500:
                break
        except Exception as e:
            print(f"   ⚠️  新浪API第{page}页失败: {e}")
            break
    print(f"   ✅ 个股行情(新浪API): {len(stocks)} 只")
    return stocks


def collect_all() -> dict:
    """采集所有数据并组装"""
    print("📊 正在采集 A 股行情数据...")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # ---- 基础数据采集 ----
    data = {
        "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "采集日期": today_str,
        "星期": ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()],
        "指数": fetch_index_data(),
        "市场宽度": fetch_market_breadth(),
        "涨跌停": fetch_limit_up_data(),
        "资金流向": fetch_fund_flow(),
        "板块排名": fetch_sector_ranking(),
        "龙虎榜": fetch_dragon_tiger(),
        "连板股": fetch_consecutive_board_stocks(),
        "个股行情": fetch_stock_list(),
    }

    # ---- 成交额环比：从昨日缓存读取 ----
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        cache_files = sorted([f for f in os.listdir(cache_dir) if f.endswith(".json")])
        # 找最近的非今日缓存文件
        prev_amount = 0
        for cf in reversed(cache_files):
            if cf == "latest_data.json":
                continue
            try:
                with open(os.path.join(cache_dir, cf), "r") as f:
                    prev_data = json.load(f)
                prev_amount = prev_data.get("市场宽度", {}).get("总成交额", 0)
                if prev_amount > 0:
                    break
            except Exception:
                continue
        # 也检查 latest_data.json（如果不是今天采集的）
        if prev_amount == 0:
            cache_file = os.path.join(cache_dir, "latest_data.json")
            if os.path.exists(cache_file):
                with open(cache_file, "r") as f:
                    prev_data = json.load(f)
                prev_date = prev_data.get("采集日期", "")
                if prev_date != today_str:
                    prev_amount = prev_data.get("市场宽度", {}).get("总成交额", 0)

        curr_amount = data["市场宽度"].get("总成交额", 0)
        if prev_amount > 0 and curr_amount > 0:
            data["市场宽度"]["成交额环比"] = round((curr_amount - prev_amount) / prev_amount * 100, 2)
            data["市场宽度"]["昨日成交额"] = prev_amount
            print(f"   ✅ 成交额环比: {data['市场宽度']['成交额环比']}% (昨日{prev_amount}亿)")
        else:
            data["市场宽度"]["成交额环比"] = 0.0
    except Exception:
        data["市场宽度"]["成交额环比"] = 0.0

    # ---- 全市场平均涨跌幅 ----
    stock_list = data.get("个股行情", [])
    if stock_list:
        changes = [s["涨跌幅"] for s in stock_list]
        data["市场宽度"]["平均涨跌幅"] = round(sum(changes) / len(changes), 2)
    else:
        data["市场宽度"]["平均涨跌幅"] = 0.0

    # ---- ST/退市股统计 ----
    try:
        df = safe_call(ak.stock_zh_a_spot_em)
        if df is not None and not df.empty:
            st_up = int(len(df[(df["名称"].str.contains("ST", na=False)) & (df["涨跌幅"] > 0)]))
            st_down = int(len(df[(df["名称"].str.contains("ST", na=False)) & (df["涨跌幅"] < 0)]))
            data["风险指标"] = data.get("风险指标", {})
            data["风险指标"]["ST股上涨家数"] = st_up
            data["风险指标"]["ST股下跌家数"] = st_down
        else:
            # 从个股行情中筛选（新浪备用方案没有ST股，因为已被过滤）
            st_stocks = [s for s in stock_list if "ST" in s.get("名称", "")]
            data["风险指标"] = data.get("风险指标", {})
            data["风险指标"]["ST股上涨家数"] = sum(1 for s in st_stocks if s["涨跌幅"] > 0)
            data["风险指标"]["ST股下跌家数"] = sum(1 for s in st_stocks if s["涨跌幅"] < 0)
    except Exception:
        data["风险指标"] = data.get("风险指标", {})
        data["风险指标"]["ST股上涨家数"] = 0
        data["风险指标"]["ST股下跌家数"] = 0

    # ---- 风险指标（从个股行情计算）----
    if stock_list and "跌幅≥5%家数" not in data.get("风险指标", {}):
        down5 = sum(1 for s in stock_list if s["涨跌幅"] <= -5)
        down9 = sum(1 for s in stock_list if s["涨跌幅"] <= -9)
        data["风险指标"]["跌幅≥5%家数"] = down5
        data["风险指标"]["跌幅≥9%家数"] = down9

    # ---- 构建代码→个股行情映射（供连板股增强用）----
    stock_map = {s.get("代码", ""): s for s in stock_list}

    # ---- 减持风险采集 ----
    reduction_list = fetch_reduction_risk()
    reduction_codes = {r["代码"] for r in reduction_list}
    data["减持风险"] = reduction_list

    # ---- 连板股增强：补充市值/PE/板块类型/减持标记 ----
    cb_stocks = data.get("连板股", [])
    if cb_stocks:
        data["连板股"] = _enrich_consecutive_board(cb_stocks, stock_map, reduction_codes)
        print(f"   ✅ 连板股增强: {len(data['连板股'])} 只（已补充市值/PE/板块类型）")

    # ---- 溢价基因：仅对连板股采集（200日历史较重）----
    if cb_stocks:
        premium_data = fetch_premium_gene(cb_stocks)
        data["溢价基因"] = premium_data

    # ---- 板块涨停统计合并到板块排名 ----
    sector_zt = data.get("涨跌停", {}).get("板块涨停统计", {})
    if sector_zt and data.get("板块排名", {}).get("板块列表"):
        for sector_info in data["板块排名"]["板块列表"]:
            sname = sector_info.get("名称", "")
            if sname in sector_zt:
                sector_info["涨停家数"] = sector_zt[sname]
        # 也更新领涨/领跌板块
        for item in data["板块排名"].get("领涨板块", []):
            item["涨停家数"] = sector_zt.get(item.get("名称", ""), 0)
        for item in data["板块排名"].get("领跌板块", []):
            item["涨停家数"] = sector_zt.get(item.get("名称", ""), 0)

    # ---- 数据完整性报告 ----
    missing = []
    if not data["指数"]: missing.append("指数行情")
    if data["市场宽度"]["总家数"] == 0: missing.append("全市场行情(涨跌家数/成交额)")
    if data["资金流向"]["主力资金净流入"] == 0 and not data["资金流向"]["板块主力流入TOP3"]:
        missing.append("资金流向")
    if missing:
        print(f"   ⚠️  数据缺口: {', '.join(missing)}（AI 将联网搜索补充）")

    print("✅ 数据采集完成")
    return data


if __name__ == "__main__":
    result = collect_all()
    output_path = "/Users/leiwanyue/stock-review/logs/data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"📁 数据已保存到 {output_path}")
