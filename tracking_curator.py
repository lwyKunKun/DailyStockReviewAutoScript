"""
AI 驱动的股票智能筛选与仪表盘生成模块

在 stock_tracker.py 更新 tracking-db.json 后运行，
读取当日的 AI 分析文档 + 跟踪数据库，通过 DeepSeek 智能筛选，
自动生成 仪表盘.md、核心池.md、观察池.md、退潮池.md。

三层池体系：
  核心池(10只) → 观察池(15只) → 退潮池(无上限)
  - 升降级：核心→观察→退潮，支持退潮回流复活
  - 稳定机制：已在池中的股票给予加分保护，避免频繁变动
  - 变动追踪：仪表盘中展示今日池子变动一览
"""

import json
import os
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_analyzer import analyze
from holiday_checker import is_holiday
from stock_tracker import is_likely_sector_name


def _is_trading_day(date_str: str) -> bool:
    """判断是否是交易日（周一至周五且非法定节假日）"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return True  # 解析失败时宽容处理
    if dt.weekday() >= 5:  # 周六日
        return False
    is_hol, _, _, _ = is_holiday(dt)
    return not is_hol

OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_TRACKING_DIR = os.getenv("OBSIDIAN_TRACKING_DIR", "01-Projects/股票跟踪")
OBSIDIAN_REVIEW_DIR = os.getenv("OBSIDIAN_REVIEW_DIR", "01-Projects/股票复盘")

TRACKING_DIR = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_TRACKING_DIR)
TRACKING_DB_FILE = os.path.join(TRACKING_DIR, "tracking-db.json")
DASHBOARD_FILE = os.path.join(TRACKING_DIR, "仪表盘.md")
CORE_POOL_FILE = os.path.join(TRACKING_DIR, "核心池.md")
WATCH_POOL_FILE = os.path.join(TRACKING_DIR, "观察池.md")
FADING_POOL_FILE = os.path.join(TRACKING_DIR, "退潮池.md")

POOL_STATE_FILE = os.path.join(TRACKING_DIR, "pool_state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "latest_data.json")

MAX_CORE = 10
MAX_WATCH = 15
STALE_DAYS = 5  # 连续 N 天未出现则标记为时间退潮


def _load_db() -> dict:
    if os.path.exists(TRACKING_DB_FILE):
        with open(TRACKING_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 2, "lastUpdated": "", "stocks": {}}


def _find_latest_analysis() -> dict:
    """找到最近一天的 AI 分析文档目录，返回各文档内容"""
    review_base = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_REVIEW_DIR)
    if not os.path.isdir(review_base):
        return {}

    latest_dir = None
    latest_date = ""
    for year_dir in sorted(os.listdir(review_base), reverse=True):
        year_path = os.path.join(review_base, year_dir)
        if not os.path.isdir(year_path) or year_dir.startswith("."):
            continue
        for month_dir in sorted(os.listdir(year_path), reverse=True):
            month_path = os.path.join(year_path, month_dir)
            if not os.path.isdir(month_path) or month_dir.startswith("."):
                continue
            for day_dir in sorted(os.listdir(month_path), reverse=True):
                day_path = os.path.join(month_path, day_dir)
                if not os.path.isdir(day_path) or day_dir.startswith("."):
                    continue
                if os.path.exists(os.path.join(day_path, "复盘.md")):
                    latest_dir = day_path
                    latest_date = day_dir
                    break
            if latest_dir:
                break
        if latest_dir:
            break

    if not latest_dir:
        return {}

    doc_names = ["复盘", "放量筛选", "涨停跌停潮", "消息汇总"]
    docs = {}
    for name in doc_names:
        filepath = os.path.join(latest_dir, f"{name}.md")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                docs[name] = f.read()

    docs["_date"] = latest_date
    docs["_dir"] = latest_dir
    return docs


def _extract_market_summary(docs: dict) -> str:
    """从复盘文档中提取市场概况"""
    fupan = docs.get("复盘", "")
    lines = fupan.split("\n")
    return "\n".join(lines[:200])


def _build_stock_table(db: dict, prices: dict) -> str:
    """构建跟踪池概况表（含最新价格）"""
    stocks = db.get("stocks", {})
    tracking = {k: v for k, v in stocks.items() if v.get("status") == "跟踪中"}

    if not tracking:
        return "（无跟踪中的股票）"

    has_prices = any(prices.get(code) for code in tracking)

    header = "| 代码 | 名称 | 现价 | 信号来源 | 跟踪天数 | 最近出现 | 最高连板 |"
    sep = "|------|------|------|---------|---------|---------|---------|"
    if not has_prices:
        header = "| 代码 | 名称 | 信号来源 | 跟踪天数 | 最近出现 | 最高连板 |"
        sep = "|------|------|---------|---------|---------|---------|"

    lines = [
        f"共 {len(tracking)} 只跟踪中的股票：",
        "",
        header,
        sep,
    ]

    for code, s in sorted(tracking.items(), key=lambda x: x[0]):
        sources = list(set(h.get("source", "") for h in s.get("history", [])))
        src_str = "+".join(sources) if sources else "-"
        try:
            first = datetime.strptime(s["firstSeen"], "%Y-%m-%d")
            days = (datetime.now() - first).days + 1
        except (ValueError, KeyError):
            days = 0
        price_str = f"| {prices.get(code, '--')} " if has_prices else ""
        lines.append(
            f"| {code} | {s['name']} | {price_str}"
            f"| {src_str} | {days}天 "
            f"| {s.get('lastSeen', '-')} | {s.get('maxBoard', 0)}板 |"
        )

    return "\n".join(lines)


def _get_current_prices() -> dict:
    """从缓存文件中获取个股最新价格，返回 {代码: 价格} 映射"""
    prices = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for stock in data.get("个股行情", []):
                code = stock.get("代码", "")
                price = stock.get("最新价")
                if code and price is not None:
                    try:
                        prices[code] = float(price)
                    except (ValueError, TypeError):
                        pass
            if prices:
                print(f"   💰 从缓存获取到 {len(prices)} 只股票价格")
        except Exception as e:
            print(f"   ⚠️ 读取价格缓存失败: {e}")
    return prices


def _extract_price_from_docs(docs: dict, code: str) -> float:
    """尝试从分析文档中提取某只股票的价格（备用方案）"""
    return None


def _load_pool_state() -> dict:
    """加载池状态文件（独立于 tracking-db.json）"""
    if os.path.exists(POOL_STATE_FILE):
        with open(POOL_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_pool_state(state: dict):
    """保存池状态文件"""
    with open(POOL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _get_yesterday_pools(pool_state: dict, today: str) -> dict:
    """从 pool_state 快照中读取最近一天的池子状态

    返回: {"core": set(), "watch": set(), "fading": set(), "date": "..."}
    若无历史快照则返回空集合。
    """
    snapshots = pool_state.get("_snapshots", {})
    # 按日期降序排列，找最近的非今日快照
    sorted_dates = sorted(snapshots.keys(), reverse=True)
    yesterday_pools = {"core": set(), "watch": set(), "fading": set(), "date": ""}

    for date in sorted_dates:
        if date == today:
            continue
        snap = snapshots[date]
        yesterday_pools["core"] = set(snap.get("core", []))
        yesterday_pools["watch"] = set(snap.get("watch", []))
        yesterday_pools["fading"] = set(snap.get("fading", []))
        yesterday_pools["date"] = date
        print(f"   📅 昨日快照日期: {date} (核心{len(yesterday_pools['core'])} 观察{len(yesterday_pools['watch'])} 退潮{len(yesterday_pools['fading'])})")
        break

    return yesterday_pools


def _backfill_snapshots_if_needed(pool_state: dict):
    """如果 _snapshots 为空但 pool_state 中有入池/出池历史记录，
    从历史记录反推每天的快照（仅核心池和观察池，退潮池旧数据无法还原）。
    """
    if pool_state.get("_snapshots"):
        return  # 已有快照，不需要回填

    # 检查是否有可回填的历史记录
    has_history = False
    for key, val in pool_state.items():
        if key.startswith("_"):
            continue
        if isinstance(val, list) and len(val) > 0:
            has_history = True
            break

    if not has_history:
        return

    # 收集所有出现的日期范围
    all_dates = set()
    for key, val in pool_state.items():
        if key.startswith("_"):
            continue
        if not isinstance(val, list):
            continue
        for entry in val:
            for date_field in ("entryDate", "exitDate"):
                d = entry.get(date_field)
                if d and d != "null":
                    all_dates.add(d)

    if not all_dates:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    all_dates.add(today)

    min_date = min(all_dates)
    max_date = max(all_dates)

    # 生成日期范围内的每一天
    from datetime import timedelta
    try:
        start = datetime.strptime(min_date, "%Y-%m-%d")
        end = datetime.strptime(max_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return

    snapshots = {}
    current = start
    while current <= end:
        if not _is_trading_day(current.strftime("%Y-%m-%d")):
            current += timedelta(days=1)
            continue
        date_str = current.strftime("%Y-%m-%d")
        core_codes = set()
        watch_codes = set()

        for code, history in pool_state.items():
            if code.startswith("_"):
                continue
            if not isinstance(history, list):
                continue
            for entry in history:
                entry_date = entry.get("entryDate", "")
                exit_date = entry.get("exitDate")
                pool = entry.get("pool", "")

                # 检查该股票在此日期是否在该池子中
                if not entry_date:
                    continue
                if (date_str >= entry_date and
                        (exit_date is None or date_str < exit_date)):
                    if pool == "核心池":
                        core_codes.add(code)
                    elif pool == "观察池":
                        watch_codes.add(code)

        # 只保存有数据的日期
        if core_codes or watch_codes:
            snapshots[date_str] = {
                "core": sorted(list(core_codes)),
                "watch": sorted(list(watch_codes)),
                "fading": [],  # 旧数据无法还原退潮池
            }

        current += timedelta(days=1)

    if snapshots:
        pool_state["_snapshots"] = snapshots
        _save_pool_state(pool_state)
        print(f"   📜 历史快照回填完成: {len(snapshots)} 天 ({min_date} ~ {max_date})")


def _save_snapshot(pool_state: dict, today: str, core_codes: set, watch_codes: set, fading_codes: set):
    """保存今日池子快照到 pool_state"""
    if "_snapshots" not in pool_state:
        pool_state["_snapshots"] = {}

    pool_state["_snapshots"][today] = {
        "core": sorted(list(core_codes)),
        "watch": sorted(list(watch_codes)),
        "fading": sorted(list(fading_codes)),
    }

    # 只保留最近 90 天的快照，避免文件无限膨胀
    snapshots = pool_state["_snapshots"]
    sorted_dates = sorted(snapshots.keys(), reverse=True)
    if len(sorted_dates) > 90:
        for old_date in sorted_dates[90:]:
            del snapshots[old_date]

    _save_pool_state(pool_state)


def _update_pool_state(core_codes: set, watch_codes: set, prices: dict, today: str):
    """更新池状态：记录新入池股票的入池日期和入池价格"""
    all_active = core_codes | watch_codes

    state = _load_pool_state()
    new_entries = 0

    for code in all_active:
        pool = "核心池" if code in core_codes else "观察池"
        history = state.get(code, [])

        # 类型安全：跳过快照字段
        if not isinstance(history, list):
            continue

        # 检查当前是否已在此池中
        in_pool = any(h.get("pool") == pool and h.get("exitDate") is None for h in history)

        if in_pool:
            continue

        # 如果之前在另一个池中，先标记退出
        for h in history:
            if h.get("exitDate") is None and h.get("pool") != pool:
                h["exitDate"] = today

        # 记录新入池
        history.append({
            "pool": pool,
            "entryDate": today,
            "entryPrice": prices.get(code),
            "exitDate": None,
        })
        state[code] = history
        new_entries += 1

    # 标记已退出池的股票
    for code in list(state.keys()):
        if code.startswith("_"):
            continue
        if code not in all_active:
            history = state.get(code, [])
            if isinstance(history, list):
                for h in history:
                    if h.get("exitDate") is None:
                        h["exitDate"] = today

    if new_entries > 0:
        _save_pool_state(state)
        print(f"   🆕 新入池: {new_entries} 只")

    return state


def _get_entry_info(stock_code: str, pool_state: dict, prices: dict) -> dict:
    """获取某只股票的入池信息，包含入池日期、入池价格、当前价格、收益%"""
    history = pool_state.get(stock_code, [])
    if not isinstance(history, list):
        history = []

    # 找最后一条未退出的入池记录
    entry = None
    for h in reversed(history):
        if h.get("exitDate") is None:
            entry = h
            break
    if not entry and history:
        entry = history[-1]

    result = {
        "entryDate": entry.get("entryDate", "-") if entry else "-",
        "entryPrice": entry.get("entryPrice") if entry else None,
        "currentPrice": prices.get(stock_code),
        "daysInPool": 0,
        "returnPct": None,
    }

    if entry and entry.get("entryDate"):
        try:
            entry_dt = datetime.strptime(entry["entryDate"], "%Y-%m-%d")
            result["daysInPool"] = (datetime.now() - entry_dt).days
        except (ValueError, TypeError):
            pass

    entry_price = result["entryPrice"]
    current_price = result["currentPrice"]
    if entry_price and current_price and entry_price > 0:
        result["returnPct"] = round((current_price - entry_price) / entry_price * 100, 2)

    return result


def _format_return(pct) -> str:
    """格式化收益率显示"""
    if pct is None:
        return "--"
    if pct > 0:
        return f"+{pct}%"
    elif pct < 0:
        return f"{pct}%"
    return "0%"


def _build_price_reference(docs: dict, prices: dict) -> str:
    """从分析文档中提取所有出现的股票代码，构建含现价的价格参考表"""
    all_text = ""
    for name in ["放量筛选", "涨停跌停潮", "复盘"]:
        all_text += docs.get(name, "")

    codes_seen = {}  # code -> name（改用 dict，便于用正确名称覆盖板块名）
    for m in re.finditer(r"\|\s*(\d{6})\s*\|\s*([\u4e00-\u9fa5]{2,8})\s*\|", all_text):
        code, name = m.group(1), m.group(2)
        if code not in codes_seen or (is_likely_sector_name(codes_seen[code]) and not is_likely_sector_name(name)):
            codes_seen[code] = name
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,8})\s*[（(]\s*(\d{6})\s*[）)]", all_text):
        code, name = m.group(2), m.group(1)
        if code not in codes_seen or (is_likely_sector_name(codes_seen[code]) and not is_likely_sector_name(name)):
            codes_seen[code] = name

    if not codes_seen:
        return ""

    lines = [
        f"分析文档中出现的 {len(codes_seen)} 只股票的价格参考：",
        "",
        "| 代码 | 名称 | 现价 |",
        "|------|------|------|",
    ]
    for code, name in sorted(codes_seen.items()):
        price = prices.get(code)
        price_str = str(price) if price else "--"
        lines.append(f"| {code} | {name} | {price_str} |")

    return "\n".join(lines)


def _get_recent_trading_days(n: int) -> set:
    """获取最近 n 个交易日的日期字符串集合（从今天往回数，跳过非交易日）"""
    result = set()
    today = datetime.now()
    current = today
    count = 0
    max_iter = n * 3  # 防止无限循环（例如长假期）
    while count < n and max_iter > 0:
        max_iter -= 1
        date_str = current.strftime("%Y-%m-%d")
        if _is_trading_day(date_str) and date_str <= today.strftime("%Y-%m-%d"):
            result.add(date_str)
            count += 1
        current -= timedelta(days=1)
    return result


def _count_recent_appearances(stock_entry: dict, recent_days: set) -> int:
    """统计某只股票在指定日期集合中出现了多少天（从 history 记录中提取）"""
    appeared_days = set()
    for h in stock_entry.get("history", []):
        d = h.get("date", "")
        if d in recent_days:
            appeared_days.add(d)
    return len(appeared_days)


def _build_doc_candidate_table(docs: dict, prices: dict, db: dict,
                                yesterday_pools: dict) -> tuple:
    """构建候选标的表格（三层来源并集）

    候选池 = 当日文档中的股票 ∪ 昨日池子(核心+观察) ∪ 近5日活跃标的
    返回: (表格字符串, 候选数量)
    """
    # 1. 从分析文档中提取股票代码（复用 _build_price_reference 的提取逻辑）
    doc_codes = {}  # code -> name
    all_text = ""
    for name in ["放量筛选", "涨停跌停潮", "复盘", "消息汇总"]:
        all_text += docs.get(name, "")

    for m in re.finditer(r"\|\s*(\d{6})\s*\|\s*([\u4e00-\u9fa5]{2,8})\s*\|", all_text):
        code, name = m.group(1), m.group(2)
        # 如果名称是板块名但不存更好候选，先占位；如果已有板块名则用正确名称覆盖
        if code not in doc_codes or (is_likely_sector_name(doc_codes[code]) and not is_likely_sector_name(name)):
            doc_codes[code] = name
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,8})\s*[（(]\s*(\d{6})\s*[）)]", all_text):
        code, name = m.group(2), m.group(1)
        if code not in doc_codes or (is_likely_sector_name(doc_codes[code]) and not is_likely_sector_name(name)):
            doc_codes[code] = name

    # 2. 昨日池子
    yesterday_core = yesterday_pools.get("core", set())
    yesterday_watch = yesterday_pools.get("watch", set())
    yesterday_fading = yesterday_pools.get("fading", set())

    # 3. 近5个交易日日期集合（用于统计出现次数和活跃标的筛选）
    recent_5d = _get_recent_trading_days(5)

    # 4. 构建候选池：{code: {name, source_tags[], current_pool, recent_count}}
    candidates = {}

    def _add_candidate(code, name, tag):
        if code in candidates:
            if tag not in candidates[code]["source_tags"]:
                candidates[code]["source_tags"].append(tag)
        else:
            candidates[code] = {"name": name, "source_tags": [tag], "current_pool": "", "recent_count": 0}

    # 第一层：文档中的股票
    for code, name in doc_codes.items():
        _add_candidate(code, name, "文档")

    # 第二层：昨日核心池 + 观察池
    for code in yesterday_core:
        stock = db.get("stocks", {}).get(code)
        name = stock.get("name", code) if stock else code
        _add_candidate(code, name, "昨日核心")

    for code in yesterday_watch:
        stock = db.get("stocks", {}).get(code)
        name = stock.get("name", code) if stock else code
        _add_candidate(code, name, "昨日观察")

    # 第三层：tracking-db 中"跟踪中"且近5日出现 ≥2 次的活跃标的
    for code, entry in db.get("stocks", {}).items():
        if code in candidates:
            continue
        if entry.get("status") != "跟踪中":
            continue
        recent = _count_recent_appearances(entry, recent_5d)
        if recent >= 2:
            _add_candidate(code, entry.get("name", code), "活跃标的")
            candidates[code]["recent_count"] = recent

    if not candidates:
        return "（无候选标的）", 0

    # 5. 补全"近5日出现"和"当前池"
    for code, info in candidates.items():
        # 近5日出现
        if info["recent_count"] == 0:
            stock = db.get("stocks", {}).get(code)
            if stock:
                info["recent_count"] = _count_recent_appearances(stock, recent_5d)

        # 当前池
        if code in yesterday_core:
            info["current_pool"] = "核心池"
        elif code in yesterday_watch:
            info["current_pool"] = "观察池"
        elif code in yesterday_fading:
            info["current_pool"] = "退潮池→候选"
        else:
            info["current_pool"] = "--"

    # 6. 构建表格
    has_prices = any(prices.get(code) for code in candidates)

    header = "| 代码 | 名称 | 现价 | 近5日出现 | 当前池 | 来源 |"
    sep = "|------|------|------|----------|--------|------|"
    if not has_prices:
        header = "| 代码 | 名称 | 近5日出现 | 当前池 | 来源 |"
        sep = "|------|------|----------|--------|------|"

    doc_only = len(doc_codes)
    pool_only = sum(1 for c in candidates if "文档" not in candidates[c]["source_tags"]
                    and ("昨日核心" in candidates[c]["source_tags"] or "昨日观察" in candidates[c]["source_tags"]))
    active_only = sum(1 for c in candidates if candidates[c]["source_tags"] == ["活跃标的"])

    lines = [
        f"共 {len(candidates)} 只候选标的（文档{doc_only}只 + 昨日池子独有{pool_only}只 + 活跃标的{active_only}只）：",
        "",
        header,
        sep,
    ]

    for code in sorted(candidates.keys()):
        info = candidates[code]
        price_str = f"| {prices.get(code, '--')} " if has_prices else ""
        recent_str = f"{info['recent_count']}/5" if info["recent_count"] > 0 else "--"
        source_str = "+".join(info["source_tags"])
        lines.append(
            f"| {code} | {info['name']} | {price_str}| {recent_str} | {info['current_pool']} | {source_str} |"
        )

    return "\n".join(lines), len(candidates)


def _build_stability_context(yesterday_pools: dict, pool_state: dict) -> str:
    """构建稳定机制上下文 —— 告诉 AI 昨天池子里有什么，让它优先保留"""
    if not yesterday_pools.get("date"):
        return ""

    parts = [f"## 昨日池子状态（{yesterday_pools['date']}）", ""]

    # 昨日核心池
    core_codes = sorted(yesterday_pools.get("core", []))
    if core_codes:
        parts.append(f"**昨日核心池（{len(core_codes)}只）**：")
        for code in core_codes:
            history = pool_state.get(code, [])
            name = ""
            if isinstance(history, list) and history:
                entry = history[-1]
                # 从 tracking-db 中获取名称
                db = _load_db()
                name = db.get("stocks", {}).get(code, {}).get("name", "")
            parts.append(f"- {code} {name}")
        parts.append("")

    # 昨日观察池
    watch_codes = sorted(yesterday_pools.get("watch", []))
    if watch_codes:
        parts.append(f"**昨日观察池（{len(watch_codes)}只）**：")
        db = _load_db()
        for code in watch_codes:
            name = db.get("stocks", {}).get(code, {}).get("name", "")
            parts.append(f"- {code} {name}")
        parts.append("")

    parts.append("**稳定原则**：昨日已在核心池的标的，除非今日出现明确走弱信号（跌停、放量滞涨、板块退潮），否则应优先保留在核心池或至少保留在观察池中，避免因一日波动就频繁调出。")

    return "\n".join(parts)


def _build_curation_prompt(db: dict, docs: dict, prices: dict, today: str,
                           yesterday_pools: dict = None, pool_state: dict = None) -> str:
    """构建发给 DeepSeek 的筛选 prompt（含真实价格数据 + 昨日池子稳定上下文）

    去掉 suggested_drops 字段 —— 剔除功能由退潮池承担。
    """
    market_summary = _extract_market_summary(docs)
    stock_table, candidate_count = _build_doc_candidate_table(docs, prices, db,
                                                               yesterday_pools or {})
    price_ref = _build_price_reference(docs, prices)

    volume_doc = docs.get("放量筛选", "")
    limit_doc = docs.get("涨停跌停潮", "")
    news_doc = docs.get("消息汇总", "")

    # 构建稳定上下文
    stability_context = ""
    if yesterday_pools and pool_state:
        stability_context = _build_stability_context(yesterday_pools, pool_state)

    prompt = f"""你是专业的A股短线交易策略师。请根据以下市场数据和股票信号，筛选出真正值得跟踪和交易的标的，并给出具体的技术分析和交易计划。

