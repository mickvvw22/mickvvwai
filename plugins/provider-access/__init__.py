"""provider-access plugin — manage model providers from AI chat.

Exposes the ``manage_provider`` tool so the agent can add / list / update /
delete model providers conversationally ("帮我添加新的模型供应商"). Every
mutating action goes through the SAME password gate as the Telegram bot
(``hermes_cli.provider_access``) plus the standard tool approval gate
(``tools.approval.request_tool_approval``).

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

TOOL_NAME = "manage_provider"

_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Add, list, update or delete a model provider (e.g. deepseek, openai, "
        "or a custom OpenAI-compatible endpoint). "
        "Actions 'add', 'update' and 'delete' ALWAYS require the provider-access "
        "password in the 'password' argument — ask the user for it conversationally "
        "first and never repeat it back in any reply. "
        "For 'add': give either 'provider' (a built-in name) or 'name' + 'base_url' "
        "(custom endpoint), plus 'api_key' and optionally 'model'. "
        "For 'update': give 'provider' and the new 'api_key'. "
        "For 'delete': give 'provider' (works only when the owner enabled deletion "
        "in settings). 'list' needs no password."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "update", "delete"],
                       "description": "What to do."},
            "provider": {"type": "string",
                         "description": "Built-in provider name, e.g. 'deepseek'."},
            "name": {"type": "string",
                     "description": "Custom endpoint display name (add, custom only)."},
            "base_url": {"type": "string",
                         "description": "Custom endpoint base URL (add, custom only)."},
            "api_key": {"type": "string",
                        "description": "API key. Never repeat it back."},
            "model": {"type": "string",
                      "description": "Default model id for a custom endpoint."},
            "password": {"type": "string",
                         "description": "Provider-access password. Required for add/update/delete. Never repeat it back."},
        },
        "required": ["action"],
    },
}


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _gate_password(password: Any) -> tuple[bool, str]:
    from hermes_cli import provider_access as pa

    return pa.verify_password(password or "")


def _handle_manage_provider(args: dict[str, Any], **kwargs: Any) -> str:
    from hermes_cli import provider_access as pa

    action = str((args or {}).get("action") or "").strip().lower()

    allowed, _reason = pa.check_gate()
    if not allowed:
        return _err("provider access is disabled in settings")

    if action == "list":
        rows = pa.list_providers_with_sources()
        return json.dumps({"ok": True, "providers": rows}, ensure_ascii=False)

    if action not in {"add", "update", "delete"}:
        return _err("unknown action: %s" % action)

    ok, reason = _gate_password((args or {}).get("password"))
    if not ok:
        if reason.startswith("locked:"):
            return _err("password locked, try again in %s seconds" % reason.split(":", 1)[1])
        return _err("wrong password")

    if action == "delete" and not pa.is_delete_allowed():
        return _err("deletion is disabled in settings (allow_delete is off)")

    # Human approval gate for the sensitive mutation itself.
    try:
        from tools.approval import request_tool_approval

        verdict = request_tool_approval(
            TOOL_NAME,
            "manage_provider %s %s" % (action, (args or {}).get("provider") or (args or {}).get("name") or ""),
            rule_key="provider-access:%s" % action,
        )
    except Exception as exc:
        logger.warning("provider-access: approval gate error: %s", exc)
        return _err("approval gate unavailable")
    if not (isinstance(verdict, dict) and verdict.get("approved") is True):
        return _err("denied by approval gate")

    try:
        if action == "delete":
            result = pa.delete_provider_entry(str((args or {}).get("provider") or ""))
            result["source"] = None
            return json.dumps({"ok": bool(result.get("ok")), "result": result}, ensure_ascii=False)
        if action == "update":
            result = pa.add_builtin_key(str((args or {}).get("provider") or ""),
                                        str((args or {}).get("api_key") or ""), pa.SOURCE_CHAT)
            return json.dumps({"ok": bool(result.get("ok")), "result": _public_result(result)},
                              ensure_ascii=False)
        # add
        provider = str((args or {}).get("provider") or "").strip()
        if provider:
            result = pa.add_builtin_key(provider, str((args or {}).get("api_key") or ""),
                                        pa.SOURCE_CHAT)
        else:
            result = pa.add_custom_endpoint(str((args or {}).get("name") or ""),
                                            str((args or {}).get("base_url") or ""),
                                            str((args or {}).get("api_key") or ""),
                                            str((args or {}).get("model") or ""),
                                            pa.SOURCE_CHAT)
        return json.dumps({"ok": bool(result.get("ok")), "result": _public_result(result)},
                          ensure_ascii=False)
    except Exception as exc:
        logger.warning("provider-access: %s failed: %s", action, exc)
        return _err("operation failed")


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Strip anything secret-adjacent before returning to the model."""
    safe = dict(result)
    safe.pop("api_key", None)
    return safe


def _check_requirements() -> bool:
    try:
        from hermes_cli import provider_access as pa

        return bool(pa.check_gate()[0])
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(name=TOOL_NAME, toolset="provider_access", schema=_SCHEMA,
                      handler=_handle_manage_provider, check_fn=_check_requirements,
                      emoji="🔑")
