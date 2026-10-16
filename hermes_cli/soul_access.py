"""Shared backend for managing the bot persona prompt (SOUL.md) via bot/chat.

The persona prompt lives in ``$HERMES_HOME/SOUL.md`` (e.g. ``/root/.hermes/SOUL.md``
for the Telegram bot gateway). This module provides read / replace / append /
clear operations with a safety backup (``SOUL.md.bak``) before every mutation.

Mutating actions go through the SAME password gate as provider-access
(``hermes_cli.provider_access.verify_password``), so the Telegram bot and the
agent plugin tool (``plugins/soul-access``) enforce identical auth.

Security rules (do not weaken):
- The access password travels only as the ``password`` argument; callers verify
  it with ``verify_password`` and NEVER echo it back, in success or error text.
- ``delete``/``clear`` additionally requires the ``allow_delete`` setting
  (off by default) — same as provider deletion.
- Every mutation writes a timestamped backup first; failures leave the file
  untouched.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"
BACKUP_SUFFIX = ".bak"


def get_soul_path() -> Path:
    """Resolve the SOUL.md path for the current HERMES_HOME."""
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        import os

        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return home / SOUL_FILENAME


def read_soul() -> dict[str, Any]:
    """Return the current prompt text (no password needed)."""
    path = get_soul_path()
    try:
        text = path.read_text(encoding="utf-8")
        return {"ok": True, "exists": True, "path": str(path), "content": text}
    except FileNotFoundError:
        return {"ok": True, "exists": False, "path": str(path), "content": ""}
    except Exception as exc:
        logger.warning("soul_access: read failed: %s", exc)
        return {"ok": False, "error": "read failed"}


def _backup(path: Path) -> Optional[str]:
    """Write a timestamped backup; return backup path or None."""
    try:
        if not path.exists():
            return None
        backup = path.with_name(f"{path.name}.{int(time.time())}{BACKUP_SUFFIX}")
        shutil.copy2(path, backup)
        return str(backup)
    except Exception as exc:
        logger.warning("soul_access: backup failed: %s", exc)
        return None


def _gate(password: Any) -> tuple[bool, str]:
    """Shared password gate (same as provider-access)."""
    from hermes_cli import provider_access as pa

    return pa.verify_password(password or "")


def set_soul(content: str, password: str) -> dict[str, Any]:
    """Replace the entire prompt file (password required)."""
    ok, reason = _gate(password)
    if not ok:
        if reason.startswith("locked:"):
            return {"ok": False, "error": f"password locked, try again in {reason.split(':', 1)[1]} seconds"}
        return {"ok": False, "error": "wrong password"}
    path = get_soul_path()
    try:
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content or "", encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "path": str(path), "backup": backup,
                "chars": len(content or "")}
    except Exception as exc:
        logger.warning("soul_access: set failed: %s", exc)
        return {"ok": False, "error": "write failed"}


def append_soul(content: str, password: str) -> dict[str, Any]:
    """Append text to the prompt file (password required)."""
    ok, reason = _gate(password)
    if not ok:
        if reason.startswith("locked:"):
            return {"ok": False, "error": f"password locked, try again in {reason.split(':', 1)[1]} seconds"}
        return {"ok": False, "error": "wrong password"}
    path = get_soul_path()
    try:
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new_text = existing + sep + (content or "") + "\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "path": str(path), "backup": backup,
                "chars": len(new_text)}
    except Exception as exc:
        logger.warning("soul_access: append failed: %s", exc)
        return {"ok": False, "error": "write failed"}


def delete_soul(password: str) -> dict[str, Any]:
    """Clear the prompt file (password + allow_delete required)."""
    from hermes_cli import provider_access as pa

    ok, reason = _gate(password)
    if not ok:
        if reason.startswith("locked:"):
            return {"ok": False, "error": f"password locked, try again in {reason.split(':', 1)[1]} seconds"}
        return {"ok": False, "error": "wrong password"}
    if not pa.is_delete_allowed():
        return {"ok": False, "error": "deletion is disabled in settings (allow_delete is off)"}
    path = get_soul_path()
    try:
        backup = _backup(path)
        if path.exists():
            path.write_text("", encoding="utf-8")
        return {"ok": True, "path": str(path), "backup": backup}
    except Exception as exc:
        logger.warning("soul_access: delete failed: %s", exc)
        return {"ok": False, "error": "delete failed"}