**重要：下面两张表中的"现价"是真实的市场收盘价，所有技术面分析必须以现价为基准！**

## 市场环境（来自当日AI复盘）

{market_summary}

## 放量筛选分析（含具体量价数据）

{volume_doc[:5000]}

## 涨停跌停潮分析（含连板、封板数据）

{limit_doc[:3000]}

## 消息面汇总

{news_doc[:2000]}

## 今日候选标的（{candidate_count}只，来自当日分析文档+昨日池子+活跃标的）

{stock_table}

## 全量价格参考（分析文档中出现的所有股票）

{price_ref}

{stability_context}

---

## 你的任务

请基于以上全部信息，完成筛选并给出详细分析。**关键要求：读懂分析报告中的具体量价数据、封板质量、板块效应来做判断，而不是机械数信号数量。**

### ⚠️ 技术面分析铁律

1. **支撑位必须低于现价**，阻力位必须高于现价。如果现价15元，支撑位不能是25元。
2. 支撑位看下方：近期低点、均线、整数关口、涨停板开盘价
3. 阻力位看上方：前期高点、密集成交区、整数关口
4. 价格数字必须与现价有合理关系，不要凭空捏造

### ⚠️ 池子稳定原则（结合表格中的"近5日出现"和"当前池"列）

1. 昨日已在核心池的标的（"当前池"列=核心池），如无明确走弱信号（跌停、放量滞涨、板块退潮），应优先保留
2. 昨日已在观察池的标的（"当前池"列=观察池），如有持续改善（量价配合、板块回暖），可优先升级
3. "近5日出现"≥3/5 的标的通常比 1/5 的标的更有持续跟踪价值，即使当日信号不突出也值得保留
4. 避免因为单日波动就频繁调入调出，保持池子稳定性

