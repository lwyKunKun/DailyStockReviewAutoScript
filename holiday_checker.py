"""
节假日判断模块 - 判断今天应该执行什么模式
"""

import json
import os
from datetime import datetime, timedelta


def _load_holidays() -> list[dict]:
    """加载节假日数据"""
    path = os.path.join(os.path.dirname(__file__), "holidays.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("2026", [])


def is_weekend(date: datetime = None) -> bool:
    """判断是否是周末（周六/周日）"""
    if date is None:
        date = datetime.now()
    return date.weekday() >= 5  # 5=周六, 6=周日


def is_holiday(date: datetime = None) -> tuple[bool, str, str, str]:
    """判断 date 是否在节假日中。返回 (是否节假日, 节假日名称, 起始日, 结束日)"""
    if date is None:
        date = datetime.now()
    date_str = date.strftime("%Y-%m-%d")

    holidays = _load_holidays()
    for h in holidays:
        if h["起始日"] <= date_str <= h["结束日"]:
            return True, h["名称"], h["起始日"], h["结束日"]
    return False, "", "", ""


def is_holiday_last_day(date: datetime = None) -> tuple[bool, str, str, str]:
    """判断 date 是否是节假日的最后一天"""
    if date is None:
        date = datetime.now()

    is_hol, name, start, end = is_holiday(date)
    if not is_hol:
        return False, "", "", ""

    date_str = date.strftime("%Y-%m-%d")
    if date_str == end:
        return True, name, start, end
    return False, "", "", ""


def get_next_trading_day(after_date: str) -> str:
    """获取某个日期之后的下一个交易日（简单版：跳过周末）"""
    d = datetime.strptime(after_date, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def should_run_today() -> str:
    """
    判断今天应该运行什么模式
    返回: "daily" | "weekly" | "holiday" | "skip"
    """
    now = datetime.now()

    # 周六 → 跳过
    if now.weekday() == 5:
        return "skip"

    # 判断是否节假日最后一天
    is_last, name, start, end = is_holiday_last_day(now)
    if is_last:
        return "holiday"

    # 判断是否在节假日中（非最后一天）
    is_hol, _, _, _ = is_holiday(now)
    if is_hol:
        return "skip"

    # 周日 → 周末汇总
    if now.weekday() == 6:
        return "weekly"

    # 周一～周五 → 每日复盘
    return "daily"


def get_holiday_info() -> dict:
    """如果是节假日最后一天，返回节假日信息"""
    now = datetime.now()
    is_last, name, start, end = is_holiday_last_day(now)
    if is_last:
        return {
            "holiday_name": name,
            "start_date": start,
            "end_date": end,
            "next_trading_day": get_next_trading_day(end),
        }
    return {}
