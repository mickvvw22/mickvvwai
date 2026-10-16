"""soul-access plugin — manage the bot persona prompt from AI chat.

Exposes the ``manage_soul`` tool so the agent can view / replace / append /
clear the persona prompt (``$HERMES_HOME/SOUL.md``) conversationally
("把提示词改成..."). Every mutating action goes through the SAME password gate
as provider-access (``hermes_cli.provider_access.verify_password``) plus the
standard tool approval gate (``tools.approval.request_tool_approval``).

Security rules (do not weaken):
- The access password travels only as the ``password`` argument; the handler
  verifies it with ``verify_password`` and NEVER echoes it back, in success
  or error text.
- Mutations are owner-gated by the approval call below; conversational
  "confirm" alone is not sufficient.
- Delete additionally requires the ``allow_delete`` setting (off by default).
"""

from __future__ import annotations

import json
import logging
from typing import Any

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


def _gate_password(password: Any) -> tuple[bool, str]:
    from hermes_cli import provider_access as pa

    return pa.verify_password(password or "")


def _handle_manage_soul(args: dict[str, Any], **kwargs: Any) -> str:
    from hermes_cli import soul_access as sa

    action = str((args or {}).get("action") or "").strip().lower()

    if action == "view":
        result = sa.read_soul()
        if not result.get("ok"):
            return _err(result.get("error", "read failed"))
        # Don't leak the full path internals beyond what's useful
        return json.dumps({
            "ok": True,
            "exists": result.get("exists"),
            "content": result.get("content", ""),
        }, ensure_ascii=False)

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

    # Human approval gate for the sensitive mutation itself.
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
        if action == "delete":
            result = sa.delete_soul(str((args or {}).get("password") or ""))
        elif action == "add":
            result = sa.append_soul(str((args or {}).get("content") or ""),
                                   str((args or {}).get("password") or ""))
        else:  # set
            result = sa.set_soul(str((args or {}).get("content") or ""),
                                 str((args or {}).get("password") or ""))
        # Strip backup path from agent-visible result (internal detail)
        safe = dict(result)
        safe.pop("backup", None)
        safe.pop("path", None)
        return json.dumps(safe, ensure_ascii=False)
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
