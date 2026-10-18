#!/bin/bash
# 网关巡检自查脚本：检查进程存活，掉线拉起并 Telegram 告警
# 建议 cron 每 5 分钟跑一次：
#   */5 * * * * /opt/mickvvwai-extracted/squashfs-root/resources/agent-payload/hermes-agent/gateway/patrol.sh
#
# 告警通过 Bot API 直接发，不依赖网关本身。需要 TELEGRAM_BOT_TOKEN 和 ADMIN_CHAT_ID。

set -e
export HOME=/home/ubuntu
AGENT_DIR="/opt/mickvvwai-extracted/squashfs-root/resources/agent-payload/hermes-agent"
STATE_FILE="/home/ubuntu/.hermes/patrol.state"

# 从 .env 读 token 和管理员 chat_id
ENV_FILE="/home/ubuntu/.hermes/.env"
BOT_TOKEN=""
ADMIN_CHAT_ID=""
if [ -f "$ENV_FILE" ]; then
  BOT_TOKEN=$(grep -E "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
  # 管理员 chat_id 从 config.yaml 的 allow_admin_from 读
  ADMIN_CHAT_ID=$(grep -A2 "allow_admin_from" /home/ubuntu/.hermes/config.yaml 2>/dev/null | grep -oE "[0-9]{6,}" | head -1)
fi

send_tg() {
  local msg="$1"
  [ -z "$BOT_TOKEN" ] && return 0
  [ -z "$ADMIN_CHAT_ID" ] && return 0
  curl -s -m 10 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${ADMIN_CHAT_ID}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 || true
}

is_alive() {
  pgrep -f "gateway.run" >/dev/null 2>&1
}

was_alive="1"
if [ -f "$STATE_FILE" ]; then
  was_alive=$(cat "$STATE_FILE" 2>/dev/null || echo "1")
fi

if is_alive; then
  echo "1" > "$STATE_FILE"
  if [ "$was_alive" = "0" ]; then
    send_tg "✅ 网关已恢复上线 ($(date '+%m-%d %H:%M'))"
  fi
  echo "[patrol] gateway alive"
  exit 0
fi

# 进程掉了
echo "0" > "$STATE_FILE"
if [ "$was_alive" = "1" ]; then
  send_tg "⚠️ 网关掉线，正在尝试拉起 ($(date '+%m-%d %H:%M'))"
fi

echo "[patrol] gateway down, restarting..."
bash "$AGENT_DIR/gateway/start_safe.sh" 2>&1 | tail -5

sleep 10
if is_alive; then
  echo "1" > "$STATE_FILE"
  send_tg "✅ 网关拉起成功 ($(date '+%m-%d %H:%M'))"
  echo "[patrol] restarted OK"
else
  send_tg "❌ 网关拉起失败，请手动检查 ($(date '+%m-%d %H:%M'))"
  echo "[patrol] restart FAILED"
  exit 1
fi
