#!/usr/bin/env python3
"""
股票每日复盘自动化系统 · 主入口
用法:
  python3 main.py                                # 自动判断模式 (读取缓存数据)
  python3 main.py --mode fetch                   # 仅采集数据，保存到缓存
  python3 main.py --mode dragon                  # 盘后补充龙虎榜数据，合并进缓存
  python3 main.py --mode daily                   # 强制每日复盘 (读缓存)
  python3 main.py --mode daily --force-refresh   # 每日复盘 (强制重新采集)
  python3 main.py --mode weekly                  # 强制周末汇总
  python3 main.py --mode holiday                 # 强制节假日汇总
  python3 main.py --mode daily --dry-run         # 干跑（不写文件，打印输出）

定时流程:
  15:00 → python3 main.py --mode fetch           # 收盘后拉基础数据，存缓存
  17:30 → python3 main.py --mode dragon          # 补充龙虎榜+涨停+连板数据
  19:00 → python3 main.py --mode auto            # 读缓存，AI分析，写Obsidian
"""

import sys
import os
import json
import traceback
from datetime import datetime

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(__file__))

from data_fetcher import collect_all, fetch_limit_up_data, fetch_dragon_tiger, fetch_consecutive_board_stocks, fetch_fund_flow, _enrich_consecutive_board, _get_board_type, fetch_reduction_risk
from prompt_builder import build_daily_prompts, build_weekly_prompt, build_holiday_prompt
from ai_analyzer import analyze_batch, analyze
from output_writer import write_daily_reviews, write_weekly_summary, write_holiday_summary, write_log
from stock_tracker import update_tracking, get_tracking_stats
from holiday_checker import should_run_today, get_holiday_info
from tracking_curator import curate_tracking

# 缓存文件路径
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "latest_data.json")


def run_fetch_mode():
    """仅采集数据并保存到缓存"""
    print("=" * 60)
    print(f"📡 数据采集模式 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    write_log("启动数据采集", "INFO")
    data = collect_all()

    # 保存缓存
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"📁 缓存已保存: {CACHE_FILE}")
    print(f"   📈 涨停: {data['涨跌停']['涨停家数']}家  "
          f"跌停: {data['涨跌停']['跌停家数']}家  "
          f"成交额: {data['市场宽度']['总成交额']}亿")
    write_log(f"数据采集完成 - 涨停{data['涨跌停']['涨停家数']}家 成交额{data['市场宽度']['总成交额']}亿", "INFO")
    print("✅ 数据采集完成!")
    return data


