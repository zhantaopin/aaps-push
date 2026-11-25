#!/usr/bin/env bash
# AAPS 推送服务 v04 一键部署脚本
# 用法:
#   ./deploy.sh                 # 默认端口 8300，未指定密码时随机生成并打印
#   PORT=9000 ./deploy.sh       # 指定端口
#   ADMIN_PASSWORD=secret PORT=9000 ./deploy.sh
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8300}"
export PORT
if [ -z "${ADMIN_PASSWORD}" ]; then
    ADMIN_PASSWORD="$(openssl rand -hex 8)"
    echo "未指定 ADMIN_PASSWORD，已随机生成: ${ADMIN_PASSWORD}"
    echo "（已写入 .env，请妥善保存）"
fi

# 写入 .env 供 docker-compose 使用
cat > .env <<EOF
PORT=${PORT}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
EOF

if command -v docker >/dev/null 2>&1; then
    echo "==> 检测到 Docker，开始构建并启动..."
    if docker compose version >/dev/null 2>&1; then
        docker compose up -d --build
    else
        docker-compose up -d --build
    fi
    echo ""
    echo "✅ 部署完成!"
    echo "   管理台: http://<服务器IP>:${PORT}"
    echo "   健康检查: http://127.0.0.1:${PORT}/api/health"
    echo "   管理密码: ${ADMIN_PASSWORD}（见 .env）"
    echo "   配置/数据目录: $(pwd)/data"
    echo ""
    echo "   查看日志:   docker logs -f aaps-push"
    echo "   停止服务:   docker compose down"
else
    echo "==> 未检测到 Docker，使用本地 Python 运行 (Ctrl+C 停止)"
    python3 -m pip install -r requirements.txt
    DATA_DIR="$(pwd)/data" ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
        python3 -m app.main --port "${PORT}"
fi
