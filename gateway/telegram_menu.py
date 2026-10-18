# -*- coding: utf-8 -*-
"""Telegram bot menu definitions for the mickvvwai gateway.

Defines the bottom reply keyboard, button→command mapping, inline submenus,
and the sender for inline submenu keyboards.

Conventions (2026-10-17 lessons):
- SUBMENUS uses ENGLISH keys (provider/soul/model/soul_add/soul_list).
  Never use emoji or Chinese as dict keys: the Nezha web terminal mangles
  emoji on paste, which silently breaks key lookup.
- Display text may contain emoji; keys must not.
- send_submenu_inline never raises: it returns False on any failure so the
  caller can fall back to the plain-text command flow.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bottom reply keyboard (7 persistent buttons)
# ---------------------------------------------------------------------------

BTN_ADD_PROVIDER = "➕添加供应商"
BTN_SOUL_MANAGE = "📝提示词管理"
BTN_MODEL_SWITCH = "🔄模型切换"
BTN_SOUL_ADD = "➕添加提示词"
BTN_SOUL_LIST = "🔀切换提示词"
BTN_GATEWAY = "🔄网关重启"
BTN_BACKUP = "💾备份管理"

MAIN_MENU_KEYBOARD = [
    [BTN_ADD_PROVIDER],
    [BTN_SOUL_MANAGE],
    [BTN_MODEL_SWITCH],
    [BTN_SOUL_ADD],
    [BTN_SOUL_LIST],
    [BTN_GATEWAY],
    [BTN_BACKUP],
]

MENU_BUTTON_COMMANDS = {
    BTN_ADD_PROVIDER: "/addprovider",
    BTN_SOUL_MANAGE: "/psoul",
    BTN_MODEL_SWITCH: "/model",
    BTN_SOUL_ADD: "/psoulnew",
    BTN_SOUL_LIST: "/psoullist",
    BTN_GATEWAY: "/pgateway",
    BTN_BACKUP: "/pbackup",
}

# ---------------------------------------------------------------------------
# Inline submenus (English keys)
# ---------------------------------------------------------------------------

# 子菜单定义：键名用英文（代码内部查找用，用户不可见）
# 用户看到的是下面的 title 和按钮文字（中文）
SUBMENUS = {
    "provider": {  # 供应商管理
        "title": "供应商管理",
        "buttons": [
            ("查看供应商", "menu:provider_list"),
            ("添加供应商", "menu:provider_add"),
            ("删除供应商", "menu:provider_del"),
        ],
    },
    "soul": {  # 提示词管理
        "title": "提示词管理",
        "buttons": [
            ("查看提示词", "menu:soul_view"),
            ("修改提示词", "menu:soul_edit"),
            ("追加提示词", "menu:soul_append"),
            ("删除提示词", "menu:soul_clear"),
        ],
    },
    "model": {  # 模型切换
        "title": "模型切换",
        "buttons": [
            ("查看模型", "menu:model_view"),
            ("模型列表", "menu:model_list"),
        ],
    },
    "soul_add": {  # 添加提示词
        "title": "添加提示词",
        "buttons": [
            ("新建提示词", "menu:soul_new"),
            ("查看现有", "menu:soul_list"),
        ],
    },
    "soul_list": {  # 切换提示词
        "title": "切换提示词",
        "buttons": [
            ("提示词列表", "menu:soul_list"),
            ("查看当前", "menu:soul_view"),
        ],
    },
    "gateway": {  # 网关管理
        "title": "网关管理",
        "buttons": [
            ("网关状态", "menu:gateway_status"),
            ("重启网关", "menu:gateway_restart"),
        ],
    },
    "backup": {  # 备份管理
        "title": "备份管理",
        "buttons": [
            ("一键备份", "menu:backup_now"),
            ("备份列表", "menu:backup_list"),
            ("恢复备份", "menu:backup_restore"),
        ],
    },
}


def _get_bot_token() -> Optional[str]:
    """Read the Telegram bot token: env first, then /home/ubuntu/.hermes/.env.

    Never use get_env_value("TELEGRAM_BOT_TOKEN") here: the gateway message
    context cannot read profile secrets and raises UnscopedSecretError.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if token:
        return token.strip()
    for env_path in ("/home/ubuntu/.hermes/.env", os.path.expanduser("~/.hermes/.env")):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"):
                        v = v.strip().strip('"').strip("'")
                        if v:
                            return v
        except Exception:
            continue
    return None


async def send_submenu_inline(chat_id, menu_key: str) -> bool:
    """Send an inline-keyboard submenu to a Telegram chat.

    Returns True on HTTP 200, False on any failure (never raises).
    """
    try:
        submenu = SUBMENUS.get(menu_key)
        if not submenu:
            logger.warning("send_submenu_inline: unknown menu_key=%r", menu_key)
            return False
        token = _get_bot_token()
        if not token:
            logger.warning("send_submenu_inline: no bot token available")
            return False

        buttons = submenu.get("buttons", [])
        # One button per row
        inline_keyboard = [[{"text": text, "callback_data": cb}] for text, cb in buttons]
        payload = json.dumps({
            "chat_id": str(chat_id),
            "text": submenu.get("title", menu_key),
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }).encode("utf-8")

        import asyncio

        def _post():
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status == 200
            except urllib.error.HTTPError as e:
                logger.warning("send_submenu_inline: HTTP %s for key=%s", e.code, menu_key)
                return False
            except Exception as e:
                logger.warning("send_submenu_inline: request failed for key=%s: %s", menu_key, e)
                return False

        return await asyncio.to_thread(_post)
    except Exception as e:
        logger.warning("send_submenu_inline: unexpected failure for key=%s: %s", menu_key, e)
        return False
