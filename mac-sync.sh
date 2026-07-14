#!/bin/bash
# ================================================
# Mac 端同步脚本
# 从阿里云服务器 rsync 产出文件 → Obsidian vault
# 可以手动运行，也可以配 launchd 每小时自动跑
# ================================================
set -e

SSH_ALIAS="aliyun-stock"
SERVER_OUTPUT="/root/stock-review/output/01-Projects/"
OBSIDIAN_VAULT="$HOME/Documents/Obsidian Vault/01-Projects/"

echo "📥 从服务器同步产出文件..."
rsync -avz -e ssh "$SSH_ALIAS:$SERVER_OUTPUT" "$OBSIDIAN_VAULT"

echo "✅ 同步完成"
