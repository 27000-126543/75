#!/bin/bash
echo "========================================="
echo "  智慧矿山安全生产与智能调度系统"
echo "========================================="
echo ""

echo "[1/3] 检查 Python 环境..."
python3 --version

echo ""
echo "[2/3] 安装依赖..."
pip3 install -r requirements.txt

echo ""
echo "[3/3] 启动服务..."
echo "访问地址: http://localhost:8000"
echo "API文档: http://localhost:8000/docs"
echo ""

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
