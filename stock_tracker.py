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
from holiday_checker import is_holiday

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

# A股常见的行业/板块名称集合，用于识别被误提取为股票名的板块名
# 当正则从"板块名（代码）"中提取到这些词时，应识别为板块名而非股票名
_KNOWN_SECTOR_NAMES = {
    # 一级行业
    "电力", "贵金属", "化学原料", "化学制品", "化学纤维", "化学制药",
    "油服工程", "家居用品", "计算机设备", "计算机应用",
    "有色金属", "工业金属", "能源金属", "小金属",
    "煤炭", "煤炭开采", "钢铁", "银行", "保险", "券商", "证券",
    "房地产", "房地产开发", "房地产服务", "物业管理",
    "医药", "医药生物", "中药", "医疗器械", "生物制品", "医药商业",
    "白酒", "食品饮料", "食品加工", "家电", "家纺", "纺织服饰",
    "新能源", "光伏", "光伏设备", "锂电池", "电池", "储能", "充电桩",
    "半导体", "芯片", "消费电子", "光学光电子", "电子元件",
    "军工", "国防军工", "军工电子", "地面兵装", "航海装备",
    "通信", "通信设备", "通信服务", "软件开发", "IT服务",
    "传媒", "游戏", "影视", "出版", "广告", "广告营销",
    "汽车", "汽车整车", "零部件", "汽车服务",
    "机械设备", "通用设备", "专用设备", "自动化设备", "轨交设备",
    "建筑材料", "水泥", "玻璃", "装修建材",
    "农林牧渔", "养殖业", "种植业", "农产品加工", "渔业",
    "交通运输", "航空机场", "铁路公路", "航运港口", "物流",
    "公用事业", "环保", "燃气", "水务",
    "风电", "风电设备", "核电", "氢能源", "天然气", "石油",
    "特高压", "智能电网", "电网", "电网设备", "电力设备",
    "工程机械", "工业母机", "机器人",
    "轻工制造", "商业贸易", "休闲服务",
    "造纸", "包装印刷", "塑料", "橡胶",
    "非金属材料", "金属新材料", "稀土", "磁材", "碳纤维",
    "多元金融", "期货", "信托",
    "教育", "医疗", "医美", "医疗服务",
    # 常见被截断的板块名
    "计算机设",  # "计算机设备"被截断
    "化学原",    # "化学原料"被截断
    "家居用",    # "家居用品"被截断
}


def is_likely_sector_name(name: str) -> bool:
    """判断给定名称是否更可能是板块/行业名而非股票名"""
    return _is_likely_sector_name(name)


def _is_likely_sector_name(name: str) -> bool:
    """判断给定名称是否更可能是板块/行业名而非股票名"""
    if name in _KNOWN_SECTOR_NAMES:
        return True
    # 后缀匹配：只使用几乎不出现在公司名中的板块专用后缀
    # 刻意排除 "电子""医药""生物""设备""材料""化工" 等，因为这些常见于公司名
    _sector_unique_suffixes = ("原料", "开采", "用品", "元件", "制品")
    for suffix in _sector_unique_suffixes:
        if name.endswith(suffix) and len(name) >= 4:
            return True
    return False


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


def _is_blacklisted(name: str) -> bool:
    """过滤 ST 股、退市股"""
    return "ST" in name or "退" in name


