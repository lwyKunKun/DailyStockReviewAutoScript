#!/bin/bash
# ================================================
# 同步产出文件到 GitHub
# 在 docker/daily 模式跑完后由 cron 自动调用
# ================================================
set -e

cd "$(dirname "$0")/.."
TODAY=$(date '+%Y-%m-%d')

# 如果没有产出文件，跳过
if [ ! -d "output/01-Projects/股票复盘" ]; then
    echo "[$TODAY] output 目录为空，跳过 git push"
    exit 0
fi

git add output/ tracking-db.json 2>/dev/null || true

# 检查是否有变更
if git diff --cached --quiet; then
    echo "[$TODAY] 无文件变更，跳过 git push"
    exit 0
fi

git commit -m "自动推送: ${TODAY} 股票复盘产出" --quiet
git push origin main --quiet

echo "[$TODAY] 产出文件已推送到 GitHub"
