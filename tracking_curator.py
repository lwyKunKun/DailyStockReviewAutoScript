"""
AI 驱动的股票智能筛选与仪表盘生成模块

在 stock_tracker.py 更新 tracking-db.json 后运行，
读取当日的 AI 分析文档 + 跟踪数据库，通过 DeepSeek 智能筛选，
自动生成 仪表盘.md、核心池.md、观察池.md。

核心改进：
1. 用 AI 定性判断替代死板规则分组
2. 核心池/观察池各一个文件，按推荐程度从上到下排列
3. 技术画像和交易计划由 AI 填写
4. 跟踪入池日期、入池价格、入池后收益
"""

import json
import os
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from ai_analyzer import analyze

OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_TRACKING_DIR = os.getenv("OBSIDIAN_TRACKING_DIR", "01-Projects/股票跟踪")
OBSIDIAN_REVIEW_DIR = os.getenv("OBSIDIAN_REVIEW_DIR", "01-Projects/股票复盘")

TRACKING_DIR = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_TRACKING_DIR)
TRACKING_DB_FILE = os.path.join(TRACKING_DIR, "tracking-db.json")
DASHBOARD_FILE = os.path.join(TRACKING_DIR, "仪表盘.md")
CORE_POOL_FILE = os.path.join(TRACKING_DIR, "核心池.md")
WATCH_POOL_FILE = os.path.join(TRACKING_DIR, "观察池.md")

POOL_STATE_FILE = os.path.join(TRACKING_DIR, "pool_state.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "latest_data.json")

MAX_CORE = 10
MAX_WATCH = 15


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
    for doc_name in ["放量筛选", "涨停跌停潮", "复盘"]:
        text = docs.get(doc_name, "")
        # 匹配表格行中的价格信息：| 代码 | 名称 | 涨跌幅 | ... 格式
        # 无法直接获取绝对价格，只能获取涨跌幅
        pass
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


def _update_pool_state(curated: dict, prices: dict, today: str):
    """更新池状态：记录新入池股票的入池日期和入池价格

    存储在独立的 pool_state.json 中，不污染 tracking-db.json。
    格式: {"002829": [{"pool": "核心池", "entryDate": "...", "entryPrice": 19.5, "exitDate": null}, ...]}
    """
    core_codes = {s["code"] for s in curated.get("core_pool", [])}
    watch_codes = {s["code"] for s in curated.get("watch_pool", [])}
    all_active = core_codes | watch_codes

    state = _load_pool_state()
    new_entries = 0

    for code in all_active:
        pool = "核心池" if code in core_codes else "观察池"
        history = state.get(code, [])

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
        if code not in all_active:
            for h in state[code]:
                if h.get("exitDate") is None:
                    h["exitDate"] = today

    if new_entries > 0:
        _save_pool_state(state)
        print(f"   🆕 新入池: {new_entries} 只")

    return prices


def _get_entry_info(stock_code: str, pool_state: dict, prices: dict) -> dict:
    """获取某只股票的入池信息，包含入池日期、入池价格、当前价格、收益%"""
    history = pool_state.get(stock_code, [])

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

    # 提取所有6位股票代码
    codes_seen = set()
    # 格式1: 表格行 | 000977 | 浪潮信息 |
    for m in re.finditer(r"\|\s*(\d{6})\s*\|\s*([\u4e00-\u9fa5]{2,8})\s*\|", all_text):
        codes_seen.add((m.group(1), m.group(2)))
    # 格式2: 名称(代码) 或 名称（代码）
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,8})\s*[（(]\s*(\d{6})\s*[）)]", all_text):
        codes_seen.add((m.group(2), m.group(1)))

    if not codes_seen:
        return ""

    lines = [
        f"分析文档中出现的 {len(codes_seen)} 只股票的价格参考：",
        "",
        "| 代码 | 名称 | 现价 |",
        "|------|------|------|",
    ]
    for code, name in sorted(codes_seen):
        price = prices.get(code)
        price_str = str(price) if price else "--"
        lines.append(f"| {code} | {name} | {price_str} |")

    return "\n".join(lines)