### 筛选标准

**核心池（最多{MAX_CORE}只）—— 近期可能交易的标的：**
- 属于当前市场主线板块
- 多信号共振且有清晰的上涨逻辑
- 优先考虑"近5日出现"≥3/5 的持续活跃标的
- 有板块效应（同板块多只标的共振）
- 排除：纯消息驱动无技术面支撑、退市股、单日游资炒作

**观察池（最多{MAX_WATCH}只）—— 有潜力但条件未完全满足：**
- 信号共振但跟踪天数不够，或板块方向待确认
- 等待关键位置突破或回踩确认
- 昨日核心池降级标的（需说明降级原因）
- "近5日出现"=1/5 或 2/5 的新面孔适合先放观察池跟踪

### 输出格式

请严格按以下JSON格式输出（不要输出其他内容）：

```json
{{
  "market_assessment": {{
    "index_trend": "下跌/震荡/上涨",
    "sentiment": "冰点/偏冷/温/偏热/过热",
    "main_themes": ["主线板块1", "主线板块2"],
    "volume_assessment": "放量/正常/缩量",
    "strategy": "今日操作策略建议（一句话）"
  }},
  "core_pool": [
    {{
      "code": "股票代码",
      "name": "股票名称",
      "sector": "所属板块/题材",
      "confidence": 5,
      "reason": "入选核心池的理由（基于分析报告中的具体数据，若为昨日核心池保留需说明保留原因）",
      "trend": "上升/下降/震荡",
      "technical": {{
        "support": "支撑位及理由（必须低于现价！如：现价19.5元，支撑位约17.8元，是5日均线+前低共振位）",
        "resistance": "阻力位及理由（必须高于现价！如：现价19.5元，阻力位约22.0元，是前高+整数关口）",
        "ma_status": "均线状态描述（如：站上5/10日线，20日线走平）",
        "volume_profile": "量能特征描述（如：放量1.8倍突破，换手率XX%）"
      }},
      "trade_plan": {{
        "entry_zone": "建议入场价格区间及方式（如：回踩42.5-43.5低吸，或突破45.2追涨）",
        "stop_loss": "止损位及百分比（必须低于入场价！如：41.0元，约-3.5%）",
        "target_1": "第一目标位及百分比（必须高于入场价！如：48.0元，约+10%）",
        "target_2": "第二目标位（如有，否则填'--'）",
        "position_pct": "建议仓位百分比（如：10%）",
        "trigger": "具体入场触发条件（如：回踩5日线不破+分时放量拉升）"
      }}
    }}
  ],
  "watch_pool": [
    {{
      "code": "股票代码",
      "name": "股票名称",
      "sector": "所属板块/题材",
      "reason": "关注逻辑（基于分析报告中的具体数据，若为昨日核心池降级需说明降级原因）",
      "trend": "上升/下降/震荡",
      "downgrade_reason": "若从核心池降级，说明具体原因；否则填 null",
      "technical": {{
        "support": "支撑位",
        "resistance": "阻力位",
        "ma_status": "均线状态",
        "volume_profile": "量能特征"
      }},
      "promotion_condition": "升级到核心池需要满足什么条件"
    }}
  ]
}}
```