def _is_trading_day(date_str: str) -> bool:
    """判断是否是交易日（周一至周五且非法定节假日）"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return True
    if dt.weekday() >= 5:
        return False
    is_hol, _, _, _ = is_holiday(dt)
    return not is_hol


def _count_trading_days_since(from_date_str: str, to_date: datetime) -> int:
    """计算从 from_date 到 to_date（含）之间经过了多少个交易日（不包含 from_date 当天）"""
    try:
        start = datetime.strptime(from_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0
    if start >= to_date:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= to_date:
        if _is_trading_day(current.strftime("%Y-%m-%d")):
            count += 1
        current += timedelta(days=1)
    return count


def extract_stock_codes(texts: list[str]) -> list[dict]:
    """从 AI 生成的文本中提取股票代码和名称（自动过滤 ST/退市股和板块名）"""
    stocks = []
    seen = {}  # code -> index in stocks (改为 dict，便于后续覆盖)

    # 格式1: 名称（代码）或 名称(代码) — 常见于分析文本中
    pattern_text = re.compile(r"([\u4e00-\u9fa5]{2,6})\s*[（(]?\s*(\d{6})\s*[）)]?")
    # 格式2: | 000977 | 浪潮信息 | 或 | 1 | 000977 | 浪潮信息 | — 常见于表格行
    pattern_table = re.compile(r"\|\s*(?:\d+\s*\|)?\s*(\d{6})\s*\|\s*([\u4e00-\u9fa5]{2,6})\s*\|")

    def _accept(code, name):
        """尝试接受一个(代码,名称)对，如果代码已存在但之前是板块名，则用新名称覆盖"""
        if _is_blacklisted(name):
            return
        if _is_likely_sector_name(name):
            # 板块名不视为有效股票名，但如果该代码之前没被记录过则先占位
            if code not in seen:
                seen[code] = len(stocks)
                stocks.append({"代码": code, "名称": name})
            # 如果已存在，不覆盖（保留旧名称，无论旧名称是什么）
            return

        # 非板块名（正常的股票名）
        if code in seen:
            idx = seen[code]
            old_name = stocks[idx]["名称"]
            if _is_likely_sector_name(old_name):
                # 旧名称是板块名，用新名称覆盖
                stocks[idx]["名称"] = name
        else:
            seen[code] = len(stocks)
            stocks.append({"代码": code, "名称": name})

    for text in texts:
        # 先匹配文本格式
        for name, code in pattern_text.findall(text):
            _accept(code, name)
        # 再匹配表格格式
        for code, name in pattern_table.findall(text):
            _accept(code, name)

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


def _update_status_lifecycle(db: dict):
    """遍历 tracking-db，按交易日计算生命周期

    规则：
    - "跟踪中" + 连续 ≥5 个交易日未出现 → "休眠"
    - "休眠" 的不做额外处理（在 update_tracking 中当日重新出现时激活）
    """
    today = datetime.now()
    dormant_count = 0

    for code, entry in db["stocks"].items():
        if entry["status"] != "跟踪中":
            continue

        trading_days_gap = _count_trading_days_since(entry["lastSeen"], today)
        if trading_days_gap >= 5:
            entry["status"] = "休眠"
            entry["history"].append({
                "date": today.strftime("%Y-%m-%d"),
                "event": f"自动休眠（连续{trading_days_gap}个交易日未出现）",
                "source": "系统",
            })
            dormant_count += 1

    if dormant_count:
        print(f"   💤 {dormant_count} 只标记为休眠（≥5个交易日未出现）")


def update_tracking(source_texts: list[tuple[str, str]]):
    """根据 AI 生成的内容更新股票跟踪库

    Args:
        source_texts: [(来源名, AI文本), ...]
                      来源名如 '放量筛选' '涨停跌停潮分析' '每日复盘' 等
    """
    today = datetime.now().strftime("%Y-%m-%d")
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
                # 防止板块名覆盖正确的股票名：如果新名称像板块名而旧名称不像，保留旧名称
                if not _is_likely_sector_name(name) or _is_likely_sector_name(entry.get("name", "")):
                    entry["name"] = name
                entry["lastSeen"] = today

                # 休眠重激活：如果之前是休眠状态，当日重新出现则激活
                if entry["status"] == "休眠":
                    entry["status"] = "跟踪中"
                    entry["history"].append({
                        "date": today,
                        "event": f"重新激活（休眠后再次出现在{source_name}）",
                        "source": source_name,
                    })
                    updated_count += 1
                    continue  # 跳过常规的"再次出现"记录，避免重复

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
                # 首次入库时如果名称像板块名，记录警告但仍入库（后续正确名称出现时会覆盖）
                if _is_likely_sector_name(name):
                    print(f"   ⚠️ 警告: {code} 首次入库但名称为疑似板块名 '{name}'，待后续修正")
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

    # 生命周期管理：跟踪中 → 休眠（≥5个交易日未出现）
    _update_status_lifecycle(db)

    _save_db(db)

    # 旧格式输出（股票跟踪总表.md + 看板）已废弃，由 tracking_curator.py 替代
    # _generate_table(db, today)
    # _generate_canvas(db, today)

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
