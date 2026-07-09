"""
股票跟踪库管理模块 - 优先级分层 + Canvas 可视化看板
输出: 01-Projects/股票跟踪/股票跟踪总表.md (分层 Markdown)
     01-Projects/股票跟踪/股票跟踪看板.canvas (可视化看板)
同时维护 tracking-db.json 作为数据库
"""

import json
import os
import re
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_TRACKING_DIR = os.getenv("OBSIDIAN_TRACKING_DIR", "01-Projects/股票跟踪")

TRACKING_DIR = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_TRACKING_DIR)
TRACKING_DB_FILE = os.path.join(TRACKING_DIR, "tracking-db.json")
TRACKING_TABLE_FILE = os.path.join(TRACKING_DIR, "股票跟踪总表.md")
TRACKING_CANVAS_FILE = os.path.join(TRACKING_DIR, "股票跟踪看板.canvas")

# 层级颜色映射（Canvas 预设色号 1-6 + 备用 hex）
TIER_COLORS = {
    "明日重点": "1",     # 红色
    "放量异动": "2",     # 橙色
    "连板梯队": "3",     # 黄色
    "逆势个股": "4",     # 绿色
    "其他关注": "5",     # 青色
    "观察池": "6",       # 紫色
}

# 来源名→分类映射
SOURCE_TO_CATEGORY = {
    "放量筛选": "放量异动",
    "涨停跌停潮分析": "连板梯队",
    "每日复盘": "其他关注",       # 通用来源，无法判断子分类时归入其他
    "逆势个股筛选": "逆势个股",
    "全球市场消息汇总": "其他关注",
}


def _hash_id(s: str) -> str:
    """生成 16 位 hex Canvas node ID"""
    return hashlib.md5(s.encode()).hexdigest()[:16]


def _load_db() -> dict:
    """加载跟踪数据库"""
    os.makedirs(TRACKING_DIR, exist_ok=True)
    if os.path.exists(TRACKING_DB_FILE):
        with open(TRACKING_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 2, "lastUpdated": "", "stocks": {}}


