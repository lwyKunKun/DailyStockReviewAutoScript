"""
输出写入模块 - 将 AI 生成的内容写入 Obsidian Vault
每日输出结构: 01-Projects/股票复盘/{年}/{月}/{日期}/
"""

import os
import sys
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", "")
OBSIDIAN_REVIEW_DIR = os.getenv("OBSIDIAN_REVIEW_DIR", "01-Projects/股票复盘")

if not OBSIDIAN_VAULT or not os.path.isdir(OBSIDIAN_VAULT):
    print(f"❌ Obsidian Vault 路径无效: {OBSIDIAN_VAULT}")
    print("   请在 .env 中设置正确的 OBSIDIAN_VAULT 路径")


def _get_daily_dir(date: datetime = None) -> str:
    """获取每日归档目录路径，格式: 2026/07月/2026-07-07/"""
    if date is None:
        date = datetime.now()
    year = date.strftime("%Y")
    month = date.strftime("%m月")
    day = date.strftime("%Y-%m-%d")
    dir_path = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_REVIEW_DIR, year, month, day)
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def _get_monthly_dir(date: datetime = None) -> str:
    """获取按月归档的目录路径（周末/节假日汇总用）"""
    if date is None:
        date = datetime.now()
    year = date.strftime("%Y")
    month = date.strftime("%m月")
    dir_path = os.path.join(OBSIDIAN_VAULT, OBSIDIAN_REVIEW_DIR, year, month)
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def write_daily_reviews(results: list[dict], date: datetime = None) -> list[str]:
    """将每日复盘 4合1 的结果写入日期子文件夹"""
    if date is None:
        date = datetime.now()
    output_dir = _get_daily_dir(date)

    written_files = []

    for result in results:
        suffix = result["output_suffix"]
        filename = f"{suffix}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(result["content"])

        written_files.append(filepath)
        print(f"📁 已写入: {filepath}")

    return written_files


def write_weekly_summary(result: dict, date: datetime = None) -> str:
    """写入周末消息汇总（放在月份目录下）"""
    if date is None:
        date = datetime.now()
    output_dir = _get_monthly_dir(date)

    suffix = result["output_suffix"]
    filename = f"{suffix}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(result["content"])

    print(f"📁 已写入: {filepath}")
    return filepath


def write_holiday_summary(result: dict, date: datetime = None) -> str:
    """写入节假日消息汇总（放在月份目录下）"""
    if date is None:
        date = datetime.now()
    output_dir = _get_monthly_dir(date)

    suffix = result["output_suffix"]
    filename = f"{suffix}.md"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(result["content"])

    print(f"📁 已写入: {filepath}")
    return filepath


def write_log(message: str, level: str = "INFO"):
    """写入运行日志"""
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"run-{date_str}.log")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{level}] {message}\n")
