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

BTN_ADD_PROVIDER = "/addprovider 添加供应商"
BTN_SOUL_MANAGE = "/psoul 提示词管理"
BTN_MODEL_SWITCH = "/model 模型切换"
BTN_SOUL_ADD = "/psoulnew 添加提示词"
BTN_SOUL_LIST = "/psoullist 切换提示词"
BTN_GATEWAY = "/pgateway 网关重启"
BTN_BACKUP = "/pbackup 备份管理"
BTN_MEMBERS = "/pmembers 成员管理"      # 2026-10-19：第 8 个常驻按钮（放在「备份管理」之后）

MAIN_MENU_KEYBOARD = [
    [BTN_ADD_PROVIDER],
    [BTN_SOUL_MANAGE],
    [BTN_MODEL_SWITCH],
    [BTN_SOUL_ADD],
    [BTN_SOUL_LIST],
    [BTN_GATEWAY],
    [BTN_BACKUP],
    [BTN_MEMBERS],
]

# 按钮标签本身就是可执行命令（"/<命令> <中文说明>"）：点一下 = 用户手打这条命令，
# 走 Telegram 的 COMMAND 通道 → 与「手打命令」完全同一条已验证可用的代码路径。
# 说明文字属于「多余参数」，各处理函数会忽略它（见 slash_commands_model.py 里的注释）。
MENU_BUTTON_COMMANDS = {
    BTN_ADD_PROVIDER: "/addprovider",
    BTN_SOUL_MANAGE: "/psoul",
    BTN_MODEL_SWITCH: "/model",
    BTN_SOUL_ADD: "/psoulnew",
    BTN_SOUL_LIST: "/psoullist",
    BTN_GATEWAY: "/pgateway",
    BTN_BACKUP: "/pbackup",
    BTN_MEMBERS: "/pmembers",
}

# ---------------------------------------------------------------------------
# Inline submenus (English keys)
# ---------------------------------------------------------------------------

