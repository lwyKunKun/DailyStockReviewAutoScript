#!/bin/bash
# ================================================
# 股票复盘 · 云服务器一键部署
# 适用: Ubuntu 22.04 / 24.04 (腾讯云/阿里云轻量服务器)
# 用法: 购买服务器并 SSH 登录后，运行:
#   bash <(curl -s https://raw.githubusercontent.com/lwyKunKun/DailyStockReviewAutoScript/main/server/setup.sh)
# 或手动:
#   git clone https://github.com/lwyKunKun/DailyStockReviewAutoScript.git ~/stock-review
#   cd ~/stock-review && bash server/setup.sh
# ================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  股票复盘 · 云服务器一键部署${NC}"
echo -e "${GREEN}========================================${NC}"

# ---- 1. 基础环境 ----
echo -e "\n${YELLOW}[1/6] 安装基础软件包...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip git curl 2>&1 | tail -1

# ---- 2. 设置时区为北京时间 ----
echo -e "\n${YELLOW}[2/6] 设置时区为北京时间...${NC}"
sudo timedatectl set-timezone Asia/Shanghai
echo "  当前时间: $(date '+%Y-%m-%d %H:%M:%S')"

# ---- 3. 安装 Python 依赖 ----
echo -e "\n${YELLOW}[3/6] 安装 Python 依赖...${NC}"
pip3 install --user akshare pandas requests python-dotenv openai 2>&1 | tail -1

# ---- 4. 配置 .env ----
echo -e "\n${YELLOW}[4/6] 配置环境变量...${NC}"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  ⚠️  请编辑 .env 填入你的 DeepSeek API Key:"
    echo "     nano ~/stock-review/.env"
    echo ""
    echo "  获取 API Key: https://platform.deepseek.com/api_keys"
    echo ""
    echo "  修改以下两行:"
    echo "    DEEPSEEK_API_KEY=sk-your-key-here"
    echo "    OBSIDIAN_VAULT=$(pwd)/output"
    echo ""
else
    echo "  .env 已存在，跳过"
fi

# ---- 5. 配置 Git 远程推送 ----
echo -e "\n${YELLOW}[5/6] 配置 Git SSH 推送...${NC}"
if [ ! -f ~/.ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -C "stock-review-server" -f ~/.ssh/id_ed25519 -N "" -q
    echo ""
    echo "  ⚠️  请将以下 SSH 公钥添加到 GitHub:"
    echo "     https://github.com/lwyKunKun/DailyStockReviewAutoScript/settings/keys/new"
    echo ""
    cat ~/.ssh/id_ed25519.pub
    echo ""
fi
git remote set-url origin git@github.com:lwyKunKun/DailyStockReviewAutoScript.git 2>/dev/null || true

# ---- 6. 配置 cron 定时任务 ----
echo -e "\n${YELLOW}[6/6] 配置定时任务...${NC}"
CRON_FILE="$SCRIPT_DIR/server/crontab.txt"
if [ -f "$CRON_FILE" ]; then
    crontab "$CRON_FILE"
    echo "  Cron 已配置:"
    crontab -l
else
    echo "  ⚠️  crontab.txt 不存在，手动创建"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  部署完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  下一步:"
echo "  1. 编辑 .env:        nano ~/stock-review/.env"
echo "  2. 添加 SSH Key:     https://github.com/.../settings/keys/new"
echo "  3. 等待 cron 自动运行，或手动测试:"
echo "     cd ~/stock-review && python3 main.py --mode fetch"
echo ""
echo "  Cron 时间表:"
echo "     15:00  数据采集  (fetch)"
echo "     17:30  龙虎榜补充 (dragon)"
echo "     19:00  AI分析输出 (daily)"
echo "     19:05  推送GitHub"
echo ""