### 重要约束
1. 核心池最多{MAX_CORE}只，观察池最多{MAX_WATCH}只
2. core_pool 和 watch_pool 都按推荐程度从高到低排序，最重要的排在最前面
3. confidence 取值 1-5，5表示最高把握
4. **所有价格必须以表格中的"现价"为基准，支撑位<现价<阻力位。如果某股票在两张表中都有出现，以前者为准。找不到现价的股票，从分析报告中的涨跌幅倒推估算。**
5. 不要捏造股票代码或名称，只从候选标的表格中选择
6. 只输出JSON，不要输出任何解释性文字"""

    return prompt


def _parse_response(text: str) -> dict:
    """从 AI 返回的文本中提取 JSON"""
    m = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        raw = m.group(0)
        raw = re.sub(r",\s*(\}|\])", r"\1", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    print("   ⚠️ 无法解析 AI 返回的 JSON，使用空结果")
    print(f"   AI 返回前500字符: {text[:500]}")
    return {}


# ============================================================
# 变动追踪：昨天 vs 今天池子对比
# ============================================================

def _compute_changes(yesterday_pools: dict, core_codes: set, watch_codes: set,
                     fading_codes: set, ai_core: list, ai_watch: list) -> list[dict]:
    """对比昨天和今天的池子，生成变动记录列表

    每条记录: {"code": str, "name": str, "change": str, "detail": str}
    change 取值: new_core, new_watch, upgrade, downgrade_core, downgrade_fading, revive, stable
    """
    changes = []

    yesterday_core = yesterday_pools.get("core", set())
    yesterday_watch = yesterday_pools.get("watch", set())
    yesterday_fading = yesterday_pools.get("fading", set())
    yesterday_all = yesterday_core | yesterday_watch | yesterday_fading

    db = _load_db()
    stocks = db.get("stocks", {})

    def _name(code):
        return stocks.get(code, {}).get("name", code)

    # 1. 不在昨日任何池中 → 新进
    for code in core_codes - yesterday_all:
        changes.append({
            "code": code, "name": _name(code),
            "change": "new_core", "label": "🔥 新进核心池",
            "detail": "首次进入核心池",
        })
    for code in watch_codes - yesterday_all:
        changes.append({
            "code": code, "name": _name(code),
            "change": "new_watch", "label": "🆕 新进观察池",
            "detail": "首次进入观察池",
        })

    # 2. 昨日观察池 → 今日核心池（升级）
    for code in yesterday_watch & core_codes:
        changes.append({
            "code": code, "name": _name(code),
            "change": "upgrade", "label": "⬆️ 升为核心",
            "detail": "观察池升级到核心池",
        })

    # 3. 昨日核心池 → 今日观察池（降级）
    for code in yesterday_core & watch_codes:
        # 从 AI 返回的 watch_pool 中找到降级原因
        reason = ""
        for s in ai_watch:
            if s.get("code") == code:
                reason = s.get("downgrade_reason") or ""
                break
        changes.append({
            "code": code, "name": _name(code),
            "change": "downgrade_core", "label": "⬇️ 降为观察",
            "detail": f"核心池降级到观察池{f'：{reason}' if reason else ''}",
        })

    # 4. 昨日核心池 → 今日不在任何池（强制降级到观望或退潮）
    for code in yesterday_core - core_codes - watch_codes:
        if code in fading_codes:
            changes.append({
                "code": code, "name": _name(code),
                "change": "downgrade_fading", "label": "❄️ 降为退潮",
                "detail": "核心池标的失去关注价值，进入退潮池",
            })
        else:
            changes.append({
                "code": code, "name": _name(code),
                "change": "downgrade_core", "label": "⬇️ 降为观察",
                "detail": "核心池降级（稳定性保护：至少保留观察）",
            })

    # 5. 昨日观察池 → 今日不在任何池（降为退潮）
    for code in yesterday_watch - core_codes - watch_codes:
        changes.append({
            "code": code, "name": _name(code),
            "change": "downgrade_fading", "label": "❄️ 降为退潮",
            "detail": "观察池标的被挤出，进入退潮池",
        })

    # 6. 昨日退潮池 → 今日核心或观察池（回流复活）
    for code in yesterday_fading & (core_codes | watch_codes):
        target = "核心池" if code in core_codes else "观察池"
        changes.append({
            "code": code, "name": _name(code),
            "change": "revive", "label": "🔄 回流复活",
            "detail": f"退潮池回流至{target}",
        })

    # 7. 稳定保持的（不显示，但记录统计用）
    stable_core = yesterday_core & core_codes
    stable_watch = yesterday_watch & watch_codes
    if stable_core:
        changes.append({
            "code": "", "name": "",
            "change": "stable", "label": "— 核心池稳定",
            "detail": f"{len(stable_core)}只标的保持不变",
            "count": len(stable_core),
        })
    if stable_watch:
        changes.append({
            "code": "", "name": "",
            "change": "stable", "label": "— 观察池稳定",
            "detail": f"{len(stable_watch)}只标的保持不变",
            "count": len(stable_watch),
        })

    return changes


# ============================================================
# 退潮池分类与生成
# ============================================================

def _classify_fading_stocks(fading_codes: set, yesterday_pools: dict, db: dict) -> dict:
    """将退潮池股票按原因分类

    返回: {
        "自然降级": [{"code": str, "name": str, "detail": str}, ...],
        "时间退潮": [{"code": str, "name": str, "detail": str, "lastSeen": str, "remaining": int}, ...],
        "持续观察": [{"code": str, "name": str, "detail": str}, ...],
    }
    """
    result = {"自然降级": [], "时间退潮": [], "持续观察": []}

    yesterday_watch = yesterday_pools.get("watch", set())
    yesterday_fading = yesterday_pools.get("fading", set())
    yesterday_core = yesterday_pools.get("core", set())
    stocks = db.get("stocks", {})

    def _name(code):
        return stocks.get(code, {}).get("name", code)

    today = datetime.now()

    for code in fading_codes:
        stock = stocks.get(code, {})
        name = _name(code)

        # 分类1: 昨日观察池降下来的 → 自然降级
        if code in yesterday_watch:
            result["自然降级"].append({
                "code": code, "name": name,
                "detail": "昨日在观察池中，今日被更好标的挤出",
                "lastSeen": stock.get("lastSeen", "-"),
            })
        # 分类2: 昨日核心池降下来的 → 也是自然降级（但更严重）
        elif code in yesterday_core:
            result["自然降级"].append({
                "code": code, "name": name,
                "detail": "昨日在核心池中，今日被降级",
                "lastSeen": stock.get("lastSeen", "-"),
            })
        # 分类3: 昨日已在退潮池 → 持续观察
        elif code in yesterday_fading:
            result["持续观察"].append({
                "code": code, "name": name,
                "detail": "已在退潮池中，继续观察",
                "lastSeen": stock.get("lastSeen", "-"),
            })
        # 分类4: 时间退潮（从 tracking-db 来的）
        else:
            try:
                last_seen = datetime.strptime(stock.get("lastSeen", ""), "%Y-%m-%d")
                days_since = (today - last_seen).days
                remaining = max(0, STALE_DAYS - days_since)
            except (ValueError, KeyError):
                days_since = 0
                remaining = 0
            result["时间退潮"].append({
                "code": code, "name": name,
                "detail": f"连续{days_since}天未在分析中出现",
                "lastSeen": stock.get("lastSeen", "-"),
                "remaining": remaining,
            })

    return result


def _get_tracking_db_fading_stocks(db: dict) -> set:
    """从 tracking-db 中获取时间退潮的股票代码"""
    today = datetime.now()
    three_days_ago = today - timedelta(days=3)
    fading = set()
    for code, s in db.get("stocks", {}).items():
        if s.get("status") != "跟踪中":
            continue
        try:
            last_seen = datetime.strptime(s["lastSeen"], "%Y-%m-%d")
            if last_seen <= three_days_ago:
                fading.add(code)
        except (ValueError, KeyError):
            pass
    return fading


# ============================================================
# 池子流向面板（独立文件）
# ============================================================

FLOW_PANEL_FILE = os.path.join(TRACKING_DIR, "池子流向.md")

# 池子颜色映射
POOL_COLORS = {
    "核心池": {"bg": "#FFE0E0", "text": "#C0392B", "label": "核心"},
    "观察池": {"bg": "#FFF3CD", "text": "#B7950B", "label": "观察"},
    "退潮池": {"bg": "#D5F5E3", "text": "#1E8449", "label": "退潮"},
}


def _generate_flow_panel(pool_state: dict, today: str, today_core: set, today_watch: set,
                          today_fading: set):
    """生成池子流向面板 —— 每只股票近5天在三级池中的流动矩阵

    用带背景色的 HTML 表格展示，红色=核心池，黄色=观察池，绿色=退潮池。
    输出到独立文件 池子流向.md。
    """
    snapshots = pool_state.get("_snapshots", {})

    # 收集最近5天每天的池子归属
    all_days_data = {}  # {date: {code: pool_name}}
    all_codes = set()

    # 历史快照
    for date_str, snap in snapshots.items():
        day_map = {}
        for code in snap.get("core", []):
            day_map[code] = "核心池"
            all_codes.add(code)
        for code in snap.get("watch", []):
            if code not in day_map:
                day_map[code] = "观察池"
            all_codes.add(code)
        for code in snap.get("fading", []):
            if code not in day_map:
                day_map[code] = "退潮池"
            all_codes.add(code)
        all_days_data[date_str] = day_map

    # 今日数据
    today_map = {}
    for code in today_core:
        today_map[code] = "核心池"
        all_codes.add(code)
    for code in today_watch:
        if code not in today_map:
            today_map[code] = "观察池"
        all_codes.add(code)
    for code in today_fading:
        if code not in today_map:
            today_map[code] = "退潮池"
        all_codes.add(code)
    all_days_data[today] = today_map

    # 取最近5天
    # 取最近5个交易日（跳过周末+法定节假日）
    all_sorted = sorted(all_days_data.keys(), reverse=True)
    sorted_dates = []
    for d in all_sorted:
        if _is_trading_day(d):
            sorted_dates.append(d)
        if len(sorted_dates) >= 5:
            break
    sorted_dates.reverse()

    if len(sorted_dates) < 2:
        return

    def _fmt_date(d):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            return dt.strftime("%m-%d")
        except (ValueError, TypeError):
            return d

    # 获取股票名称
    db = _load_db()
    stocks = db.get("stocks", {})

    def _name(code):
        return stocks.get(code, {}).get("name", code)

    # 排序：三层复合排序 —— 池子分组 → 动作信号 → 连续天数 → 代码兜底
    # 昨日归属（用于判断动作信号）
    pool_yesterday_map = {}
    if len(sorted_dates) >= 2:
        pool_yesterday_map = all_days_data.get(sorted_dates[-2], {})

    def _consecutive_days(code, target_pool):
        """计算股票在 target_pool 中连续停留的天数（从今天往前数）"""
        days = 0
        for d in reversed(sorted_dates):
            p = all_days_data.get(d, {}).get(code, "")
            if p == target_pool:
                days += 1
            else:
                break
        return days

    def _sort_key(code):
        # 第一层：池子分组（核心=0，观察=1，其他=2）
        pool_today = all_days_data.get(today, {}).get(code, "")
        if pool_today == "核心池":
            group = 0
        elif pool_today == "观察池":
            group = 1
        else:
            group = 2

        pool_yesterday = pool_yesterday_map.get(code, "")

        # 第二层：动作信号
        # 0=新进（昨天不在任何池） 1=升级/回流（池子向上） 2=连续稳定 3=降级（池子向下）
        if not pool_yesterday:
            action = 0   # 新进
        elif pool_today == pool_yesterday:
            action = 2   # 连续稳定
        elif pool_today == "核心池":
            action = 1   # 观察/退潮 → 核心，升级/回流
        elif pool_today == "观察池" and pool_yesterday in ("退潮池",):
            action = 1   # 退潮 → 观察，回流
        elif pool_today == "观察池" and pool_yesterday == "核心池":
            action = 3   # 核心 → 观察，降级
        else:
            action = 2

        # 第三层：连续天数
        # 新进/升级/降级 → 天数少排前面（越新越要看）
        # 连续稳定 → 天数多排前面（持续性越强越靠前）
        days = _consecutive_days(code, pool_today)
        if action == 2:
            stability = -days    # 负值，天数越多值越小 → 排越前
        else:
            stability = days     # 正值，天数越少值越小 → 排越前

        return (group, action, stability, code)

    sorted_codes = sorted(all_codes, key=_sort_key)

    # 过滤：只在展示范围内出现过、且不在退潮池中
    sorted_codes = [
        c for c in sorted_codes
        if any(all_days_data.get(d, {}).get(c) for d in sorted_dates)
        and all_days_data.get(today, {}).get(c, "") != "退潮池"
    ]

    if not sorted_codes:
        return

    # 构建 HTML 行
    rows_html = []
    for code in sorted_codes:
        name = _name(code)
        cells = []
        for d in sorted_dates:
            pool = all_days_data.get(d, {}).get(code)
            if pool and pool in POOL_COLORS:
                c = POOL_COLORS[pool]
                cells.append(
                    f"<td style='background:{c['bg']};text-align:center;"
                    f"font-weight:bold;color:{c['text']};padding:4px 8px;font-size:12px'>"
                    f"{c['label']}</td>"
                )
            else:
                cells.append(
                    f"<td style='text-align:center;padding:4px 8px;color:#ccc;font-size:12px'>-</td>"
                )
        rows_html.append(
            f"<tr>"
            f"<td style='text-align:center;font-family:monospace;font-size:12px'>{code}</td>"
            f"<td style='text-align:left;padding-left:6px;font-size:12px'>{name}</td>"
            f"{''.join(cells)}"
            f"</tr>"
        )

    # 统计变化
    changed_count = 0
    if len(sorted_dates) >= 2:
        for code in sorted_codes:
            pools = []
            for d in sorted_dates:
                p = all_days_data.get(d, {}).get(code, "")
                if p:
                    pools.append(p)
            if len(set(pools)) >= 2:
                changed_count += 1

    # 构建表头
    date_headers = "".join(
        f"<th style='text-align:center;padding:4px 6px;font-size:11px;background:#f0f0f0'>{_fmt_date(d)}</th>"
        for d in sorted_dates
    )

    content = f"""---
