#!/bin/bash
# ================================================
# 同步产出文件到 GitHub
# 在 auto 模式跑完后由 cron 自动调用
# ================================================

cd "$(dirname "$0")/.."
TODAY=$(date '+%Y-%m-%d')

# 如果没有产出文件，跳过
if [ ! -d "output/01-Projects/股票复盘" ]; then
    echo "[$TODAY] output 目录为空，跳过 git push"
    exit 0
fi

# 等待 auto 任务完全结束（最多等 60 秒，确保文件写入完成）
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    # 检查是否有 python main.py 进程在运行
    if ! pgrep -f "main.py.*auto" > /dev/null 2>&1; then
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done

# 先 pull 避免 diverged branch
git pull --no-rebase origin main 2>&1 || true

# git add（保留 stderr 以便排查问题）
git add output/ tracking-db.json 2>&1

# 检查是否有变更
if git diff --cached --quiet; then
    # 兜底：用 git status 排查为什么没有变更
    echo "[$TODAY] 无文件变更，跳过 git push"
    echo "--- git status 诊断 ---"
    git status --porcelain output/ 2>&1
    exit 0
fi

git commit -m "自动推送: ${TODAY} 股票复盘产出" --quiet 2>&1
git push origin main --quiet 2>&1

echo "[$TODAY] 产出文件已推送到 GitHub"