def load_cached_data(force_stale: bool = False) -> dict:
    """读取缓存数据。工作日缓存非今日数据时拒绝运行（除非 force_stale=True）。"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cache_time = data.get("采集时间", "未知")
        print(f"📂 使用缓存数据 (采集于 {cache_time})")

        # 缓存新鲜度检查：工作日（周一到周五）必须用今日缓存
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        weekday = today.weekday()  # 0=周一, 6=周日
        cache_date = cache_time[:10] if cache_time != "未知" else ""

        if weekday < 5 and cache_date and cache_date != today_str:
            msg = (f"❌ 致命错误：缓存数据来自 {cache_date}，非今日({today_str})数据！\n"
                   f"   15:00 fetch 可能尚未完成或已失败。\n"
                   f"   为确保复盘准确性，已拒绝运行。\n"
                   f"   若确认要强制使用过期缓存，请加 --force 参数。")
            print(msg)
            write_log(f"拒绝运行：缓存过期 (缓存{cache_date} vs 今日{today_str})", "ERROR")
            if force_stale:
                print("⚠️  已通过 --force 强制使用过期缓存，数据可能不准确！")
                write_log("强制使用过期缓存", "WARN")
            else:
                sys.exit(1)
        elif cache_date and cache_date != today_str:
            print(f"⚠️  警告：缓存数据来自 {cache_date}，非今日({today_str})数据！（周末/节假日容忍）")

        return data
    else:
        print("⚠️  缓存不存在，重新采集数据...")
        return collect_all()


def run_dragon_mode():
    """盘后补充采集龙虎榜+涨停数据，合并进缓存"""
    print("=" * 60)
    print(f"🐉 龙虎榜补充采集 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    write_log("启动龙虎榜补充采集", "INFO")

    # 1. 读取现有缓存
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"📂 读取现有缓存 (采集于 {data.get('采集时间', '未知')})")
    else:
        print("⚠️  缓存不存在，无法补充采集（请先执行 --mode fetch）")
        write_log("龙虎榜补充采集失败：缓存不存在", "ERROR")
        return

    # 2. 重新采集龙虎榜相关数据（覆盖缓存中的旧值）
    print("🐉 采集涨停板数据...")
    data["涨跌停"] = fetch_limit_up_data()
    print("🐉 采集龙虎榜数据...")
    data["龙虎榜"] = fetch_dragon_tiger()
    print("🐉 采集连板股数据...")
    data["连板股"] = fetch_consecutive_board_stocks()

    # 3. 补充资金流向（15:00 可能也不完整，顺手补一下）
    try:
        north_south = data.get("资金流向", {}).get("北向资金净流入", 0)
        if north_south == 0:
            print("🐉 补充资金流向数据...")
            data["资金流向"] = fetch_fund_flow()
    except Exception:
        pass

    # 4. 连板股增强（补充市值/PE/板块类型/减持标记）
    try:
        stock_list = data.get("个股行情", [])
        if stock_list:
            stock_map = {s.get("代码", ""): s for s in stock_list}
            reduction_list = fetch_reduction_risk()
            reduction_codes = {r["代码"] for r in reduction_list}
            data["减持风险"] = reduction_list
            data["连板股"] = _enrich_consecutive_board(
                data.get("连板股", []), stock_map, reduction_codes
            )
            print(f"🐉 连板股增强: {len(data['连板股'])} 只")
    except Exception:
        pass

    # 5. 板块涨停统计合并到板块排名
    try:
        sector_zt = data.get("涨跌停", {}).get("板块涨停统计", {})
        if sector_zt and data.get("板块排名", {}).get("板块列表"):
            for info in data["板块排名"]["板块列表"]:
                if info.get("名称", "") in sector_zt:
                    info["涨停家数"] = sector_zt[info.get("名称", "")]
            for item in data["板块排名"].get("领涨板块", []):
                item["涨停家数"] = sector_zt.get(item.get("名称", ""), 0)
            for item in data["板块排名"].get("领跌板块", []):
                item["涨停家数"] = sector_zt.get(item.get("名称", ""), 0)
    except Exception:
        pass

    # 6. 更新采集时间
    data["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 5. 保存回缓存
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"📁 缓存已更新: {CACHE_FILE}")
    print(f"   🐉 涨停: {data['涨跌停']['涨停家数']}家  "
          f"跌停: {data['涨跌停']['跌停家数']}家  "
          f"连板: {data['涨跌停']['连板家数']}家  "
          f"龙虎榜: {len(data['龙虎榜'].get('龙虎榜个股', []))}只")
    write_log(
        f"龙虎榜补充采集完成 - 涨停{data['涨跌停']['涨停家数']}家 龙虎榜{len(data['龙虎榜'].get('龙虎榜个股', []))}只",
        "INFO",
    )
    print("✅ 龙虎榜补充采集完成!")


def run_daily_mode(dry_run: bool = False, force_refresh: bool = False, force_stale: bool = False):
    """执行每日复盘 4合1"""
    print("=" * 60)
    print(f"📊 每日复盘模式 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    write_log("启动每日复盘模式", "INFO")

    # 1. 加载数据（优先读缓存）
    if force_refresh:
        write_log("强制刷新，重新采集数据", "INFO")
        data = collect_all()
    else:
        data = load_cached_data(force_stale=force_stale)

    write_log(f"数据就绪 - 成交额: {data['市场宽度']['总成交额']}亿", "INFO")

    # 2. 构建 prompts
    prompts = build_daily_prompts(data)
    write_log(f"构建 {len(prompts)} 个分析任务", "INFO")

    # 3. AI 分析
    write_log("开始 AI 分析", "INFO")
    results = analyze_batch(prompts)
    write_log("AI 分析完成", "INFO")

    # 4. 写入文件
    if not dry_run:
        written = write_daily_reviews(results)
        write_log(f"写入 {len(written)} 个文件", "INFO")
    else:
        print("\n--- 🧪 干跑模式，跳过文件写入 ---")
        for r in results:
            print(f"\n{'='*40}")
            print(f"📝 {r['name']}")
            print(f"{'='*40}")
            print(r["content"][:500] + "..." if len(r["content"]) > 500 else r["content"])

    # 5. 更新股票跟踪
    if not dry_run and results:
        source_texts = [(r["name"], r["content"]) for r in results]
        db = update_tracking(source_texts)
        stats = get_tracking_stats()
        write_log(f"股票跟踪更新 - 总计: {stats['总计']} 跟踪中: {stats['跟踪中']} 已退潮: {stats['已退潮']}", "INFO")
        print(f"📈 股票跟踪库: {stats}")

        # 5.5 AI 智能筛选（生成仪表盘+个股文件）
        try:
            curated = curate_tracking()
            if curated:
                write_log(
                    f"AI筛选完成 - 核心池: {len(curated.get('core_pool', []))}只 "
                    f"观察池: {len(curated.get('watch_pool', []))}只",
                    "INFO",
                )
                print(f"🧠 AI 智能筛选: 核心池 {len(curated.get('core_pool', []))}只 | 观察池 {len(curated.get('watch_pool', []))}只")
        except Exception as e:
            print(f"⚠️  AI 智能筛选失败（不影响其他流程）: {e}")
            write_log(f"AI筛选失败: {e}", "WARN")

    # 6. 健康检查：验证输出文件日期
    if not dry_run and results:
        today_str = datetime.now().strftime("%Y-%m-%d")
        health_issues = []
        for r in results:
            content = r.get("content", "")
            out_date = r.get("output_suffix", "")
            # 检查内容中是否出现今日日期
            if today_str not in content and "2026" in content:
                health_issues.append(f"{out_date}: 内容中未找到今日日期 {today_str}")
        if health_issues:
            for issue in health_issues:
                print(f"⚠️  健康检查警告: {issue}")
            write_log(f"健康检查发现问题: {'; '.join(health_issues)}", "WARN")
        else:
            write_log(f"健康检查通过: {len(results)} 份报告日期校验一致", "INFO")

    # 7. 完成
    write_log("每日复盘模式执行完毕", "INFO")
    print("\n✅ 每日复盘完成!")

    return results


def run_weekly_mode(dry_run: bool = False):
    """执行周日消息汇总"""
    print("=" * 60)
    print(f"📰 周末消息汇总模式 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    write_log("启动周末消息汇总模式", "INFO")

    # 使用缓存数据（如果有的话，周日的缓存是周五采集的）
    data = load_cached_data(force_stale=True)

    # 构建 prompt
    task = build_weekly_prompt(data)
    write_log("构建周末汇总任务", "INFO")

    # AI 分析
    result = analyze(task["prompt"], task["name"])
    result_dict = {
        "name": task["name"],
        "output_suffix": task["output_suffix"],
        "content": result,
    }

    # 写入（传入周日日期）
    if not dry_run:
        filepath = write_weekly_summary(result_dict, date=task["date"])
        write_log(f"写入: {filepath}", "INFO")
    else:
        print("\n--- 🧪 干跑模式 ---")
        print(result[:500] + "..." if len(result) > 500 else result)

    write_log("周末消息汇总完成", "INFO")
    print("\n✅ 周末消息汇总完成!")

    return [result_dict]


def run_holiday_mode(dry_run: bool = False):
    """执行节假日消息汇总"""
    info = get_holiday_info()
    if not info:
        print("❌ 今天不是节假日最后一天")
        return []

    print("=" * 60)
    print(f"🏖️  节假日消息汇总模式 - {info['holiday_name']} ({info['start_date']} ~ {info['end_date']})")
    print("=" * 60)

    write_log(f"启动节假日消息汇总 - {info['holiday_name']}", "INFO")

    # 使用缓存数据
    data = load_cached_data()

    # 构建 prompt
    task = build_holiday_prompt(
        data,
        info["holiday_name"],
        info["start_date"],
        info["end_date"],
        info["next_trading_day"],
    )
    write_log(f"构建节假日汇总: {info['holiday_name']}", "INFO")

    # AI 分析
    result = analyze(task["prompt"], task["name"])
    result_dict = {
        "name": task["name"],
        "output_suffix": task["output_suffix"],
        "content": result,
    }

    # 写入（传入节假日最后一天日期）
    if not dry_run:
        end_date = datetime.strptime(info["end_date"], "%Y-%m-%d")
        filepath = write_holiday_summary(result_dict, date=end_date)
        write_log(f"写入: {filepath}", "INFO")
    else:
        print("\n--- 🧪 干跑模式 ---")
        print(result[:500] + "..." if len(result) > 500 else result)

    write_log("节假日消息汇总完成", "INFO")
    print("\n✅ 节假日消息汇总完成!")

    return [result_dict]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="股票每日复盘自动化系统")
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "holiday", "auto", "fetch", "dragon"],
        default="auto",
        help="运行模式: fetch=仅采集数据, dragon=补充龙虎榜, auto=自动判断, daily/weekly/holiday=指定模式",
    )
    parser.add_argument("--dry-run", action="store_true", help="干跑模式，不写入文件")
    parser.add_argument("--force-refresh", action="store_true", help="强制重新采集数据（不使用缓存）")
    parser.add_argument("--force", action="store_true", help="强制运行，忽略缓存过期等安全检查")
    args = parser.parse_args()

    # fetch 模式：仅采集数据
    if args.mode == "fetch":
        run_fetch_mode()
        return

    # dragon 模式：盘后补充龙虎榜
    if args.mode == "dragon":
        run_dragon_mode()
        return

    # 自动判断模式
    if args.mode == "auto":
        mode = should_run_today()
        print(f"🔍 自动判断: 今日模式 = {mode}")
        if mode == "skip":
            print("⏭️  今日无需执行复盘（周六或节假日中），退出")
            write_log("自动判断跳过执行", "INFO")
            return
        args.mode = mode

    try:
        if args.mode == "daily":
            run_daily_mode(dry_run=args.dry_run, force_refresh=args.force_refresh, force_stale=args.force)
        elif args.mode == "weekly":
            run_weekly_mode(dry_run=args.dry_run)
        elif args.mode == "holiday":
            run_holiday_mode(dry_run=args.dry_run)
    except Exception as e:
        error_msg = f"执行失败: {e}\n{traceback.format_exc()}"
        print(f"❌ {error_msg}")
        write_log(error_msg, "ERROR")


if __name__ == "__main__":
    main()
