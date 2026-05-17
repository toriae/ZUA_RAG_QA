#!/usr/bin/env bash
# ZUA 招生咨询助手 - 一键启动脚本
# 使用前请确保已安装依赖: pip install -r requirements.txt
# 并已将 .env.example 复制为 .env 并填写了 API Key

set -e

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "⚠️  未找到 .env 配置文件"
    echo "📋 正在从 .env.example 创建 .env ..."
    cp .env.example .env
    echo ""
    echo "❌ 请先编辑 .env 文件，填写你的 API Key，然后重新运行此脚本"
    exit 1
fi

# 检查 API Key
if grep -q "your-api-key-here" .env 2>/dev/null; then
    echo "⚠️  .env 中的 API Key 似乎还是占位符"
    echo "📝 请编辑 .env 文件，将 ZUA_API_KEY 设置为你的真实 API Key"
    exit 1
fi

echo "🚀 启动 ZUA 招生咨询助手..."
echo "🌐 访问地址: http://localhost:8012"
echo "🔧 管理面板: http://localhost:8012/v1/admin"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

# 设置 PYTHONPATH 确保 utils 模块能被正确导入
export PYTHONPATH="$(pwd)"

# 启动 uvicorn 开发服务器
python3 -m uvicorn utils.main:app --host 0.0.0.0 --port 8012 --reload