def _build_curation_prompt(db: dict, docs: dict, prices: dict, today: str) -> str:
    """构建发给 DeepSeek 的筛选 prompt（含真实价格数据）"""
    market_summary = _extract_market_summary(docs)
    stock_table = _build_stock_table(db, prices)

    volume_doc = docs.get("放量筛选", "")
    limit_doc = docs.get("涨停跌停潮", "")
    news_doc = docs.get("消息汇总", "")

    tracking_count = len([s for s in db.get("stocks", {}).values() if s.get("status") == "跟踪中"])

    # 构建分析文档中出现过的所有股票的价格参考表
    price_ref = _build_price_reference(docs, prices)

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

## 当前跟踪池（{tracking_count}只）

{stock_table}

## 全量价格参考（分析文档中出现的所有股票）

{price_ref}

---

## 你的任务

请基于以上全部信息，完成筛选并给出详细分析。**关键要求：读懂分析报告中的具体量价数据、封板质量、板块效应来做判断，而不是机械数信号数量。**

### ⚠️ 技术面分析铁律

1. **支撑位必须低于现价**，阻力位必须高于现价。如果现价15元，支撑位不能是25元。
2. 支撑位看下方：近期低点、均线、整数关口、涨停板开盘价
3. 阻力位看上方：前期高点、密集成交区、整数关口
4. 价格数字必须与现价有合理关系，不要凭空捏造

### 筛选标准

**核心池（最多{MAX_CORE}只）—— 近期可能交易的标的：**
- 属于当前市场主线板块
- 多信号共振且有清晰的上涨逻辑
- 持续活跃（跟踪≥2天）
- 有板块效应（同板块多只标的共振）
- 排除：纯消息驱动无技术面支撑、退市股、单日游资炒作

**观察池（最多{MAX_WATCH}只）—— 有潜力但条件未完全满足：**
- 信号共振但跟踪天数不够，或板块方向待确认
- 等待关键位置突破或回踩确认

