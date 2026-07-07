"""
股票跟踪库管理模块 - 单文件表格形式展示所有跟踪股票
输出: 05-Skills/股票跟踪/股票跟踪总表.md
同时维护 tracking-db.json 作为数据库
"""

import json
import os
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_TRACKING_DIR = os.getenv("OBSIDIAN_TRACKING_DIR", "05-Skills/股票跟踪")

TRACKING_DIR = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_TRACKING_DIR)
TRACKING_DB_FILE = os.path.join(TRACKING_DIR, "tracking-db.json")
TRACKING_TABLE_FILE = os.path.join(TRACKING_DIR, "股票跟踪总表.md")


def _load_db() -> dict:
    """加载跟踪数据库"""
    os.makedirs(TRACKING_DIR, exist_ok=True)
    if os.path.exists(TRACKING_DB_FILE):
        with open(TRACKING_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "lastUpdated": "", "stocks": {}}


def _save_db(db: dict):
    """保存跟踪数据库"""
    db["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(TRACKING_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def extract_stock_codes(texts: list[str]) -> list[dict]:
    """从 AI 生成的文本中提取股票代码和名称"""
    stocks = []
    seen = set()

    # 匹配 "名称代码" 或 "名称（代码）" 格式
    pattern = re.compile(r"([\u4e00-\u9fa5]{2,6})\s*[（(]?\s*(\d{6})\s*[）)]?")

    for text in texts:
        matches = pattern.findall(text)
        for name, code in matches:
            if code not in seen:
                seen.add(code)
                stocks.append({"代码": code, "名称": name})

    return stocks


def update_tracking(ai_texts: list[str], source: str = "每日复盘"):
    """
    根据 AI 生成的内容更新股票跟踪库
    """
    today = datetime.now().strftime("%Y-%m-%d")
    db = _load_db()
    stocks_found = extract_stock_codes(ai_texts)

    print(f"🔍 从分析结果中提取到 {len(stocks_found)} 只股票")

    updated_count = 0
    new_count = 0

    for stock in stocks_found:
        code = stock["代码"]
        name = stock["名称"]

        if code in db["stocks"]:
            entry = db["stocks"][code]
            entry["name"] = name
            entry["lastSeen"] = today
            entry["history"].append(
                {"date": today, "event": f"再次出现({source})", "source": source}
            )
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
                "history": [
                    {"date": today, "event": f"首次入库({source})", "source": source}
                ],
            }
            new_count += 1

    # 检查退潮条件：连续5天未出现
    five_days_ago = datetime.now() - timedelta(days=5)
    for code, entry in db["stocks"].items():
        if entry["status"] == "跟踪中":
            last_seen = datetime.strptime(entry["lastSeen"], "%Y-%m-%d")
            if last_seen < five_days_ago:
                entry["status"] = "已退潮"
                entry["history"].append(
                    {"date": today, "event": "自动退潮(5日未出现)", "source": "系统"}
                )

    _save_db(db)

    # 生成单文件跟踪总表
    _generate_table(db, today)

    print(f"   ✅ 新增 {new_count} 只，更新 {updated_count} 只")
    return db


def _generate_table(db: dict, date_str: str):
    """生成股票跟踪总表 Markdown 文件"""
    stocks = db.get("stocks", {})
    if not stocks:
        return

    # 按状态排序：跟踪中在前，已退潮在后
    sorted_stocks = sorted(stocks.values(), key=lambda s: (0 if s["status"] == "跟踪中" else 1, s["code"]))

    content = f"""---
tags: [股票跟踪]
updated: {date_str}
---

# 股票跟踪总表

> 自动更新于 {date_str} | 总计 {len(stocks)} 只 | 跟踪中 {sum(1 for s in stocks.values() if s['status'] == '跟踪中')} 只 | 已退潮 {sum(1 for s in stocks.values() if s['status'] == '已退潮')} 只

## 跟踪中

| 代码 | 名称 | 首次出现 | 最近出现 | 最高连板 | 标签 | 最近事件 |
|------|------|---------|---------|---------|------|---------|
"""
    # 跟踪中的股票
    for s in sorted_stocks:
        if s["status"] != "跟踪中":
            continue
        last_event = s["history"][-1]["event"] if s["history"] else "-"
        tags = ", ".join(s.get("tags", [])) if s.get("tags") else "-"
        content += f"| {s['code']} | {s['name']} | {s['firstSeen']} | {s['lastSeen']} | {s.get('maxBoard', 0)}板 | {tags} | {last_event} |\n"

    # 已退潮的股票
    ebbed = [s for s in sorted_stocks if s["status"] == "已退潮"]
    if ebbed:
        content += "\n## 已退潮\n\n"
        content += "| 代码 | 名称 | 首次出现 | 最后出现 | 最高连板 | 标签 |\n"
        content += "|------|------|---------|---------|---------|------|\n"
        for s in ebbed:
            tags = ", ".join(s.get("tags", [])) if s.get("tags") else "-"
            content += f"| {s['code']} | {s['name']} | {s['firstSeen']} | {s['lastSeen']} | {s.get('maxBoard', 0)}板 | {tags} |\n"

    content += f"\n> 详细历史记录见 [tracking-db.json](tracking-db.json)"

    with open(TRACKING_TABLE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def get_tracking_stats() -> dict:
    """获取跟踪统计"""
    db = _load_db()
    stocks = db.get("stocks", {})
    tracking = sum(1 for s in stocks.values() if s["status"] == "跟踪中")
    ebbed = sum(1 for s in stocks.values() if s["status"] == "已退潮")
    return {"总计": len(stocks), "跟踪中": tracking, "已退潮": ebbed}
