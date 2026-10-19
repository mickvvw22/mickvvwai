# -*- coding: utf-8 -*-
"""backup-access plugin — one-click backup/restore of hermes config to WebDAV.

Exposes the ``manage_backup`` tool so the agent can upload / download /
list encrypted hermes backups conversationally.

Backup contents: ``$HERMES_HOME/config.yaml`` (providers),
``$HERMES_HOME/SOUL.md``, ``$HERMES_HOME/souls/`` directory.
Packed into a zip in memory, uploaded as
``hermes-cloud-backup-YYYYMMDDTHHMMSSZ-<8hex>.hbackup`` (the filename
format enforced by ``hermes_cli.webdav_backup.WebDAVClient``).

WebDAV credentials are read from the environment or ``$HERMES_HOME/.env``:
``WEBDAV_URL``, ``WEBDAV_USER``, ``WEBDAV_PASS``. They are NEVER echoed
back in any reply.

Security rules (do not weaken):
- Actions 'backup' and 'restore' ALWAYS require the provider-access
  password in the 'password' argument; verified with ``verify_password``
  and NEVER echoed back.
- 'list' is read-only and needs no password.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "manage_backup"

_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Back up or restore the hermes configuration (providers in config.yaml, "
        "SOUL.md, souls/ directory) to/from WebDAV. "
        "Actions 'backup' and 'restore' ALWAYS require the provider-access "
        "password in the 'password' argument — ask the user for it conversationally "
        "first and never repeat it back in any reply. "
        "For 'backup': packs config into a zip and uploads it; returns the remote filename. "
        "For 'restore': 'filename' downloads and unpacks the backup, overwriting local config. "
        "For 'list': returns remote backup filenames (no password needed). "
        "WebDAV must be configured via WEBDAV_URL / WEBDAV_USER / WEBDAV_PASS "
        "in the environment or $HERMES_HOME/.env."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["backup", "restore", "list"],
                       "description": "What to do."},
            "filename": {"type": "string",
                         "description": "Remote backup filename (restore only)."},
            "password": {"type": "string",
                         "description": "Provider-access password. Required for backup/restore. Never repeat it back."},
        },
        "required": ["action"],
    },
}


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _webdav_config() -> tuple[Optional[str], str, str]:
    """Return (url, user, pass). Never logs the password."""
    home = _hermes_home()
    env_file = _read_env_file(home / ".env")
    url = os.environ.get("WEBDAV_URL") or env_file.get("WEBDAV_URL") or ""
    user = os.environ.get("WEBDAV_USER") or env_file.get("WEBDAV_USER") or ""
    passwd = os.environ.get("WEBDAV_PASS") or env_file.get("WEBDAV_PASS") or ""
    return (url.strip() or None, user, passwd)


def _make_client():
    url, user, passwd = _webdav_config()
    if not url:
        return None, "未配置 WebDAV：请在 $HERMES_HOME/.env 里设置 WEBDAV_URL、WEBDAV_USER、WEBDAV_PASS。"
    try:
        from hermes_cli.webdav_backup import WebDAVClient

        return WebDAVClient(url, user, passwd), ""
    except Exception as exc:
        # WebDAVError messages never contain credentials.
        return None, f"WebDAV 配置无效：{exc}"


def _gate_password(password: Any) -> tuple[bool, str]:
    from hermes_cli import provider_access as pa

    return pa.verify_password(password or "")


def _build_backup_zip() -> tuple[Optional[bytes], str]:
    """Pack config.yaml + SOUL.md + souls/ into a zip. Returns (bytes, error)."""
    try:
        home = _hermes_home()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in ("config.yaml", "SOUL.md"):
                p = home / rel
                if p.exists() and p.is_file():
                    zf.write(p, rel)
            souls = home / "souls"
            if souls.exists() and souls.is_dir():
                for f in sorted(souls.glob("*.md")):
                    if f.is_file():
                        zf.write(f, f"souls/{f.name}")
        data = buf.getvalue()
        if not data:
            return None, "没有可备份的内容。"
        return data, ""
    except Exception as exc:
        logger.warning("backup-access: build zip failed: %s", exc)
        return None, "打包备份失败。"


def _backup_filename() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rand = secrets.token_hex(4)
    return f"hermes-cloud-backup-{ts}-{rand}.hbackup"


def _restore_zip(data: bytes) -> tuple[bool, str]:
    """Unpack a backup zip over $HERMES_HOME. Returns (ok, error)."""
    try:
        home = _hermes_home()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Validate entries first: no absolute paths, no ".."
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in name.split("/"):
                    return False, "备份文件内容非法，已拒绝恢复。"
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = home / info.filename
                # Final containment check
                if home not in target.resolve().parents and target.resolve() != home:
                    # allow direct children too
                    try:
                        target.resolve().relative_to(home.resolve())
                    except ValueError:
                        return False, "备份文件内容非法，已拒绝恢复。"
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
        return True, ""
    except zipfile.BadZipFile:
        return False, "备份文件损坏，无法解压。"
    except Exception as exc:
        logger.warning("backup-access: restore failed: %s", exc)
        return False, "恢复备份失败。"


def _handle_manage_backup(args: dict[str, Any], **kwargs: Any) -> str:
    action = str((args or {}).get("action") or "").strip().lower()

    if action == "list":
        client, err = _make_client()
        if client is None:
            return _err(err)
        try:
            items = client.list_backups()
            return json.dumps({"ok": True, "backups": items}, ensure_ascii=False)
        except Exception as exc:
            logger.warning("backup-access: list failed: %s", type(exc).__name__)
            return _err("列出远程备份失败，请检查 WebDAV 配置与网络。")

    if action not in {"backup", "restore"}:
        return _err(f"unknown action: {action}")

    ok, reason = _gate_password((args or {}).get("password"))
    if not ok:
        if reason.startswith("locked:"):
            return _err(f"password locked, try again in {reason.split(':', 1)[1]} seconds")
        return _err("wrong password")

    client, err = _make_client()
    if client is None:
        return _err(err)

    if action == "backup":
        data, err = _build_backup_zip()
        if data is None:
            return _err(err)
        filename = _backup_filename()
        import tempfile

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".hbackup", delete=False) as tf:
                tf.write(data)
                tmp = tf.name
            size = client.put_file(filename, tmp)
            return json.dumps({"ok": True, "filename": filename, "bytes": size},
                              ensure_ascii=False)
        except Exception as exc:
            logger.warning("backup-access: upload failed: %s", type(exc).__name__)
            return _err("上传备份失败，请检查 WebDAV 配置与网络。")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # restore
    filename = str((args or {}).get("filename") or "").strip()
    # Sanitize: basename only, must end with .hbackup
    filename = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not filename.endswith(".hbackup") or "/" in filename or not filename:
        return _err("备份文件名无效。")
    import tempfile

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".hbackup")
        os.close(fd)
        client.get_file(filename, tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        ok2, err2 = _restore_zip(data)
        if not ok2:
            return _err(err2)
        return json.dumps({"ok": True, "filename": filename}, ensure_ascii=False)
    except Exception as exc:
        logger.warning("backup-access: download failed: %s", type(exc).__name__)
        return _err("下载备份失败，请检查文件名与网络。")
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _check_requirements() -> bool:
    try:
        from hermes_cli import provider_access as pa  # noqa: F401
        from hermes_cli import webdav_backup as wb  # noqa: F401

        return True
    except Exception:
        return False


def register(ctx) -> None:
    ctx.register_tool(name=TOOL_NAME, toolset="backup_access", schema=_SCHEMA,
                      handler=_handle_manage_backup, check_fn=_check_requirements,
                      emoji="💾")
