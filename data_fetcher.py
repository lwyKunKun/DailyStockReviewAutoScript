"""
数据采集模块 - 使用 akshare 获取 A 股每日行情数据
改进版：Session 保持连接 + 自动重试 + 备用接口
独立运行: python3 data_fetcher.py（打印采集结果）
"""

import json
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
    try:
        codes = ",".join([f"1.{code}" for code in target_indices.values()])
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {"fltt": "2", "invt": "2", "fields": "f2,f3,f4,f5,f6", "secids": codes}
        resp = _get_session().get(url, params=params, timeout=15)
        if resp.status_code == 200:
            items = resp.json().get("data", {}).get("diff", [])
            for item, (name, code) in zip(items, target_indices.items()):
                result[name] = {
                    "代码": code,
                    "最新价": _safe_float(item.get("f2", 0)),
                    "涨跌幅": _safe_float(item.get("f3", 0)),
                    "涨跌额": _safe_float(item.get("f4", 0)),
                    "成交额": _safe_float(item.get("f6", 0)),
                }
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


def fetch_index_data() -> dict:
    """采集主要指数行情（含备用方案）"""
    target_indices = {
        "上证指数": "000001", "深证成指": "399001", "创业板指": "399006",
        "科创50": "000688", "北证50": "899050", "沪深300": "000300",
        "上证50": "000016", "中证500": "000905", "中证1000": "000852",
    }

    # 方案 A：实时行情接口
    df = safe_call(ak.stock_zh_index_spot_em)
    if df is not None and not df.empty:
        result = {}
        for name, code in target_indices.items():
            row = df[df["代码"] == code]
            if not row.empty:
                result[name] = _parse_index_row(row)
        if result:
            print(f"   ✅ 指数数据: 实时接口 ({len(result)} 项)")
            return result

    # 方案 B：HTTP 直接抓取东方财富
    print("   ⚠️  实时接口失败，尝试 HTTP 备用方案...")
    result = _fetch_index_http(target_indices)
    if result:
        return result

    # 方案 C：腾讯行情 API（24小时可用，不依赖东方财富）
    print("   ⚠️  HTTP 备用方案失败，尝试腾讯行情 API...")
    result = _fetch_index_tencent(target_indices)
    if result:
        return result

    # 方案 D：日线历史（最后手段）
    for name, code in target_indices.items():
        prefix = "sh" if code.startswith("6") or code.startswith("0") else "sz"
        try:
            hist = safe_call(ak.stock_zh_index_daily_em, symbol=f"{prefix}{code}")
            if hist is not None and not hist.empty:
                row = hist.iloc[-1]
                result[name] = {"代码": code, "最新价": float(row["close"]), "涨跌幅": float(row.get("pct_chg", 0)), "涨跌额": float(row.get("change", 0)), "成交额": float(row.get("amount", 0))}
        except Exception:
            pass
    if result:
        print(f"   ✅ 指数数据: 历史接口 ({len(result)} 项)")
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
    """采集涨停板相关数据（收盘后数据可用）"""
    result = {"涨停家数": 0, "跌停家数": 0, "涨停股列表": [], "炸板率": 0.0, "连板家数": 0, "最高连板高度": 0}

    date_str = datetime.now().strftime("%Y%m%d")

    # 涨停池
    zt_df = safe_call(ak.stock_zt_pool_em, date=date_str)
    if zt_df is not None and not zt_df.empty:
        zt_df = zt_df[~zt_df["名称"].str.contains("ST", na=False)]
        result["涨停家数"] = len(zt_df)
        for _, row in zt_df.iterrows():
            result["涨停股列表"].append({
                "代码": row.get("代码", ""), "名称": row.get("名称", ""),
                "涨停时间": str(row.get("封板时间", "")),
                "连板数": int(row.get("连板数", 1)) if "连板数" in row else 1,
                "封单金额": float(row.get("封单金额", 0)) if "封单金额" in row else 0,
                "换手率": float(row.get("换手率", 0)) if "换手率" in row else 0,
            })
        if "连板数" in zt_df.columns:
            result["连板家数"] = int(len(zt_df[zt_df["连板数"] >= 2]))
            result["最高连板高度"] = int(zt_df["连板数"].max())
        print(f"   ✅ 涨停池: {result['涨停家数']}家, 连板{result['连板家数']}家")
    else:
        print("   ⚠️  涨停池获取失败")

    # 跌停池
    dt_df = safe_call(ak.stock_zt_pool_dtgc_em, date=date_str)
    if dt_df is not None and not dt_df.empty:
        dt_df = dt_df[~dt_df["名称"].str.contains("ST", na=False)]
        result["跌停家数"] = len(dt_df)
        print(f"   ✅ 跌停池: {result['跌停家数']}家")

    # 炸板
    strong_df = safe_call(ak.stock_zt_pool_strong_em, date=date_str)
    if strong_df is not None and not strong_df.empty and result["涨停家数"] > 0:
        total_attempts = result["涨停家数"] + len(strong_df)
        result["炸板率"] = round(len(strong_df) / total_attempts * 100, 2)

    return result