tags: [股票跟踪, 池子流向]
updated: {today}
---

# 池子流向面板

> 更新于 {today} | 近{len(sorted_dates)}日 {len(sorted_codes)}只股票在三级池中的流动轨迹
> 发生池子变动的股票：{changed_count}只

**图例**：<span style='background:#FFE0E0;color:#C0392B;padding:2px 8px;border-radius:3px;font-weight:bold'>核心池</span> <span style='background:#FFF3CD;color:#B7950B;padding:2px 8px;border-radius:3px;font-weight:bold'>观察池</span> <span style='background:#D5F5E3;color:#1E8449;padding:2px 8px;border-radius:3px;font-weight:bold'>退潮池</span> <span style='color:#ccc'>- 未入池</span>

---

<table style='width:100%;border-collapse:collapse;font-size:12px'>
<thead>
<tr style='border-bottom:2px solid #ddd'>
<th style='text-align:center;padding:4px;background:#f8f8f8'>代码</th>
<th style='text-align:left;padding:4px;background:#f8f8f8'>名称</th>
{date_headers}
</tr>
</thead>
<tbody>
{''.join(rows_html)}
</tbody>
</table>

---

> **使用说明**：横向看一只股票的颜色变化，可快速发现池子升降级轨迹。红色→黄色=降级，黄色→绿色=退潮，绿色→黄色=回流。连续红色=核心标的持续强势。
"""

    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(FLOW_PANEL_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   📊 池子流向面板已生成: {FLOW_PANEL_FILE}")


# ============================================================
# 输出文件生成
# ============================================================

def _generate_dashboard(curated: dict, db: dict, pool_state: dict, prices: dict,
                        today: str, changes: list[dict]):
    """生成仪表盘 Markdown 文件（含变动追踪面板）"""
    market = curated.get("market_assessment", {})
    core = curated.get("core_pool", [])[:MAX_CORE]
    watch = curated.get("watch_pool", [])[:MAX_WATCH]

    tracking_count = sum(1 for s in db.get("stocks", {}).values() if s.get("status") == "跟踪中")

    # 退潮池数量（从快照或计算得出）
    yesterday_pools = _get_yesterday_pools(pool_state, today)

    # 计算总收益
    total_return = _calc_pool_total_return(curated, pool_state, prices)

    # 计算退潮池数量
    fading_codes = set()
    for snap in pool_state.get("_snapshots", {}).values():
        fading_codes |= set(snap.get("fading", []))
    # 如果今天刚生成，从昨天的退潮 + 今日降级的估算
    fading_count = len(fading_codes)  # 近似值

    lines = [
        "---",
        "tags: [股票跟踪, 仪表盘]",
        f"updated: {today}",
        "---",
        "",
        "# 股票跟踪仪表盘",
        "",
        f"> 自动更新于 {today} | 跟踪池 {tracking_count} 只 | 核心池 {len(core)} 只 | 观察池 {len(watch)} 只 | 退潮池 ~{fading_count} 只",
        f"> 核心池入池以来总收益: {_format_return(total_return)}",
        "",
        "---",
        "",
        "## 市场环境",
        "",
        "| 项目 | 现状 |",
        "|------|------|",
        f"| 指数趋势 | {market.get('index_trend', '-')} |",
        f"| 市场情绪 | {market.get('sentiment', '-')} |",
        f"| 量能 | {market.get('volume_assessment', '-')} |",
        f"| 主线板块 | {'、'.join(market.get('main_themes', ['-']))} |",
        "",
        f"> **今日策略**：{market.get('strategy', '-')}",
        "",
        "---",
        "",
    ]

    # ---- 今日变动面板 ----
    if changes:
        action_changes = [c for c in changes if c.get("change") not in ("stable",)]
        if action_changes:
            lines.extend([
                "## 📋 今日池子变动",
                "",
                "| 变动 | 代码 | 名称 | 说明 |",
                "|------|------|------|------|",
            ])
            for c in action_changes:
                lines.append(f"| {c['label']} | {c['code']} | {c['name']} | {c['detail']} |")
            lines.append("")

    lines.extend([
        "---",
        "",
        "## 核心池速览",
        "",
        f"> 共 {len(core)}/{MAX_CORE} 只 | 详细分析见 [[核心池|核心池.md]]",
        "",
    ])

    if core:
        lines.extend([
            "| # | 代码 | 名称 | 板块 | 趋势 | 入池日 | 入池价 | 现价 | 收益 | 置信度 |",
            "|:--:|------|------|------|:---:|--------|--------|------|------|:---:|",
        ])
        for i, s in enumerate(core, 1):
            conf = s.get("confidence", 3)
            stars = "★" * conf + "☆" * (5 - conf)
            entry = _get_entry_info(s["code"], pool_state, prices)
            lines.append(
                f"| {i} | {s['code']} | {s['name']} | {s.get('sector', '-')} "
                f"| {_trend_icon(s.get('trend', '-'))} "
                f"| {entry['entryDate']} | {entry['entryPrice'] or '--'} "
                f"| {entry['currentPrice'] or '--'} | {_format_return(entry['returnPct'])} "
                f"| {stars} |"
            )
        lines.append("")
    else:
        lines.extend(["> 暂无核心池标的", ""])

    lines.extend([
        "---",
        "",
        "## 观察池速览",
        "",
        f"> 共 {len(watch)}/{MAX_WATCH} 只 | 详细分析见 [[观察池|观察池.md]]",
        "",
    ])

    if watch:
        lines.extend([
            "| # | 代码 | 名称 | 板块 | 入池日 | 入池价 | 现价 | 收益 | 升级条件 |",
            "|:--:|------|------|------|--------|--------|------|------|---------|",
        ])
        for i, s in enumerate(watch, 1):
            entry = _get_entry_info(s["code"], pool_state, prices)
            lines.append(
                f"| {i} | {s['code']} | {s['name']} | {s.get('sector', '-')} "
                f"| {entry['entryDate']} | {entry['entryPrice'] or '--'} "
                f"| {entry['currentPrice'] or '--'} | {_format_return(entry['returnPct'])} "
                f"| {s.get('promotion_condition', '-')} |"
            )
        lines.append("")
    else:
        lines.extend(["> 暂无观察池标的", ""])

    # 退潮预警（保留：来自 tracking-db 的时间退潮统计）
    fading = _get_fading_stocks(db)
    if fading:
        lines.extend([
            "---",
            "",
            "## 退潮预警",
            "",
            "> 以下标的连续多日未出现，即将触发时间退潮。详见 [[退潮池|退潮池.md]]",
            "",
            "| 代码 | 名称 | 最后出现 | 剩余天数 |",
            "|------|------|---------|---------|",
        ])
        for s in fading[:15]:  # 只显示前15个
            lines.append(f"| {s['code']} | {s['name']} | {s['lastSeen']} | {s['remaining']}天 |")
        if len(fading) > 15:
            lines.append(f"| （已列15只，共{len(fading)}只） | | 详见退潮池 | |")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 快捷入口",
        "",
        "- [[核心池]] — 可交易标的深度分析（技术面+交易计划）",
        "- [[观察池]] — 蓄势标的跟踪",
        "- [[退潮池]] — 已退潮/降级标的归档",
        "",
        "---",
        "",
        "> **使用说明**：每日开盘前先看市场环境定策略基调 → 看今日变动了解池子变化 → 看核心池有无触发入场/止损条件 → 扫一眼观察池有无新满足条件的标的 → 退潮池确认已退出的不需要再关注。",
    ])

    content = "\n".join(lines)
    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   📊 仪表盘已生成: {DASHBOARD_FILE}")


def _generate_fading_pool_file(fading_classified: dict, yesterday_pools: dict,
                                pool_state: dict, prices: dict, today: str):
    """生成退潮池.md"""
    sections = []

    total_count = sum(len(v) for v in fading_classified.values())

    sections.extend([
        "---",
        f"tags: [股票跟踪, 退潮池]",
        f"updated: {today}",
        "---",
        "",
        "# 退潮池",
        "",
        f"> 更新于 {today} | 共 {total_count} 只 | 按退潮原因分类",
        "",
        "> **说明**：退潮池中的标的不作为交易候选，仅供回顾参考。若后续重新出现信号，可能回流至观察池。",
        "",
        "---",
        "",
    ])

    # 分类1: 自然降级（观察池/核心池降下来的）
    natural = fading_classified.get("自然降级", [])
    if natural:
        sections.extend([
            "## 自然降级",
            "",
            f"> 共 {len(natural)} 只 — 昨日在核心池或观察池中，今日被更好标的挤出或判断变差",
            "",
            "| 代码 | 名称 | 降级原因 | 最后出现 |",
            "|------|------|---------|---------|",
        ])
        natural.sort(key=lambda x: x["code"])
        for s in natural:
            entry_info = _get_entry_info(s["code"], pool_state, prices)
            entry_price_str = f"入池价{entry_info['entryPrice']}元" if entry_info.get("entryPrice") else ""
            sections.append(
                f"| {s['code']} | {s['name']} | {s['detail']}{' (' + entry_price_str + ')' if entry_price_str else ''} | {s['lastSeen']} |"
            )
        sections.append("")

    # 分类2: 时间退潮（长期未出现）
    time_fading = fading_classified.get("时间退潮", [])
    if time_fading:
        sections.extend([
            "## 时间退潮",
            "",
            f"> 共 {len(time_fading)} 只 — 连续多日未在分析报告中出现，逐渐失去关注价值",
            "",
            "| 代码 | 名称 | 状态 | 最后出现 |",
            "|------|------|------|---------|",
        ])
        time_fading.sort(key=lambda x: x.get("remaining", 0))
        for s in time_fading:
            sections.append(
                f"| {s['code']} | {s['name']} | {s['detail']} | {s['lastSeen']} |"
            )
        sections.append("")

    # 分类3: 持续观察（已在退潮池中的）
    ongoing = fading_classified.get("持续观察", [])
    if ongoing:
        sections.extend([
            "## 持续观察",
            "",
            f"> 共 {len(ongoing)} 只 — 已在退潮池中，继续观察是否出现回流信号",
            "",
            "| 代码 | 名称 | 状态 | 最后出现 |",
            "|------|------|------|---------|",
        ])
        ongoing.sort(key=lambda x: x["code"])
        for s in ongoing:
            sections.append(
                f"| {s['code']} | {s['name']} | {s['detail']} | {s['lastSeen']} |"
            )
        sections.append("")

    sections.extend([
        "---",
        "",
        "> **回流条件**：退潮池中的股票若在后续分析报告中重新被提及（放量、涨停、板块回暖等信号），会在当日自动回流至观察池，并在仪表盘「今日变动」中显示 🔄 回流复活。",
    ])

    content = "\n".join(sections)
    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(FADING_POOL_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   📝 退潮池已生成: {FADING_POOL_FILE}")


def _calc_pool_total_return(curated: dict, pool_state: dict, prices: dict) -> float:
    """计算核心池所有标的入池以来的平均收益"""
    core = curated.get("core_pool", [])[:MAX_CORE]
    returns = []
    for s in core:
        entry = _get_entry_info(s["code"], pool_state, prices)
        if entry["returnPct"] is not None:
            returns.append(entry["returnPct"])
    if returns:
        return round(sum(returns) / len(returns), 2)
    return None


def _generate_pool_file(stocks: list, pool_type: str, market: dict, pool_state: dict,
                        prices: dict, today: str):
    """生成核心池.md 或 观察池.md"""
    is_core = pool_type == "core"
    label = "核心池" if is_core else "观察池"
    max_count = MAX_CORE if is_core else MAX_WATCH
    filepath = CORE_POOL_FILE if is_core else WATCH_POOL_FILE

    lines = [
        "---",
        f"tags: [股票跟踪, {label}]",
        f"updated: {today}",
        "---",
        "",
        f"# {label}",
        "",
        f"> 更新于 {today} | 共 {len(stocks)}/{max_count} 只 | 按推荐程度从上到下排列",
        "",
        "---",
        "",
        "## 市场环境",
        "",
        f"- **指数趋势**：{market.get('index_trend', '-')}",
        f"- **市场情绪**：{market.get('sentiment', '-')}",
        f"- **主线板块**：{'、'.join(market.get('main_themes', ['-']))}",
        f"- **策略基调**：{market.get('strategy', '-')}",
        "",
        "---",
        "",
    ]

    for i, s in enumerate(stocks, 1):
        conf = s.get("confidence", "-")
        if isinstance(conf, int):
            stars = "★" * conf + "☆" * (5 - conf)
        else:
            stars = "-"

        tech = s.get("technical", {})
        tp = s.get("trade_plan", {})
        entry = _get_entry_info(s["code"], pool_state, prices)

        # 基本信息（含入池跟踪）
        lines.extend([
            f"## {i}. {s['code']} {s['name']} {stars}",
            "",
            "### 基本信息",
            "",
            "| 项目 | 内容 |",
            "|------|------|",
            f"| 代码 | {s['code']} |",
            f"| 名称 | {s['name']} |",
            f"| 板块/题材 | {s.get('sector', '-')} |",
            f"| 趋势方向 | {s.get('trend', '-')} |",
            f"| 置信度 | {stars} |",
            "",
            "### 入池跟踪",
            "",
            "| 项目 | 内容 |",
            "|------|------|",
            f"| 入池日期 | {entry['entryDate']} |",
            f"| 入池价格 | {entry['entryPrice'] or '--'} 元 |",
            f"| 最新价格 | {entry['currentPrice'] or '--'} 元 |",
            f"| 入池天数 | {entry['daysInPool']} 天 |",
            f"| 入池收益 | {_format_return(entry['returnPct'])} |",
            "",
            "### AI 关注逻辑",
            "",
            f"> {s.get('reason', '-')}",
            "",
            "### 技术画像",
            "",
            "| 项目 | 分析 |",
            "|------|------|",
            f"| 支撑位 | {tech.get('support', '-')} |",
            f"| 阻力位 | {tech.get('resistance', '-')} |",
            f"| 均线状态 | {tech.get('ma_status', '-')} |",
            f"| 量能特征 | {tech.get('volume_profile', '-')} |",
            "",
        ])

        if is_core and tp:
            lines.extend([
                "### 交易计划",
                "",
                "| 项目 | 内容 |",
                "|------|------|",
                f"| 入场区间 | {tp.get('entry_zone', '-')} |",
                f"| 止损位 | {tp.get('stop_loss', '-')} |",
                f"| 第一目标 | {tp.get('target_1', '-')} |",
                f"| 第二目标 | {tp.get('target_2', '--')} |",
                f"| 仓位建议 | {tp.get('position_pct', '-')} |",
                f"| 触发条件 | {tp.get('trigger', '-')} |",
                "",
            ])
        elif not is_core:
            # 观察池：显示降级原因（如果有）
            downgrade = s.get("downgrade_reason")
            if downgrade:
                lines.extend([
                    "### ⚠️ 降级说明",
                    "",
                    f"> 从核心池降级原因：{downgrade}",
                    "",
                ])
            lines.extend([
                "### 升级条件",
                "",
                f"> {s.get('promotion_condition', '-')}",
                "",
            ])

        lines.append("---")
        lines.append("")

    content = "\n".join(lines)

    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   📝 {label}已生成: {filepath}")


def _get_fading_stocks(db: dict) -> list:
    """获取即将退潮的股票（3天以上未出现）"""
    today = datetime.now()
    fading = []
    for code, s in db.get("stocks", {}).items():
        if s.get("status") != "跟踪中":
            continue
        try:
            last_seen = datetime.strptime(s["lastSeen"], "%Y-%m-%d")
            days_since = (today - last_seen).days
            if days_since >= 3:
                fading.append({
                    "code": code,
                    "name": s["name"],
                    "lastSeen": s["lastSeen"],
                    "remaining": max(0, STALE_DAYS - days_since),
                })
        except (ValueError, KeyError):
            pass
    fading.sort(key=lambda x: x["remaining"])
    return fading


def _trend_icon(trend: str) -> str:
    t = trend.strip()
    if "上升" in t or "上涨" in t:
        return "↑"
    elif "下降" in t or "下跌" in t:
        return "↓"
    else:
        return "→"


# ============================================================
# 主入口
# ============================================================

def curate_tracking() -> dict:
    """主入口：执行 AI 智能筛选并生成所有输出文件

    新流程：
    1. 加载跟踪数据库 + 最新分析文档 + 价格
    2. 读取昨日池子快照（用于对比和稳定机制）
    3. AI 筛选（含稳定上下文）
    4. 升降级对比 → 计算变动
    5. 退潮池分类（自然降级/时间退潮/持续观察）+ 回流处理
    6. 保存今日快照
    7. 生成输出文件（仪表盘/核心池/观察池/退潮池）
    """
    today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.now()
    print(f"\n🧠 开始 AI 智能筛选 ({today})")

    # 1. 加载跟踪数据库
    db = _load_db()
    tracking_count = sum(1 for s in db.get("stocks", {}).values() if s.get("status") == "跟踪中")
    print(f"   📂 跟踪池: {tracking_count} 只跟踪中")

    # 2. 找到最近的 AI 分析文档
    docs = _find_latest_analysis()
    if not docs:
        print("   ⚠️ 未找到 AI 分析文档，跳过筛选")
        return {}

    doc_date = docs.get("_date", "未知")
    print(f"   📅 分析文档日期: {doc_date}")

    # 3. 获取最新价格
    prices = _get_current_prices()

    # 4. 读取昨日池子快照（用于稳定机制和对比）
    pool_state = _load_pool_state()
    _backfill_snapshots_if_needed(pool_state)  # 首次运行从旧记录回填历史快照
    pool_state = _load_pool_state()  # 重新加载回填后的数据
    yesterday_pools = _get_yesterday_pools(pool_state, today)

    # 5. 构建筛选 prompt（含稳定上下文，不含 suggested_drops）
    prompt = _build_curation_prompt(db, docs, prices, today, yesterday_pools, pool_state)
    print(f"   📝 Prompt 大小: {len(prompt)} 字符")

    # 6. 调用 DeepSeek API
    response = analyze(prompt, "AI股票智能筛选", max_retries=2)

    # 7. 解析 AI 返回的 JSON（不再包含 suggested_drops）
    curated = _parse_response(response)
    if not curated:
        print("   ❌ AI 筛选失败，返回空结果")
        return {}

    core = curated.get("core_pool", [])[:MAX_CORE]
    watch = curated.get("watch_pool", [])[:MAX_WATCH]

    # 8. 稳定机制：昨日核心池中未被选入任何池的，强制保留到观察池
    core_codes = {s["code"] for s in core}
    watch_codes = {s["code"] for s in watch}
    yesterday_core = yesterday_pools.get("core", set())
    yesterday_watch = yesterday_pools.get("watch", set())

    # 检查昨日核心池中消失的标的
    missing_core = yesterday_core - core_codes - watch_codes
    stability_added = []
    for code in missing_core:
        stock = db.get("stocks", {}).get(code)
        if stock and stock.get("status") == "跟踪中":
            name = stock.get("name", code)
            # 检查是否已退潮（tracking-db 状态）
            try:
                last_seen = datetime.strptime(stock.get("lastSeen", ""), "%Y-%m-%d")
                days_since = (today_dt - last_seen).days
            except (ValueError, KeyError):
                days_since = 0

            # 如果3天内还出现过，给予稳定性保护（强制保留到观察池）
            if days_since < STALE_DAYS and len(watch) < MAX_WATCH:
                watch.append({
                    "code": code,
                    "name": name,
                    "sector": "-",
                    "reason": f"昨日核心池标的，今日AI未选出，稳定性保护保留至观察池（已跟踪{days_since + 1}天）",
                    "trend": "震荡",
                    "downgrade_reason": "AI今日未主动选出，可能因当日信号减弱，给予一天观察期",
                    "technical": {
                        "support": "待评估",
                        "resistance": "待评估",
                        "ma_status": "待评估",
                        "volume_profile": "待评估",
                    },
                    "promotion_condition": "明日若重新放量或出现板块效应，可重新升级核心池",
                })
                watch_codes.add(code)
                stability_added.append(f"{code} {name}")
    if stability_added:
        print(f"   🛡️  稳定机制保护: {len(stability_added)} 只 ({', '.join(stability_added)})")

    # 9. 构建退潮池
    # 9a. 从 tracking-db 获取时间退潮的
    tracking_fading = _get_tracking_db_fading_stocks(db)

    # 9b. 昨日观察池中未被选入任何池的 → 退潮
    downgrade_fading = yesterday_watch - core_codes - watch_codes

    # 9c. 昨日退潮池中，今天未被复活的 → 继续留在退潮池
    yesterday_fading = yesterday_pools.get("fading", set())
    revived_fading = yesterday_fading & (core_codes | watch_codes)  # 回流的
    continued_fading = yesterday_fading - core_codes - watch_codes  # 继续退潮的

    if revived_fading:
        print(f"   🔄 退潮池回流: {len(revived_fading)} 只")

    # 合并退潮池
    fading_codes = tracking_fading | downgrade_fading | continued_fading

    # 9d. 昨日核心池中完全消失的（被稳定机制兜底后仍在核心池和观察池之外的）
    # 这些应该已经被稳定机制处理了，不应再出现在这里

    print(f"   ✅ 筛选完成: 核心池 {len(core)}/{MAX_CORE} 只 | 观察池 {len(watch)}/{MAX_WATCH} 只 | 退潮池 {len(fading_codes)} 只")

    # 10. 计算变动
    changes = _compute_changes(yesterday_pools, core_codes, watch_codes, fading_codes, core, watch)

    # 打印变动摘要
    action_changes = [c for c in changes if c.get("change") not in ("stable",)]
    for c in action_changes:
        if c.get("code"):  # 有具体代码的
            print(f"   {c['label']}: {c['code']} {c['name']}")

    # 11. 分类退潮池
    fading_classified = _classify_fading_stocks(fading_codes, yesterday_pools, db)

    # 12. 更新池状态（记录入池日期/价格）
    _update_pool_state(core_codes, watch_codes, prices, today)
    pool_state = _load_pool_state()  # 重新加载以获取更新后的状态

    # 13. 保存今日快照
    _save_snapshot(pool_state, today, core_codes, watch_codes, fading_codes)

    # 14. 生成仪表盘（含变动面板）
    _generate_dashboard(curated, db, pool_state, prices, today, changes)

    # 15. 生成核心池.md
    market = curated.get("market_assessment", {})
    _generate_pool_file(core, "core", market, pool_state, prices, today)

    # 16. 生成观察池.md
    _generate_pool_file(watch, "watch", market, pool_state, prices, today)

    # 17. 生成退潮池.md
    _generate_fading_pool_file(fading_classified, yesterday_pools, pool_state, prices, today)

    # 18. 生成池子流向面板（独立文件）
    _generate_flow_panel(pool_state, today, core_codes, watch_codes, fading_codes)

    return curated


if __name__ == "__main__":
    result = curate_tracking()
    if result:
        core = result.get("core_pool", [])
        watch = result.get("watch_pool", [])
        print(f"\n🏆 核心池标的:")
        for i, s in enumerate(core, 1):
            print(f"   {i}. {s['code']} {s['name']} | {s.get('sector', '-')} | ★{s.get('confidence', '-')}")
        print(f"\n🔍 观察池标的:")
        for i, s in enumerate(watch, 1):
            print(f"   {i}. {s['code']} {s['name']} | {s.get('sector', '-')}")
    else:
        print("\n❌ 筛选未产生结果")
