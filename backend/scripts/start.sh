#!/bin/sh
# 启动流程:等待数据库 -> 配置预检 -> 迁移 -> 种子初始化 -> 启动 API
set -e

echo "[opssop] 启动前配置预检(不连接数据库)..."
python scripts/preflight.py

echo "[opssop] 执行数据库迁移..."
alembic upgrade head

echo "[opssop] 种子初始化(幂等)..."
python -c "from app.seed import ensure_seed; ensure_seed()"

echo "[opssop] 启动 API 服务..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
