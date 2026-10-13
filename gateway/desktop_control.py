"""Authenticated loopback control for an already-running Mickvvw desktop app.

The descriptor is created by Electron under the process HERMES_HOME. A remote
Gateway on another host has no descriptor and fails closed; this is deliberately
not a network-reachable remote desktop control protocol.
"""
from __future__ import annotations
import http.client
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from hermes_constants import get_process_hermes_home
_DESCRIPTOR_RELATIVE_PATH = Path("state") / "desktop-control.json"
_TOKEN_RE = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)
_MAX_DESCRIPTOR_BYTES = 4096
_MAX_RESPONSE_BYTES = 4096

def request_desktop_restart(
    *, hermes_home: str | Path | None = None, timeout: float = 3.0
) -> dict[str, Any]:
    """Request a restart from this host's Desktop process; never starts an app.

    Returns ``{"ok": True, "reason": "restart_requested"}`` when the local
    Electron process accepted the request, or a small fail-closed reason code.
    """
    root = Path(hermes_home) if hermes_home is not None else get_process_hermes_home()
    descriptor_path = root / _DESCRIPTOR_RELATIVE_PATH
    try:
        info = descriptor_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_DESCRIPTOR_BYTES:
            return {"ok": False, "reason": "unavailable"}
        if os.name != "nt":
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                return {"ok": False, "reason": "unavailable"}
        raw = descriptor_path.read_text(encoding="utf-8")
        descriptor = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"ok": False, "reason": "unavailable"}

    if not isinstance(descriptor, dict):
        return {"ok": False, "reason": "unavailable"}
    try:
        version = descriptor.get("version")
        host = descriptor.get("host")
        port = int(descriptor.get("port"))
        pid = int(descriptor.get("pid"))
        token = descriptor.get("token")
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "reason": "unavailable"}
    if (
        version != 1
        or host != "127.0.0.1"
        or not 1 <= port <= 65535
        or pid <= 0
        or not isinstance(token, str)
        or not _TOKEN_RE.fullmatch(token)
    ):
        return {"ok": False, "reason": "unavailable"}

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/v1/restart",
            body=b"",
            headers={"Authorization": f"Bearer {token}", "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            return {"ok": False, "reason": "unavailable"}
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {"ok": False, "reason": "unavailable"}
        reason = result.get("reason") if isinstance(result, dict) else None
        if response.status == 202 and isinstance(result, dict) and result.get("ok") is True:
            return {"ok": True, "reason": "restart_requested"}
        if reason in {"active_work", "busy", "restart_failed"}:
            return {"ok": False, "reason": reason}
        return {"ok": False, "reason": "unavailable"}
    except (OSError, http.client.HTTPException, TimeoutError):
        return {"ok": False, "reason": "unavailable"}
    finally:
        connection.close()
