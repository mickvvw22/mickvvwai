#!/usr/bin/env bash
# bot-quickstart.sh — Telegram 机器人快速接入口
#
# 给哪吒面板终端 / SSH 粘贴用的：一行命令完成机器人 token 配置。
# 用法:
#   bash scripts/bot-quickstart.sh <BOT_TOKEN> [ALLOWED_USER_IDS]
#
#   BOT_TOKEN        去 Telegram 找 @BotFather 创建机器人拿到的 token
#   ALLOWED_USER_IDS 可选，你的 Telegram 数字 ID（去 @userinfobot 查），
#                    多个用英文逗号隔开。不填则先不限制，稍后可补。
#
# 做的事情:
#   1. 调 Telegram getMe 校验 token 是否有效
#   2. hermes config set TELEGRAM_BOT_TOKEN 写入配置
#   3. 可选写入 TELEGRAM_ALLOWED_USERS
#   4. 打印下一步（启动网关）
set -euo pipefail

TOKEN="${1:-}"
ALLOW="${2:-}"

if [[ -z "$TOKEN" ]]; then
  echo "用法: bash scripts/bot-quickstart.sh <BOT_TOKEN> [ALLOWED_USER_IDS]"
  echo ""
  echo "  1. 去 Telegram 找 @BotFather，/newbot 创建机器人，拿到 token"
  echo "  2. 去 @userinfobot 查你的数字 ID（可选，不填先不限制）"
  echo "  3. 粘贴运行: bash scripts/bot-quickstart.sh 123456:ABC... 987654321"
  exit 1
fi

if ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "❌ token 格式不对，应为 数字:字符串 的形式，请检查后重试"
  exit 1
fi

echo "→ 正在校验 token…"
ME="$(curl -fsSL --max-time 25 "https://api.telegram.org/bot${TOKEN}/getMe")" || {
  echo "❌ token 无效，或本机连不上 api.telegram.org，请检查网络后重试"
  exit 1
}
BOT_USER="$(printf '%s' "$ME" | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4)"
if [[ -z "$BOT_USER" ]]; then
  echo "❌ Telegram 返回异常，token 可能无效"
  exit 1
fi
echo "✓ token 有效，机器人: @${BOT_USER}"

echo "→ 写入配置…"
hermes config set TELEGRAM_BOT_TOKEN "$TOKEN"
echo "✓ TELEGRAM_BOT_TOKEN 已保存"

if [[ -n "$ALLOW" ]]; then
  hermes config set TELEGRAM_ALLOWED_USERS "$ALLOW"
  echo "✓ 允许用户已设为: $ALLOW"
else
  echo "⚠ 未限制允许用户。建议去 @userinfobot 查你的数字 ID 后补上："
  echo "    hermes config set TELEGRAM_ALLOWED_USERS <你的数字ID>"
fi

echo ""
echo "✓ 机器人配置完成！下一步："
echo "    hermes gateway start"
echo "  然后去 Telegram 给 @${BOT_USER} 发任意消息试试。"
echo ""
echo "  查看状态: hermes gateway status"
echo "  停止网关: hermes gateway stop"