# 子菜单定义：键名用英文（代码内部查找用，用户不可见）
# 用户看到的是 title / body / 按钮文字（中文）。
# body = 「具体用法」说明，显示在按钮**上方**；按钮每行两个，布局统一整齐。
SUBMENUS = {
    "members": {  # 成员管理（2026-10-19，底部常驻第 8 个按钮）
        "title": "成员管理",
        "body": ("• 最近在线成员 —— 谁在线、谁被下线，一目了然\n"
                 "• 拉黑成员 —— 永久下线，需你手动恢复\n"
                 "• 下线成员 —— 限时下线（点一下默认 60 分钟）\n"
                 "• 恢复成员 —— 解除下线/拉黑，放行"),
        "buttons": [
            ("最近在线成员", "menu:members_online"),
            ("拉黑成员", "menu:members_black"),
            ("下线成员", "menu:members_block"),
            ("恢复成员", "menu:members_restore"),
        ],
    },
    "provider": {  # 供应商管理
        "title": "供应商管理",
        "body": ("• 查看供应商 —— 列出已配置的供应商和各自模型数\n"
                 "• 添加供应商 —— 按引导加一个模型供应商\n"
                 "• 删除供应商 —— 删掉指定供应商（需口令）"),
        "buttons": [
            ("查看供应商", "menu:provider_list"),
            ("添加供应商", "menu:provider_add"),
            ("删除供应商", "menu:provider_del"),
        ],
    },
    "soul": {  # 提示词管理
        "title": "提示词管理",
        "body": ("• 查看提示词 —— 显示当前生效的提示词全文\n"
                 "• 修改提示词 —— 整段替换（需口令）\n"
                 "• 追加提示词 —— 在末尾追加内容（需口令）\n"
                 "• 删除提示词 —— 删掉指定提示词（需口令）"),
        "buttons": [
            ("查看提示词", "menu:soul_view"),
            ("修改提示词", "menu:soul_edit"),
            ("追加提示词", "menu:soul_append"),
            ("删除提示词", "menu:soul_clear"),
        ],
    },
    "model": {  # 模型切换
        "title": "模型切换",
        "body": ("• 查看模型 —— 打开「供应商 → 模型」选择器，点一下就切\n"
                 "• 模型列表 —— 列出当前供应商可选的模型名\n"
                 "• 推理强度 —— 关闭/最低/低/中/高/超高/最大/极致，点一下即生效"),
        "buttons": [
            ("查看模型", "menu:model_view"),
            ("模型列表", "menu:model_list"),
            ("推理强度", "menu:model_reasoning"),
        ],
    },
    "soul_add": {  # 添加提示词
        "title": "添加提示词",
        "body": ("• 新建提示词 —— 建一个空提示词，再用 /psoulset 写内容（需口令）\n"
                 "• 查看现有 —— 看看已经有哪些提示词"),
        "buttons": [
            ("新建提示词", "menu:soul_new"),
            ("查看现有", "menu:soul_list"),
        ],
    },
    "soul_list": {  # 切换提示词
        "title": "切换提示词",
        "body": ("• 提示词列表 —— 列出全部提示词（带 ← 标记当前）\n"
                 "• 查看当前 —— 显示当前提示词正文\n"
                 "切换用：/psoulswitch <名称>（需口令）"),
        "buttons": [
            ("提示词列表", "menu:soul_list"),
            ("查看当前", "menu:soul_view"),
        ],
    },
    "gateway": {  # 网关管理
        "title": "网关管理",
        "body": ("• 网关状态 —— 看网关进程是否在跑（含 pid）\n"
                 "• 重启网关 —— 直接重启：先给你一条提示，重启完成后会再通知你"),
        "buttons": [
            ("网关状态", "menu:gateway_status"),
            ("重启网关", "menu:gateway_restart"),
        ],
    },
    "backup": {  # 备份管理
        "title": "备份管理",
        "body": ("备份走你自己的 WebDAV 网盘（没配置会提示怎么配）\n"
                 "• 一键备份 —— 打包上传（需口令）\n"
                 "• 备份列表 —— 列出远程已有备份\n"
                 "• 恢复备份 —— /prestore <文件名>"),
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
    return await _send_inline_payload(chat_id, menu_key)


def command_for_button_text(text: Optional[str]) -> Optional[str]:
    """底部常驻按钮被按下时，把按钮文字翻成对应命令。

    reply keyboard 的按钮**不会**触发 callback —— Telegram 只是把按钮标签当成一条普通
    文本消息发上来。所以要在入站处按 MENU_BUTTON_COMMANDS 把它改写成命令，否则它会被
    当成聊天内容丢给模型。

    先精确匹配；再忽略 emoji/空白/标点匹配一次（防某些客户端把 emoji 弄丢）。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    exact = MENU_BUTTON_COMMANDS.get(raw)
    if exact:
        return exact

    def _norm(s: str) -> str:
        return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    target = _norm(raw)
    if not target:
        return None
    for label, cmd in MENU_BUTTON_COMMANDS.items():
        if _norm(label) == target:
            return cmd
    return None


async def _send_inline_payload(chat_id, menu_key: str) -> bool:
    """真正的实现（send_submenu_inline 转发到这里）。HTTP 200 → True，任何失败 → False，从不抛异常。"""
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
        # 布局：每行两个按钮（末行单个居中不了，但视觉上统一；参考用户给的样例排版）
        inline_keyboard = [
            [{"text": label, "callback_data": cb} for label, cb in buttons[i:i + 2]]
            for i in range(0, len(buttons), 2)
        ]
        # 文案：标题 + 分隔线 + 具体用法（用法在按钮上方，符合用户要求的「上面讲用法，下面按钮」）
        text = submenu.get("title", menu_key)
        body = (submenu.get("body") or "").strip()
        if body:
            text = f"{text}\n{'─' * 14}\n{body}"
        payload = json.dumps({
            "chat_id": str(chat_id),
            "text": text,
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


# ---------------------------------------------------------------------------
# 底部常驻按钮（reply keyboard）—— 2026-10-18 新增
#
# 为什么用它：inline 按钮要经过 callback_query → 再注入命令，链路长；而 reply keyboard
# 的按钮**点一下就等于用户手打了那条命令**，走的是最普通的消息路径，最不容易出问题。
# ---------------------------------------------------------------------------

_REPLY_KEYBOARD_LAYOUT = (
    (BTN_ADD_PROVIDER, BTN_SOUL_MANAGE),
    (BTN_MODEL_SWITCH, BTN_SOUL_ADD),
    (BTN_SOUL_LIST, BTN_GATEWAY),
    (BTN_BACKUP, BTN_MEMBERS),
)


def button_label_text(command: str) -> str:
    """该命令对应的底部按钮说明文字（``"/psoul 提示词管理"`` → ``"提示词管理"``）。

    用途：底部按钮的标签形如 ``/psoul 提示词管理``，点一下发上来的是整串文本，命令参数里
    会带上一句说明。处理函数据此把这句说明当作「没有参数」处理，避免被误当成真实参数
    （否则 /psoulnew 会把「添加提示词」当成提示词名字去创建）。
    """
    for label, cmd in MENU_BUTTON_COMMANDS.items():
        if cmd == command:
            parts = label.split(" ", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def _menu_state_file():
    from pathlib import Path
    import os as _os
    home = _os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "gateway" / "menu_state.json"


def menu_shown(chat_id) -> bool:
    """该会话的常驻菜单当前是显示还是收起（默认显示）。"""
    import json
    from pathlib import Path
    try:
        p = _menu_state_file()
        data = json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}
        return bool(data.get(str(chat_id), True))
    except Exception:
        return True


def _remember_menu_state(chat_id, shown: bool) -> None:
    import json
    try:
        p = _menu_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}
        data[str(chat_id)] = bool(shown)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


async def send_reply_keyboard(chat_id, *, show: bool = True) -> bool:
    """显示 / 收起底部常驻按钮。HTTP 200 返回 True，任何失败返回 False（从不抛异常）。

    2026-10-19：顺手记住该会话的显示状态 —— 「/pmenu 不带参数」据此做成一键收纳/展开开关。
    """
    try:
        token = _get_bot_token()
        if not token:
            logger.warning("send_reply_keyboard: no bot token available")
            return False
        if show:
            reply_markup = {
                "keyboard": [[{"text": label} for label in row] for row in _REPLY_KEYBOARD_LAYOUT],
                "resize_keyboard": True,
                "is_persistent": True,
            }
            text = "菜单已展开 ✅ 点下面按钮直接用；再点左侧 ≡ → /pmenu 就收起"
        else:
            reply_markup = {"remove_keyboard": True}
            text = "菜单已收起 📥 想再叫出来：点输入框左边的 ≡ → /pmenu（一点就回来）"
        payload = json.dumps({
            "chat_id": str(chat_id), "text": text, "reply_markup": reply_markup,
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
            except Exception as e:
                logger.warning("send_reply_keyboard: request failed: %s", e)
                return False

        ok = await asyncio.to_thread(_post)
        if ok:
            _remember_menu_state(chat_id, show)
        return ok
    except Exception as e:
        logger.warning("send_reply_keyboard: unexpected failure: %s", e)
        return False
