#!/bin/bash
# 安全启动网关：先杀掉已有的 gateway.run 进程，避免抢端口/重复拉起
# 用法（以 ubuntu 用户）：
#   export HOME=/home/ubuntu
#   bash /opt/mickvvwai-extracted/squashfs-root/resources/agent-payload/hermes-agent/gateway/start_safe.sh

set -e
export HOME=/home/ubuntu
AGENT_DIR="/opt/mickvvwai-extracted/squashfs-root/resources/agent-payload/hermes-agent"

echo "[start_safe] 停止已有的 gateway.run 进程..."
pkill -f "gateway.run" 2>/dev/null || true
sleep 3
# 二次确认
if pgrep -f "gateway.run" >/dev/null 2>&1; then
  echo "[start_safe] 还有残留进程，强制杀掉..."
  pkill -9 -f "gateway.run" 2>/dev/null || true
  sleep 2
fi

echo "[start_safe] 清理 __pycache__..."
find "$AGENT_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "[start_safe] 启动网关..."
cd "$AGENT_DIR"
HERMES_SITE="$AGENT_DIR" PYTHONPATH="$AGENT_DIR" \
  nohup venv/bin/python -m gateway.run > /home/ubuntu/nohup.out 2>&1 &
NEW_PID=$!
sleep 5
if kill -0 $NEW_PID 2>/dev/null; then
  echo "[start_safe] 网关已启动 pid=$NEW_PID，日志: /home/ubuntu/nohup.out"
else
  echo "[start_safe] 启动失败，看日志: tail -50 /home/ubuntu/nohup.out"
  exit 1
fi
