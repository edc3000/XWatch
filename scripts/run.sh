#!/bin/bash
# XWatch 启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "❌ 未找到 .env 配置文件"
    echo "请复制 .env.example 为 .env 并填入配置"
    echo "  cp .env.example .env"
    exit 1
fi

# 检查依赖
if ! python -c "import requests, telegram, dotenv, watchdog" 2>/dev/null; then
    echo "📦 安装依赖..."
    pip install -r requirements.txt
fi

echo "🚀 启动 XWatch..."
python -m src.main