def _save_db(db: dict):
    """保存跟踪数据库"""
    db["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(TRACKING_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def extract_stock_codes(texts: list[str]) -> list[dict]:
    """从 AI 生成的文本中提取股票代码和名称"""
    stocks = []
    seen = set()

    # 格式1: 名称（代码）或 名称(代码) — 常见于分析文本中
    pattern_text = re.compile(r"([\u4e00-\u9fa5]{2,6})\s*[（(]?\s*(\d{6})\s*[）)]?")
    # 格式2: | 000977 | 浪潮信息 | 或 | 1 | 000977 | 浪潮信息 | — 常见于表格行
    pattern_table = re.compile(r"\|\s*(?:\d+\s*\|)?\s*(\d{6})\s*\|\s*([\u4e00-\u9fa5]{2,6})\s*\|")

    for text in texts:
        # 先匹配文本格式
        for name, code in pattern_text.findall(text):
            if code not in seen:
                seen.add(code)
                stocks.append({"代码": code, "名称": name})
        # 再匹配表格格式
        for code, name in pattern_table.findall(text):
            if code not in seen:
                seen.add(code)
                stocks.append({"代码": code, "名称": name})

    return stocks


def _classify_stock(stock: dict) -> str:
    """根据 history 中的 sources 判定股票所属层级

    优先级：
    1. 多源共振（≥2 个不同来源）或连板 ≥2 → 明日重点
    2. 单一来源 → 按来源映射分类
    3. 其他 → 其他关注
    """
    sources = set()
    for h in stock.get("history", []):
        src = h.get("source", "")
        if src:
            sources.add(src)

    boards = stock.get("maxBoard", 0)

    # 高共振：多来源 或 连板 ≥ 2
    if len(sources) >= 2 or boards >= 2:
        return "明日重点"

    # 单来源：按映射分类
    for src in sources:
        cat = SOURCE_TO_CATEGORY.get(src)
        if cat:
            return cat

    return "其他关注"


def _get_tier(stock: dict, today: datetime) -> str:
    """获取股票的展示层级（含观察池判定）"""
    # 先检查是否该进观察池
    try:
        last_seen = datetime.strptime(stock["lastSeen"], "%Y-%m-%d")
        days_since = (today - last_seen).days
        if days_since >= 3:
            return "观察池"
    except (ValueError, KeyError):
        pass

    # 正常分类
    return _classify_stock(stock)


def update_tracking(source_texts: list[tuple[str, str]]):
    """根据 AI 生成的内容更新股票跟踪库

    Args:
        source_texts: [(来源名, AI文本), ...]
                      来源名如 '放量筛选' '涨停跌停潮分析' '每日复盘' 等
    """
    today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.now()
    db = _load_db()

    total_found = 0
    updated_count = 0
    new_count = 0

    for source_name, text in source_texts:
        stocks_found = extract_stock_codes([text])

        for stock in stocks_found:
            code = stock["代码"]
            name = stock["名称"]
            total_found += 1

            if code in db["stocks"]:
                entry = db["stocks"][code]
                entry["name"] = name
                entry["lastSeen"] = today
                # 检查是否已有此来源的今日记录，避免重复
                already_today = any(
                    h.get("date") == today and h.get("source") == source_name
                    for h in entry["history"]
                )
                if not already_today:
                    entry["history"].append({
                        "date": today,
                        "event": f"再次出现({source_name})",
                        "source": source_name,
                    })
                    updated_count += 1
            else:
                db["stocks"][code] = {
                    "name": name,
                    "code": code,
                    "firstSeen": today,
                    "lastSeen": today,
                    "status": "跟踪中",
                    "maxBoard": 0,
                    "tags": [],
                    "history": [{
                        "date": today,
                        "event": f"首次入库({source_name})",
                        "source": source_name,
                    }],
                }
                new_count += 1

    print(f"🔍 从 {len(source_texts)} 个来源中提取到 {total_found} 只股票（去重后）")

    # 检查退潮条件：连续 5 天未出现
    five_days_ago = today_dt - timedelta(days=5)
    for code, entry in db["stocks"].items():
        if entry["status"] == "跟踪中":
            last_seen = datetime.strptime(entry["lastSeen"], "%Y-%m-%d")
            if last_seen < five_days_ago:
                entry["status"] = "已退潮"
                entry["history"].append({
                    "date": today, "event": "自动退潮(5日未出现)", "source": "系统",
                })

    _save_db(db)

    # 生成分层 Markdown 表 + Canvas 看板
    _generate_table(db, today)
    _generate_canvas(db, today)

    print(f"   ✅ 新增 {new_count} 只，更新 {updated_count} 只")
    return db


def _generate_table(db: dict, date_str: str):
    """生成分层层级 Markdown 表格"""
    stocks = db.get("stocks", {})
    if not stocks:
        return

    today = datetime.now()

    # 按层级分组
    tier_groups = {
        "明日重点": [],
        "放量异动": [],
        "连板梯队": [],
        "逆势个股": [],
        "其他关注": [],
        "观察池": [],
    }
    ebbed = []

    for s in stocks.values():
        if s["status"] == "已退潮":
            ebbed.append(s)
        else:
            tier = _get_tier(s, today)
            tier_groups.setdefault(tier, []).append(s)

    # 每组内按代码排序
    for k in tier_groups:
        tier_groups[k].sort(key=lambda s: s["code"])

    # 统计
    tracking_count = sum(1 for s in stocks.values() if s["status"] == "跟踪中")
    ebbed_count = len(ebbed)

    content = f"""---
tags: [股票跟踪]
updated: {date_str}
---
> Canvas 看板: [[股票跟踪看板.canvas|打开可视化看板]]

# 股票跟踪总表

> 自动更新于 {date_str} | 总计 {len(stocks)} 只 | 跟踪中 {tracking_count} 只 | 已退潮 {ebbed_count} 只

"""

    # ---- 明日重点 ----
    key_stocks = tier_groups.get("明日重点", [])
    if key_stocks:
        content += "## 明日重点（高共振标的）\n\n"
        content += "| 代码 | 名称 | 共振来源 | 跟踪天数 | 最高连板 | 最新出现 |\n"
        content += "|------|------|---------|---------|---------|--------|\n"
        for s in key_stocks:
            sources = list(set(h.get("source", "") for h in s.get("history", [])))
            sources_str = "+".join(sources) if sources else "-"
            days = _count_tracking_days(s)
            content += (
                f"| {s['code']} | {s['name']} | {sources_str} | "
                f"{days}天 | {s.get('maxBoard', 0)}板 | {s['lastSeen']} |\n"
            )
        content += "\n"

    # ---- 常规跟踪 ----
    content += "## 常规跟踪\n\n"

    # 放量异动
    vol_stocks = tier_groups.get("放量异动", [])
    if vol_stocks:
        content += f"### 放量异动（{len(vol_stocks)}只）\n\n"
        content += "| 代码 | 名称 | 跟踪天数 | 最近出现 | 来源 |\n"
        content += "|------|------|---------|---------|------|\n"
        for s in vol_stocks:
            days = _count_tracking_days(s)
            src = _primary_source(s)
            content += f"| {s['code']} | {s['name']} | {days}天 | {s['lastSeen']} | {src} |\n"
        content += "\n"

    # 连板梯队
    board_stocks = tier_groups.get("连板梯队", [])
    if board_stocks:
        content += f"### 连板梯队（{len(board_stocks)}只）\n\n"
        content += "| 代码 | 名称 | 最高连板 | 跟踪天数 | 最近出现 |\n"
        content += "|------|------|---------|---------|--------|\n"
        for s in board_stocks:
            days = _count_tracking_days(s)
            content += f"| {s['code']} | {s['name']} | {s.get('maxBoard', 0)}板 | {days}天 | {s['lastSeen']} |\n"
        content += "\n"

    # 逆势个股
    reverse_stocks = tier_groups.get("逆势个股", [])
    if reverse_stocks:
        content += f"### 逆势个股（{len(reverse_stocks)}只）\n\n"
        content += "| 代码 | 名称 | 跟踪天数 | 最近出现 | 来源 |\n"
        content += "|------|------|---------|---------|------|\n"
        for s in reverse_stocks:
            days = _count_tracking_days(s)
            src = _primary_source(s)
            content += f"| {s['code']} | {s['name']} | {days}天 | {s['lastSeen']} | {src} |\n"
        content += "\n"

    # 其他关注
    other_stocks = tier_groups.get("其他关注", [])
    if other_stocks:
        content += f"### 其他关注（{len(other_stocks)}只）\n\n"
        content += "| 代码 | 名称 | 首次出现 | 最近出现 | 来源 |\n"
        content += "|------|------|---------|---------|------|\n"
        for s in other_stocks:
            src = _primary_source(s)
            content += f"| {s['code']} | {s['name']} | {s['firstSeen']} | {s['lastSeen']} | {src} |\n"
        content += "\n"

    # ---- 观察池 ----
    watch_stocks = tier_groups.get("观察池", [])
    if watch_stocks:
        content += "## 观察池（即将退潮）\n\n"
        content += "| 代码 | 名称 | 最后出现 | 剩余天数 | 来源 |\n"
        content += "|------|------|---------|---------|------|\n"
        for s in watch_stocks:
            try:
                last_seen = datetime.strptime(s["lastSeen"], "%Y-%m-%d")
                remaining = 5 - (today - last_seen).days
            except (ValueError, KeyError):
                remaining = "?"
            src = _primary_source(s)
            content += f"| {s['code']} | {s['name']} | {s['lastSeen']} | {remaining}天 | {src} |\n"
        content += "\n"

    # ---- 已退潮 ----
    if ebbed:
        content += "## 已退潮\n\n"
        content += "| 代码 | 名称 | 首次出现 | 最后出现 | 最高连板 |\n"
        content += "|------|------|---------|---------|--------|\n"
        for s in ebbed:
            content += f"| {s['code']} | {s['name']} | {s['firstSeen']} | {s['lastSeen']} | {s.get('maxBoard', 0)}板 |\n"
        content += "\n"

    content += f"> 详细历史记录见 [tracking-db.json](tracking-db.json)"

    with open(TRACKING_TABLE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def _generate_canvas(db: dict, date_str: str):
    """生成 Obsidian JSON Canvas 可视化看板"""
    stocks = db.get("stocks", {})
    if not stocks:
        return

    today = datetime.now()

    # 按层级分组（仅跟踪中的）
    tier_groups = {
        "明日重点": [],
        "放量异动": [],
        "连板梯队": [],
        "逆势个股": [],
        "其他关注": [],
        "观察池": [],
    }

    for s in stocks.values():
        if s["status"] != "跟踪中":
            continue
        tier = _get_tier(s, today)
        tier_groups.setdefault(tier, []).append(s)

    for k in tier_groups:
        tier_groups[k].sort(key=lambda s: s["code"])

    nodes = []
    edges = []
    node_ids_used = set()

    # ---- Canvas 布局常量 ----
    NODE_W = 210
    NODE_H = 85
    GAP_X = 20
    GAP_Y = 15
    GROUP_PAD = 30
    CANVAS_WIDTH = 1600

    # 工具函数：创建股票文本节点
    def make_stock_node(stock, x, y, color):
        nid = _hash_id(f"stock-{stock['code']}")
        sources = list(set(h.get("source", "") for h in stock.get("history", [])))
        src_label = "+".join([s.replace("分析", "").replace("筛选", "") for s in sources[:2]]) if sources else ""
        board = stock.get("maxBoard", 0)
        text = f"# {stock['name']}\n{stock['code']}"
        if board:
            text += f" | {board}板"
        if src_label:
            text += f"\n*{src_label}*"
        return {
            "id": nid,
            "type": "text",
            "x": x, "y": y,
            "width": NODE_W, "height": NODE_H,
            "color": color,
            "text": text,
        }

    # 工具函数：创建分组容器
    def make_group(label, x, y, w, h, color):
        nid = _hash_id(f"group-{label}-{x}-{y}")
        return {
            "id": nid,
            "type": "group",
            "x": x, "y": y,
            "width": w, "height": h,
            "label": label,
            "color": color,
        }

    current_y = 0

    # ========== 明日重点 ==========
    key_stocks = tier_groups.get("明日重点", [])
    if key_stocks:
        cols = max(len(key_stocks), 1)
        group_w = cols * (NODE_W + GAP_X) + GROUP_PAD * 2 - GAP_X
        group_h = NODE_H + GROUP_PAD * 2 + 30  # +30 for label
        nodes.append(make_group("明日重点", 0, current_y, group_w, group_h, TIER_COLORS["明日重点"]))
        for i, s in enumerate(key_stocks):
            nx = GROUP_PAD + i * (NODE_W + GAP_X)
            ny = current_y + GROUP_PAD + 20
            nodes.append(make_stock_node(s, nx, ny, TIER_COLORS["明日重点"]))
        current_y += group_h + 30

    # ========== 常规跟踪（三列布局：放量 | 连板 | 逆势）==========
    regular_tiers = [
        ("放量异动", tier_groups.get("放量异动", [])),
        ("连板梯队", tier_groups.get("连板梯队", [])),
        ("逆势个股", tier_groups.get("逆势个股", [])),
    ]

    col_width = (CANVAS_WIDTH - GROUP_PAD * 2) // 3
    max_rows = max(
        max((len(stocks) for _, stocks in regular_tiers), default=0),
        1,
    )

    # 计算每个分组的高度
    tier_heights = {}
    for tname, tstocks in regular_tiers:
        if tstocks:
            rows = (len(tstocks) + max(cols_for_width(col_width, NODE_W, GAP_X), 1) - 1) // max(cols_for_width(col_width, NODE_W, GAP_X), 1)
            tier_heights[tname] = max(rows * (NODE_H + GAP_Y) + GROUP_PAD * 2 + 30, NODE_H + GROUP_PAD * 2 + 30)
        else:
            tier_heights[tname] = NODE_H + GROUP_PAD * 2 + 30

    # 如果有内容才画常规跟踪行
    has_regular = any(stocks for _, stocks in regular_tiers)
    if has_regular:
        regular_row_h = max(tier_heights.values())
        # 外层大组
        nodes.append(make_group("常规跟踪", 0, current_y, CANVAS_WIDTH, regular_row_h + GROUP_PAD, "5"))

        for col_idx, (tname, tstocks) in enumerate(regular_tiers):
            if not tstocks:
                continue
            gx = GROUP_PAD + col_idx * col_width
            gy = current_y + GROUP_PAD
            gw = col_width - GAP_X
            gh = regular_row_h - 10
            nodes.append(make_group(tname, gx, gy, gw, gh, TIER_COLORS[tname]))

            inner_cols = max(cols_for_width(gw - GROUP_PAD * 2, NODE_W, GAP_X), 1)
            for i, s in enumerate(tstocks):
                row = i // inner_cols
                col = i % inner_cols
                nx = gx + GROUP_PAD + col * (NODE_W + GAP_X)
                ny = gy + GROUP_PAD + 20 + row * (NODE_H + GAP_Y)
                nodes.append(make_stock_node(s, nx, ny, TIER_COLORS[tname]))

        current_y += regular_row_h + GROUP_PAD + 30

    # ========== 其他关注 ==========
    other_stocks = tier_groups.get("其他关注", [])
    if other_stocks:
        cols = min(cols_for_width(CANVAS_WIDTH - GROUP_PAD * 2, NODE_W, GAP_X), len(other_stocks))
        rows = (len(other_stocks) + cols - 1) // cols
        group_w = CANVAS_WIDTH
        group_h = rows * (NODE_H + GAP_Y) + GROUP_PAD * 2 + 30
        nodes.append(make_group("其他关注", 0, current_y, group_w, group_h, TIER_COLORS["其他关注"]))
        for i, s in enumerate(other_stocks):
            row = i // cols
            col = i % cols
            nx = GROUP_PAD + col * (NODE_W + GAP_X)
            ny = current_y + GROUP_PAD + 20 + row * (NODE_H + GAP_Y)
            nodes.append(make_stock_node(s, nx, ny, TIER_COLORS["其他关注"]))
        current_y += group_h + 30

    # ========== 观察池 ==========
    watch_stocks = tier_groups.get("观察池", [])
    if watch_stocks:
        cols = min(cols_for_width(CANVAS_WIDTH - GROUP_PAD * 2, NODE_W, GAP_X), len(watch_stocks))
        rows = (len(watch_stocks) + cols - 1) // cols
        group_w = CANVAS_WIDTH
        group_h = rows * (NODE_H + GAP_Y) + GROUP_PAD * 2 + 30
        nodes.append(make_group("观察池", 0, current_y, group_w, group_h, TIER_COLORS["观察池"]))
        for i, s in enumerate(watch_stocks):
            row = i // cols
            col = i % cols
            nx = GROUP_PAD + col * (NODE_W + GAP_X)
            ny = current_y + GROUP_PAD + 20 + row * (NODE_H + GAP_Y)
            nodes.append(make_stock_node(s, nx, ny, TIER_COLORS["观察池"]))

    # 收集所有 ID
    for n in nodes:
        node_ids_used.add(n["id"])

    canvas = {
        "nodes": nodes,
        "edges": edges,
    }

    os.makedirs(TRACKING_DIR, exist_ok=True)
    with open(TRACKING_CANVAS_FILE, "w", encoding="utf-8") as f:
        json.dump(canvas, f, ensure_ascii=False, indent=2)

    print(f"   📊 Canvas 看板: {len(nodes)} 个节点")


def cols_for_width(width: int, node_w: int, gap: int) -> int:
    """计算给定宽度下能放多少列"""
    if width < node_w:
        return 1
    return max(1, (width + gap) // (node_w + gap))


def _count_tracking_days(stock: dict) -> int:
    """计算跟踪天数"""
    try:
        first = datetime.strptime(stock["firstSeen"], "%Y-%m-%d")
        return (datetime.now() - first).days + 1
    except (ValueError, KeyError):
        return 0


def _primary_source(stock: dict) -> str:
    """获取股票的主要来源（去重后取最新）"""
    sources = []
    for h in stock.get("history", []):
        src = h.get("source", "")
        if src and src not in sources:
            sources.append(src)
    return sources[-1] if sources else "-"


def get_tracking_stats() -> dict:
    """获取跟踪统计"""
    db = _load_db()
    stocks = db.get("stocks", {})
    tracking = sum(1 for s in stocks.values() if s["status"] == "跟踪中")
    ebbed = sum(1 for s in stocks.values() if s["status"] == "已退潮")
    return {"总计": len(stocks), "跟踪中": tracking, "已退潮": ebbed}