def fetch_fund_flow() -> dict:
    """采集资金流向"""
    result = {"北向资金净流入": 0.0, "南向资金净流入": 0.0, "主力资金净流入": 0.0, "板块主力流入TOP3": [], "板块主力流出TOP3": []}

    # 北向/南向资金（历史数据，收盘后可用）
    for direction, key in [("北向资金", "北向资金净流入"), ("南向资金", "南向资金净流入")]:
        try:
            df = safe_call(ak.stock_hsgt_hist_em, symbol=direction)
            if df is not None and not df.empty:
                result[key] = float(df.iloc[-1]["净流入"])
        except Exception:
            pass

    # 全市场主力资金
    try:
        df = safe_call(ak.stock_market_fund_flow)
        if df is not None and not df.empty:
            result["主力资金净流入"] = float(df.iloc[-1].get("主力净流入", 0))
    except Exception:
        pass

    # 板块资金排名
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

    print(f"   ✅ 资金流向: 北向{result['北向资金净流入']:.1f}亿, 主力{result['主力资金净流入']:.1f}亿")
    return result


def fetch_sector_ranking() -> dict:
    """采集行业板块排名"""
    result = {"领涨板块": [], "领跌板块": [], "板块列表": []}
    try:
        df = safe_call(ak.stock_board_industry_summary_ths)
        if df is not None and not df.empty:
            sorted_df = df.sort_values("涨跌幅", ascending=False)
            for _, row in sorted_df.head(5).iterrows():
                result["领涨板块"].append({"名称": row.get("板块名称", ""), "涨跌幅": float(row.get("涨跌幅", 0))})
            for _, row in sorted_df.tail(5).iterrows():
                result["领跌板块"].append({"名称": row.get("板块名称", ""), "涨跌幅": float(row.get("涨跌幅", 0))})
            for _, row in sorted_df.iterrows():
                result["板块列表"].append({"名称": row.get("板块名称", ""), "涨跌幅": float(row.get("涨跌幅", 0)), "涨停家数": int(row.get("涨停家数", 0)) if "涨停家数" in row else 0})
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
    """采集连板股详细列表（2板及以上）"""
    stocks = []
    date_str = datetime.now().strftime("%Y%m%d")
    try:
        zt_df = safe_call(ak.stock_zt_pool_em, date=date_str)
        if zt_df is not None and not zt_df.empty:
            zt_df = zt_df[~zt_df["名称"].str.contains("ST", na=False)]
            multi = zt_df[zt_df.get("连板数", 1) >= 2]
            for _, row in multi.iterrows():
                stocks.append({
                    "代码": row.get("代码", ""), "名称": row.get("名称", ""),
                    "连板数": int(row.get("连板数", 2)),
                    "封板时间": str(row.get("封板时间", "")),
                    "换手率": float(row.get("换手率", 0)) if "换手率" in row else 0,
                    "封单金额": float(row.get("封单金额", 0)) if "封单金额" in row else 0,
                    "涨停类型": str(row.get("涨停类型", "")),
                })
    except Exception:
        pass
    return stocks


def collect_all() -> dict:
    """采集所有数据并组装"""
    print("📊 正在采集 A 股行情数据...")
    data = {
        "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "采集日期": datetime.now().strftime("%Y-%m-%d"),
        "星期": ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()],
        "指数": fetch_index_data(),
        "市场宽度": fetch_market_breadth(),
        "涨跌停": fetch_limit_up_data(),
        "资金流向": fetch_fund_flow(),
        "板块排名": fetch_sector_ranking(),
        "龙虎榜": fetch_dragon_tiger(),
        "连板股": fetch_consecutive_board_stocks(),
    }

    # 成交额环比
    data["市场宽度"]["成交额环比"] = 0.0

    # 风险指标（从全市场数据计算）
    try:
        df = safe_call(ak.stock_zh_a_spot_em)
        if df is not None:
            data["风险指标"] = {
                "跌幅≥5%家数": int(len(df[df["涨跌幅"] <= -5])),
                "跌幅≥9%家数": int(len(df[df["涨跌幅"] <= -9])),
            }
        else:
            data["风险指标"] = {"跌幅≥5%家数": 0, "跌幅≥9%家数": 0}
    except Exception:
        data["风险指标"] = {"跌幅≥5%家数": 0, "跌幅≥9%家数": 0}

    # 数据完整性报告
    missing = []
    if not data["指数"]: missing.append("指数行情")
    if data["市场宽度"]["总家数"] == 0: missing.append("全市场行情(涨跌家数/成交额)")
    if data["资金流向"]["主力资金净流入"] == 0: missing.append("主力资金流向")
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
