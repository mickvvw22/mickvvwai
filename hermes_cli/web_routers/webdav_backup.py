"""Authenticated WebDAV backup routes for the desktop backend."""
from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_cli.backup import run_backup, run_import
from hermes_cli.backup_encryption import BackupEncryptionError, decrypt_file, encrypt_file
from hermes_cli.web_routers._common import config_scoped_to_thread, destructive_profile
from hermes_cli.webdav_backup import WebDAVClient, WebDAVError
from hermes_constants import get_hermes_home

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()


class WebDAVConnection(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    username: str = Field(default="", max_length=512)
    password: str = Field(default="", max_length=4096)


class WebDAVBackupRequest(WebDAVConnection):
    passphrase: str = Field(min_length=12, max_length=1024)


class WebDAVRestoreRequest(WebDAVBackupRequest):
    filename: str


def _client(body: WebDAVConnection) -> WebDAVClient:
    return WebDAVClient(body.url, body.username, body.password)


def _validate_passphrase(passphrase: str) -> None:
    if len(passphrase) < 12:
        raise HTTPException(status_code=400, detail="Use an encryption passphrase of at least 12 characters.")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ram_backed_temp_root() -> Optional[Path]:
    """Return a verified Linux tmpfs mount; never fall back to an ordinary disk temp directory."""
    if not sys.platform.startswith("linux"):
        return None
    candidate = Path("/dev/shm")
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return None
        resolved = candidate.resolve(strict=True)
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            filesystem = after.split(maxsplit=1)
            if not separator or len(fields) < 5 or not filesystem or filesystem[0] != "tmpfs":
                continue
            mount_point = fields[4]
            for escaped, value in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                mount_point = mount_point.replace(escaped, value)
            if Path(mount_point).resolve() == resolved:
                return resolved
    except (OSError, ValueError, IndexError):
        return None
    return None


def _secure_work_directory(prefix: str):
    """Create private temporary work only on a verified RAM-backed filesystem."""
    temp_root = _ram_backed_temp_root()
    if temp_root is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Encrypted WebDAV backup and restore require a verified RAM-backed temporary filesystem. "
                "No backup data was processed; ordinary disk storage is not used as a fallback."
            ),
        )
    try:
        return tempfile.TemporaryDirectory(prefix=prefix, dir=str(temp_root))
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="The RAM-backed temporary filesystem is unavailable or out of space; no backup data was processed.",
        ) from exc


def _run_backup(home: Path, archive: Path) -> None:
    try:
        complete = run_backup(SimpleNamespace(output=str(archive), hermes_home=str(home), keep=0))
    except SystemExit as exc:
        raise HTTPException(status_code=500, detail=f"Could not create a local backup (exit {exc.code}).") from exc
    except Exception as exc:
        _log.exception("WebDAV local backup failed")
        raise HTTPException(status_code=500, detail="Could not create a local backup.") from exc
    if not complete or not archive.is_file():
        raise HTTPException(status_code=500, detail="The local backup was incomplete; nothing was uploaded.")
    try:
        archive.chmod(0o600)
    except OSError:
        pass


def _run_import(archive: Path) -> int | None:
    try:
        return run_import(SimpleNamespace(zipfile=str(archive), force=True, start_gateway_after_import=False))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


@router.post("/api/webdav/test")
async def test_webdav(body: WebDAVConnection):
    try:
        return await asyncio.to_thread(_client(body).test)
    except WebDAVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/webdav/list")
async def list_webdav_backups(body: WebDAVConnection):
    try:
        files = await asyncio.to_thread(_client(body).list_backups)
        return {"files": files}
    except WebDAVError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/webdav/create")
async def create_webdav_backup(body: WebDAVBackupRequest, profile: Optional[str] = None):
    profile = destructive_profile(profile, "POST /api/webdav/create")
    _validate_passphrase(body.passphrase)
    try:
        client = _client(body)

        def _create_and_upload() -> dict:
            home = Path(get_hermes_home()).expanduser().resolve()
            stamp = _timestamp()
            filename = f"hermes-cloud-backup-{stamp}-{secrets.token_hex(4)}.hbackup"
            with _secure_work_directory(prefix="hermes-webdav-backup-") as temp_dir:
                work = Path(temp_dir)
                archive = work / "backup.zip"
                encrypted = work / filename
                _run_backup(home, archive)
                try:
                    encrypt_file(archive, encrypted, body.passphrase)
                except BackupEncryptionError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                uploaded = client.put_file(filename, encrypted)
            return {"ok": True, "filename": filename, "size": uploaded}

        return await config_scoped_to_thread(profile, _create_and_upload)
    except HTTPException:
        raise
    except WebDAVError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _log.exception("WebDAV backup upload failed")
        raise HTTPException(status_code=500, detail="Could not upload the encrypted backup.") from exc


@router.post("/api/webdav/restore")
async def restore_webdav_backup(body: WebDAVRestoreRequest, profile: Optional[str] = None):
    profile = destructive_profile(profile, "POST /api/webdav/restore")
    _validate_passphrase(body.passphrase)
    try:
        client = _client(body)

        def _download_and_restore() -> dict:
            home = Path(get_hermes_home()).expanduser().resolve()
            with _secure_work_directory(prefix="hermes-webdav-restore-") as temp_dir:
                work = Path(temp_dir)
                encrypted = work / "remote.hbackup"
                archive = work / "restore.zip"
                client.get_file(body.filename, encrypted)
                try:
                    decrypt_file(encrypted, archive, body.passphrase)
                except BackupEncryptionError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if not zipfile.is_zipfile(archive):
                    raise HTTPException(status_code=400, detail="The decrypted file is not a valid backup archive.")

                rollback = work / "rollback.zip"
                _run_backup(home, rollback)
                try:
                    result = _run_import(archive)
                except Exception:
                    _log.exception("WebDAV restore failed; attempting to roll back the local profile")
                    result = 1
                if result not in (None, 0):
                    try:
                        rollback_result = _run_import(rollback)
                    except Exception:
                        rollback_result = 1
                    if rollback_result not in (None, 0):
                        backup_dir = home / "backups"
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        recovery = backup_dir / f"hermes-cloud-recovery-{_timestamp()}-{secrets.token_hex(4)}.hbackup"
                        try:
                            encrypt_file(rollback, recovery, body.passphrase)
                        except Exception:
                            _log.exception("Could not preserve encrypted recovery archive after failed WebDAV restore")
                            raise HTTPException(
                                status_code=500,
                                detail=(
                                    "Restore and automatic rollback both failed. "
                                    "Contact support before making further changes."
                                ),
                            )
                        raise HTTPException(
                            status_code=500,
                            detail=(
                                "Restore and automatic rollback both failed. An encrypted recovery archive "
                                f"was saved locally at {recovery}."
                            ),
                        )
                    raise HTTPException(
                        status_code=500,
                        detail="The remote backup could not be fully restored. The previous local data was restored.",
                    )
                return {"ok": True, "filename": body.filename, "restored": True}

        return await config_scoped_to_thread(profile, _download_and_restore)
    except HTTPException:
        raise
    except WebDAVError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _log.exception("WebDAV restore failed")
        raise HTTPException(status_code=500, detail="Could not restore the remote backup.") from exc
