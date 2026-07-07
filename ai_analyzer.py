"""
AI 分析模块 - 调用 DeepSeek API（OpenAI 兼容格式）
"""

import os
import sys
import time
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from openai import OpenAI

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

if not DEEPSEEK_API_KEY:
    print("❌ 请在 .env 文件中设置 DEEPSEEK_API_KEY")
    sys.exit(1)

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def analyze(prompt: str, task_name: str = "AI分析", max_retries: int = 3) -> str:
    """调用 DeepSeek API 进行分析，失败自动重试"""
    print(f"🤖 正在执行: {task_name}...")

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是专业的A股短线交易分析师，擅长技术分析、资金面分析、市场情绪研判和板块轮动研究。请严格按照用户要求的格式输出，确保数据准确。对于需要联网获取的信息（新闻、公告等），请自行搜索最新数据。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=8192,
                timeout=120,
            )

            content = response.choices[0].message.content
            token_usage = response.usage

            print(
                f"   ✅ {task_name} 完成 "
                f"(输入: {token_usage.prompt_tokens} tokens, "
                f"输出: {token_usage.completion_tokens} tokens)"
            )
            return content

        except Exception as e:
            print(f"   ⚠️ 第 {attempt}/{max_retries} 次尝试失败: {e}")
            if attempt < max_retries:
                wait_sec = 2 ** attempt
                print(f"   ⏳ 等待 {wait_sec} 秒后重试...")
                time.sleep(wait_sec)
            else:
                print(f"   ❌ {task_name} 彻底失败，已达最大重试次数")
                return f"# {task_name}\n\n> ⚠️ AI 分析失败，请稍后重试。错误: {e}"


def analyze_batch(tasks: list[dict]) -> list[dict]:
    """串行执行多个分析任务"""
    results = []
    for task in tasks:
        result_text = analyze(task["prompt"], task["name"])
        results.append(
            {
                "name": task["name"],
                "output_suffix": task["output_suffix"],
                "content": result_text,
            }
        )
    return results
