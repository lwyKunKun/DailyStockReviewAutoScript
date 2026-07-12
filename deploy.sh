#!/bin/bash
# ================================================
# 股票复盘 · 部署脚本（自动备份 + 拉取最新代码）
# 用法: bash deploy.sh [服务器别名，默认 aliyun-stock]
# ================================================
set -e

SERVER="${1:-aliyun-stock}"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_DIR=~/stock-review-backups/${TIMESTAMP}

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  股票复盘 · 部署到服务器${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# ---- 1. 推送本地代码到 GitHub ----
echo -e "${YELLOW}[1/4] 推送代码到 GitHub...${NC}"
git add -A
if git diff --cached --quiet; then
    echo "  没有需要提交的改动"
else
    git commit -m "部署: ${TIMESTAMP}" || echo "  提交被跳过（可能没有改动）"
fi
git push origin main
echo -e "${GREEN}  GitHub 推送完成${NC}"
echo ""

# ---- 2. 备份服务器当前代码 ----
echo -e "${YELLOW}[2/4] 备份服务器当前代码...${NC}"
ssh "${SERVER}" "
    mkdir -p ${BACKUP_DIR} && \
    if [ -d ~/stock-review ]; then
        cp -r ~/stock-review/* ${BACKUP_DIR}/ 2>/dev/null && \
        echo '  备份完成: ${BACKUP_DIR}'
    else
        echo '  ⚠️ 服务器上无 stock-review 目录，跳过备份'
    fi
"
echo ""

# ---- 3. 拉取最新代码 ----
echo -e "${YELLOW}[3/4] 服务器拉取最新代码...${NC}"
ssh "${SERVER}" "
    cd ~/stock-review && \
    git fetch origin && \
    git reset --hard origin/main
"
echo -e "${GREEN}  代码同步完成${NC}"
echo ""

# ---- 4. 验证 ----
echo -e "${YELLOW}[4/4] 验证部署结果...${NC}"
ssh "${SERVER}" "
    cd ~/stock-review && \
    echo '  最新提交:' && \
    git log --oneline -1 && \
    echo '' && \
    echo '  文件列表:' && \
    ls *.py
"
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  部署完成${NC}"
echo -e "${GREEN}  备份位置: ${BACKUP_DIR}${NC}"
echo -e "${GREEN}========================================${NC}"
