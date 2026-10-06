#!/usr/bin/env bash
# ==============================================================================
# WorkBuddy2API-Hub 一键安装与快速更新脚本
# 支持系统: Linux, macOS, NAS (fnOS / 群晖 / 威联通 / Unraid 等)
# 用法:
#   首次部署: curl -fsSL https://raw.githubusercontent.com/ardeyouxipianyi/workbuddy2api-hub/main/quick-deploy.sh | bash
#   快速更新: 再次运行相同的命令即可完成平滑无感升级（账号配置绝不丢失）
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "\n${BLUE}======================================================${NC}"
echo -e "${GREEN}   WorkBuddy2API-Hub Docker 一键部署与更新脚本${NC}"
echo -e "${BLUE}======================================================${NC}\n"

# 1. 检查 Docker 环境
if ! command -v docker >/dev/null 2>&1; then
    echo -e "${RED}[错误] 未检测到 Docker，请先安装 Docker 后再运行此脚本。${NC}"
    exit 1
fi

# 检查 Docker Compose 支持
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[错误] 未检测到 Docker Compose 支持 (docker compose / docker-compose)。${NC}"
    exit 1
fi

# 2. 确定工作目录
APP_DIR="workbuddy2api-hub"
if [ -f "docker-compose.yml" ] && grep -q "wb-proxy" docker-compose.yml 2>/dev/null; then
    TARGET_DIR="$(pwd)"
    echo -e "${BLUE}[信息] 在当前目录检测到项目配置: ${TARGET_DIR}${NC}"
else
    TARGET_DIR="$(pwd)/${APP_DIR}"
    if [ ! -d "${TARGET_DIR}" ]; then
        echo -e "${BLUE}[信息] 创建安装目录: ${TARGET_DIR}${NC}"
        mkdir -p "${TARGET_DIR}"
    fi
    cd "${TARGET_DIR}"
fi

# 3. 首次安装 vs 更新判断
if [ -f "docker-compose.yml" ]; then
    echo -e "${YELLOW}[升级模式] 检测到已有部署，正在拉取最新镜像进行平滑更新...${NC}"
    $COMPOSE_CMD pull
    $COMPOSE_CMD up -d
    echo -e "\n${GREEN}[成功] WorkBuddy2API-Hub 已成功更新至最新版本！${NC}"
else
    echo -e "${BLUE}[安装模式] 正在进行首次初始化部署...${NC}"
    cat > docker-compose.yml << 'EOF'
services:
  wb-proxy:
    image: ghcr.io/ardeyouxipianyi/workbuddy2api-hub:latest
    container_name: wb-proxy
    restart: unless-stopped
    ports:
      - "8788:8788"
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      # - API_KEY=your_secret_key
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts
      - ./usage:/app/usage
EOF
    mkdir -p accounts usage
    echo -e "${BLUE}[信息] 正在拉取镜像并启动容器...${NC}"
    $COMPOSE_CMD up -d
    echo -e "\n${GREEN}[成功] WorkBuddy2API-Hub 首次部署成功！${NC}"
fi

# 4. 获取本地 IP 并输出引导提示
LOCAL_IP="127.0.0.1"
if command -v hostname >/dev/null 2>&1; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
fi

echo -e "\n${BLUE}------------------------------------------------------${NC}"
echo -e "${GREEN}Web 控制看板:${NC} http://${LOCAL_IP:-127.0.0.1}:8788"
echo -e "${GREEN}OpenAI 接口:${NC}  http://${LOCAL_IP:-127.0.0.1}:8788/v1"
echo -e "${BLUE}------------------------------------------------------${NC}"
echo -e "${YELLOW}查看初始 API Key:${NC}  $COMPOSE_CMD logs wb-proxy | grep -i 'api key'"
echo -e "${YELLOW}查看实时运行日志:${NC}  $COMPOSE_CMD logs -f wb-proxy"
echo -e "${YELLOW}后续如何更新:${NC}      再次运行本脚本，或执行: $COMPOSE_CMD pull && $COMPOSE_CMD up -d"
echo -e "${BLUE}======================================================${NC}\n"
