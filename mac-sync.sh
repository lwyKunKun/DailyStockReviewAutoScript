#!/bin/bash
# ================================================
# Mac 端同步脚本
# 从 GitHub pull 产出文件 → 复制到 Obsidian vault
# 可以手动运行，也可以配 launchd 每小时自动跑
# ================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$SCRIPT_DIR" != "$HOME/stock-review" ]; then
    cd "$HOME/stock-review"
else
    cd "$SCRIPT_DIR"
fi

OBSIDIAN_VAULT="$HOME/Documents/Obsidian Vault"

echo "📥 拉取最新产出..."
git pull origin main --quiet

if [ -d "output/01-Projects/股票复盘" ]; then
    echo "📁 同步到 Obsidian..."
    rsync -av --quiet output/01-Projects/ "$OBSIDIAN_VAULT/01-Projects/"
    echo "✅ 同步完成"
else
    echo "⏭️  无产出文件"
fi
