"""
Prompt 构建模块 - 将数据注入模板，生成发给 AI 的完整 prompt
"""

import json
import os

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# 周日模式模板
WEEKLY_TEMPLATE = """
## 汇总周期：{start_date} - {end_date}
### 周末核心要闻
（请通过联网搜索获取本周五收盘后至周日的最新市场要闻）

### 周末政策/监管
（请搜索周末期间发布的宏观政策、行业政策、监管动态）

### 周末个股公告
利好公告（业绩/增持/回购/订单/获批）：列出代码+名称+内容
风险公告（减持/立案/业绩暴雷/风险提示）：列出代码+名称+内容+风险标注

### 周末外围市场
全球股市表现（美股/港股/欧股周五收盘情况）
A50 期指涨跌幅
对 A 股影响分析

### 周一开盘预判
情绪倾向、主线板块预判、风险板块提示、操作建议
"""

# 节假日模式模板
HOLIDAY_TEMPLATE = """
假期名称：{holiday_name} 休市时间：{start_date} - {end_date} 开盘时间：{next_trading_day}

### 假期全周期重要新闻
（请通过联网搜索获取假期期间所有重要市场新闻）

### 假期重磅政策
（请搜索假期期间发布的重大政策）

### 假期个股公告
利好公告（代码+名称+内容）
风险公告（代码+名称+内容+风险标注）

### 假期外围市场
全球市场假期期间整体表现
A50 期指涨跌幅
对 A 股开盘影响

### 节后开盘判断
开盘预期：高开高走/高开震荡/平开/低开震荡/低开低走
主线方向预判、风险方向、仓位/策略建议
"""


def load_template(name: str) -> str:
    """读取模板文件"""
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_daily_prompts(data: dict) -> list[dict]:
    """构建每日复盘 4 合 1 的 prompt 列表"""
    raw_json = json.dumps(data, ensure_ascii=False, indent=2)

    prompts = [
        {
            "name": "每日复盘",
            "output_suffix": "复盘",
            "prompt": load_template("daily_review_prompt.md").replace("{{RAW_DATA}}", raw_json),
        },
        {
            "name": "涨停跌停潮分析",
            "output_suffix": "涨停跌停潮",
            "prompt": load_template("limit_up_analysis_prompt.md").replace("{{RAW_DATA}}", raw_json),
        },
        {
            "name": "放量筛选",
            "output_suffix": "放量筛选",
            "prompt": load_template("volume_screening_prompt.md").replace("{{RAW_DATA}}", raw_json),
        },
        {
            "name": "全球市场消息汇总",
            "output_suffix": "消息汇总",
            "prompt": load_template("news_summary_prompt.md")
            .replace("{{MODE_TITLE}}", "工作日每日消息汇总")
            .replace("{{MODE_DESC}}", "当日市场消息汇总分析")
            .replace("{{RAW_DATA}}", raw_json)
            .replace(
                "{{MODE_TEMPLATE}}",
                """
### 日期：{date}（周{weekday}）
#### 核心市场新闻
（通过联网搜索获取当日重要市场新闻，列出3-5条最关键的消息）
#### 宏观/监管政策
政策内容 + 影响评级（利好/中性/利空）+ 影响范围
#### 行业板块消息
利好行业、利空行业、行业催化
#### 个股重要公告
利好公告（业绩/增持/回购/订单/获批）：列出代码+名称+内容
风险公告（减持/立案/业绩暴雷/风险提示）：列出代码+名称+内容+风险标注
#### 外围市场
美股/港股/A50期指、大宗商品/汇率
#### 当日消息综合影响
整体评级、明日预判、重点关注、风险规避
""",
            ),
        },
    ]

    # 注入日期信息
    date_str = data.get("采集日期", "")
    weekday = data.get("星期", "")
    for p in prompts:
        p["prompt"] = p["prompt"].replace("{date}", date_str).replace("{weekday}", weekday)

    return prompts


def build_weekly_prompt(data: dict) -> dict:
    """构建周日消息汇总 prompt"""
    from datetime import datetime, timedelta

    raw_json = json.dumps(data, ensure_ascii=False, indent=2)
    today = datetime.now()
    # 周日汇总本周五到周日
    friday = today - timedelta(days=2)
    sunday = today

    template = load_template("news_summary_prompt.md")
    prompt = (
        template.replace("{{MODE_TITLE}}", "周末消息汇总")
        .replace("{{MODE_DESC}}", "周末市场消息汇总与周一开盘预判")
        .replace("{{RAW_DATA}}", raw_json)
        .replace(
            "{{MODE_TEMPLATE}}",
            WEEKLY_TEMPLATE.format(
                start_date=friday.strftime("%Y-%m-%d"), end_date=sunday.strftime("%Y-%m-%d")
            ),
        )
    )

    return {
        "name": "周末消息汇总",
        "output_suffix": f"周末消息汇总-{friday.strftime('%Y%m%d')}-{sunday.strftime('%Y%m%d')}",
        "prompt": prompt,
    }


def build_holiday_prompt(
    data: dict, holiday_name: str, start_date: str, end_date: str, next_trading_day: str
) -> dict:
    """构建节假日消息汇总 prompt"""
    raw_json = json.dumps(data, ensure_ascii=False, indent=2)

    template = load_template("news_summary_prompt.md")
    prompt = (
        template.replace("{{MODE_TITLE}}", f"{holiday_name}假期消息汇总")
        .replace("{{MODE_DESC}}", f"{holiday_name}休市期间消息汇总与节后开盘预判")
        .replace("{{RAW_DATA}}", raw_json)
        .replace(
            "{{MODE_TEMPLATE}}",
            HOLIDAY_TEMPLATE.format(
                holiday_name=holiday_name,
                start_date=start_date,
                end_date=end_date,
                next_trading_day=next_trading_day,
            ),
        )
    )

    return {
        "name": f"{holiday_name}假期消息汇总",
        "output_suffix": f"假期消息汇总-{holiday_name}",
        "prompt": prompt,
    }
