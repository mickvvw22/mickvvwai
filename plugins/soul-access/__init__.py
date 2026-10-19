"""soul-access plugin — manage the bot persona prompt from AI chat (self-contained).

Exposes the ``manage_soul`` tool so the agent can view / replace / append /
clear the persona prompt (``$HERMES_HOME/SOUL.md``) conversationally.
Self-contained: backend logic is inlined (no hermes_cli.soul_access needed),
only the password gate is imported from hermes_cli.provider_access.

Security rules (do not weaken):
- The access password travels only as the ``password`` argument; the handler
  verifies it with ``verify_password`` and NEVER echoes it back.
- Mutations are owner-gated by the approval call below.
- Delete additionally requires the ``allow_delete`` setting (off by default).
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "manage_soul"

_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "View or change the bot persona prompt (SOUL.md — the bot's role/persona "
        "instructions). "
        "Actions 'set', 'add' and 'delete' ALWAYS require the provider-access "
        "password in the 'password' argument — ask the user for it conversationally "
        "first and never repeat it back in any reply. "
        "For 'view': returns the current prompt text (no password needed). "
        "For 'set': 'content' replaces the whole prompt file. "
        "For 'add': 'content' is appended to the end of the prompt file. "
        "For 'delete': clears the prompt file (works only when the owner enabled "
        "deletion in settings). "
        "Tell the user to restart the gateway afterwards so the new prompt takes effect."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["view", "set", "add", "delete"],
                       "description": "What to do."},
            "content": {"type": "string",
                        "description": "New prompt text (set: replaces all; add: appended)."},
            "password": {"type": "string",
                         "description": "Provider-access password. Required for set/add/delete. Never repeat it back."},
        },
        "required": ["action"],
    },
}


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _get_soul_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        import os

        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return home / "SOUL.md"


def _backup(path: Path) -> Optional[str]:
    try:
        if not path.exists():
            return None
        backup = path.with_name(f"{path.name}.{int(time.time())}.bak")
        shutil.copy2(path, backup)
        return str(backup)
    except Exception as exc:
        logger.warning("soul-access: backup failed: %s", exc)
        return None


def _gate_password(password: Any) -> tuple[bool, str]:
    from hermes_cli import provider_access as pa

    return pa.verify_password(password or "")


def _handle_manage_soul(args: dict[str, Any], **kwargs: Any) -> str:
    action = str((args or {}).get("action") or "").strip().lower()
    path = _get_soul_path()

    if action == "view":
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            return json.dumps({"ok": True, "exists": path.exists(), "content": text},
                              ensure_ascii=False)
        except Exception:
            return _err("read failed")

    if action not in {"set", "add", "delete"}:
        return _err(f"unknown action: {action}")

    ok, reason = _gate_password((args or {}).get("password"))
    if not ok:
        if reason.startswith("locked:"):
            return _err(f"password locked, try again in {reason.split(':', 1)[1]} seconds")
        return _err("wrong password")

    if action == "delete":
        from hermes_cli import provider_access as pa

        if not pa.is_delete_allowed():
            return _err("deletion is disabled in settings (allow_delete is off)")

    try:
        from tools.approval import request_tool_approval

        verdict = request_tool_approval(
            TOOL_NAME,
            f"manage_soul {action} prompt file",
            rule_key=f"soul-access:{action}",
        )
    except Exception as exc:
        logger.warning("soul-access: approval gate error: %s", exc)
        return _err("approval gate unavailable")
    if not (isinstance(verdict, dict) and verdict.get("approved") is True):
        return _err("denied by approval gate")

    try:
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if action == "delete":
            if path.exists():
                path.write_text("", encoding="utf-8")
            return json.dumps({"ok": True}, ensure_ascii=False)
        elif action == "add":
            content = str((args or {}).get("content") or "")
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            sep = "" if not existing or existing.endswith("\n") else "\n"
            new_text = existing + sep + content + "\n"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            tmp.replace(path)
            return json.dumps({"ok": True, "chars": len(new_text)}, ensure_ascii=False)
        else:  # set
            content = str((args or {}).get("content") or "")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)
            return json.dumps({"ok": True, "chars": len(content)}, ensure_ascii=False)
    except Exception as exc:
        logger.warning("soul-access: %s failed: %s", action, exc)
        return _err("operation failed")


def _check_requirements() -> bool:
    try:
        from hermes_cli import provider_access as pa  # noqa: F401

        return True
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(name=TOOL_NAME, toolset="soul_access", schema=_SCHEMA,
                      handler=_handle_manage_soul, check_fn=_check_requirements,
                      emoji="🎭")