**建议剔除：**
- 信号来源单一且无板块支撑
- 已多日未出现（即将退潮）
- 纯粹放量无逻辑支撑的权重股
- 业绩暴雷、退市风险标的

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
      "reason": "入选核心池的理由（基于分析报告中的具体数据）",
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
      "reason": "关注逻辑（基于分析报告中的具体数据）",
      "trend": "上升/下降/震荡",
      "technical": {{
        "support": "支撑位",
        "resistance": "阻力位",
        "ma_status": "均线状态",
        "volume_profile": "量能特征"
      }},
      "promotion_condition": "升级到核心池需要满足什么条件"
    }}
  ],
  "suggested_drops": [
    {{
      "code": "股票代码",
      "name": "股票名称",
      "reason": "剔除理由"
    }}
  ]
}}
```

### 重要约束
1. 核心池最多{MAX_CORE}只，观察池最多{MAX_WATCH}只
2. core_pool 和 watch_pool 都按推荐程度从高到低排序，最重要的排在最前面
3. confidence 取值 1-5，5表示最高把握
4. **所有价格必须以表格中的"现价"为基准，支撑位<现价<阻力位。如果某股票在两张表中都有出现，以前者为准。找不到现价的股票，从分析报告中的涨跌幅倒推估算。**
5. 不要捏造股票代码或名称，只从跟踪池表格中选择
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


def _generate_dashboard(curated: dict, db: dict, pool_state: dict, prices: dict, today: str):
    """生成仪表盘 Markdown 文件"""
    market = curated.get("market_assessment", {})
    core = curated.get("core_pool", [])[:MAX_CORE]
    watch = curated.get("watch_pool", [])[:MAX_WATCH]
    drops = curated.get("suggested_drops", [])

    tracking_count = sum(1 for s in db.get("stocks", {}).values() if s.get("status") == "跟踪中")
    ebbed_count = sum(1 for s in db.get("stocks", {}).values() if s.get("status") == "已退潮")

    # 计算总收益
    total_return = _calc_pool_total_return(curated, db, prices)

    lines = [
        "---",
        "tags: [股票跟踪, 仪表盘]",
        f"updated: {today}",
        "---",
        "",
        "# 股票跟踪仪表盘",
        "",
        f"> 自动更新于 {today} | 跟踪池 {tracking_count} 只 | 核心池 {len(core)} 只 | 观察池 {len(watch)} 只 | 已退潮 {ebbed_count} 只",
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
        "## 核心池速览",
        "",
        f"> 共 {len(core)} 只 | 详细分析见 [[核心池|核心池.md]]",
        "",
    ]

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
        f"> 共 {len(watch)} 只 | 详细分析见 [[观察池|观察池.md]]",
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

    # 建议剔除
    if drops:
        lines.extend([
            "---",
            "",
            "## 建议剔除",
            "",
            "| 代码 | 名称 | 剔除理由 |",
            "|------|------|---------|",
        ])
        for s in drops:
            lines.append(f"| {s['code']} | {s['name']} | {s.get('reason', '-')} |")
        lines.append("")

    # 退潮预警
    fading = _get_fading_stocks(db)
    if fading:
        lines.extend([
            "---",
            "",
            "## 退潮预警",
            "",
            "| 代码 | 名称 | 最后出现 | 剩余天数 |",
            "|------|------|---------|---------|",
        ])
        for s in fading:
            lines.append(f"| {s['code']} | {s['name']} | {s['lastSeen']} | {s['remaining']}天 |")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 快捷入口",
        "",
        "- [[核心池]] — 可交易标的深度分析（技术面+交易计划）",
        "- [[观察池]] — 蓄势标的跟踪",
        "- [[信号日志/|信号日志]] — 每日信号原始记录",
        "- [[交易日志/|交易日志]] — 每笔交易记录",
        "- [[历史归档/|历史归档]] — 已退出标的",
        "",
        "---",
        "",
        "> **使用说明**：每日开盘前先看市场环境定策略基调，再看核心池有无触发入场/止损条件，最后扫一眼观察池有无新满足条件的标的。",
    ])

    content = "\n".join(lines)
    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"   📊 仪表盘已生成: {DASHBOARD_FILE}")


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


def _generate_pool_file(stocks: list, pool_type: str, market: dict, pool_state: dict, prices: dict, today: str):
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
                    "remaining": max(0, 5 - days_since),
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


def curate_tracking() -> dict:
    """主入口：执行 AI 智能筛选并生成所有输出文件"""
    today = datetime.now().strftime("%Y-%m-%d")
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

    # 3. 获取最新价格（用于注入prompt + 入池跟踪）
    prices = _get_current_prices()

    # 4. 构建筛选 prompt（含真实价格）
    prompt = _build_curation_prompt(db, docs, prices, today)
    print(f"   📝 Prompt 大小: {len(prompt)} 字符")

    # 4. 调用 DeepSeek API
    response = analyze(prompt, "AI股票智能筛选", max_retries=2)

    # 5. 解析 AI 返回的 JSON
    curated = _parse_response(response)
    if not curated:
        print("   ❌ AI 筛选失败，返回空结果")
        return {}

    core = curated.get("core_pool", [])[:MAX_CORE]
    watch = curated.get("watch_pool", [])[:MAX_WATCH]
    drops = curated.get("suggested_drops", [])
    market = curated.get("market_assessment", {})

    print(f"   ✅ 筛选完成: 核心池 {len(core)} 只 | 观察池 {len(watch)} 只 | 建议剔除 {len(drops)} 只")

    # 5. 更新池状态（记录入池日期/价格）
    _update_pool_state(curated, prices, today)
    pool_state = _load_pool_state()

    # 6. 生成仪表盘（速览，含入池收益）
    _generate_dashboard(curated, db, pool_state, prices, today)

    # 7. 生成核心池.md（含入池跟踪+技术面+交易计划）
    _generate_pool_file(core, "core", market, pool_state, prices, today)

    # 8. 生成观察池.md
    _generate_pool_file(watch, "watch", market, pool_state, prices, today)

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
