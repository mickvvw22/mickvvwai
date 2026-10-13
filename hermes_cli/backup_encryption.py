"""Authenticated streaming encryption for remote Hermes backups.

The passphrase is never persisted. The format is: magic || salt || nonce ||
AES-GCM ciphertext || tag. Header bytes are authenticated as associated data.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_MAGIC = b"XYWDAV1\0"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CHUNK_BYTES = 1024 * 1024
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1


class BackupEncryptionError(ValueError):
    """Invalid encrypted backup or passphrase."""


def _cipher_api():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise BackupEncryptionError("The cryptography package is required for encrypted WebDAV backups.") from exc
    return Cipher, algorithms, modes, InvalidTag


def _key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise BackupEncryptionError("A non-empty encryption passphrase is required.")
    try:
        return hashlib.scrypt(
            passphrase.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
            p=_SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024,
        )
    except (ValueError, MemoryError) as exc:
        raise BackupEncryptionError("Could not derive the backup encryption key.") from exc


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".partial")


def encrypt_file(source: str | Path, destination: str | Path, passphrase: str) -> Path:
    """Encrypt *source* to *destination* without loading the archive into memory."""
    source_path, destination_path = Path(source), Path(destination)
    if source_path.resolve() == destination_path.resolve():
        raise BackupEncryptionError("Source and destination must be different files.")
    cipher_class, cipher_algorithms, cipher_modes, _ = _cipher_api()
    salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
    header = _MAGIC + salt + nonce
    encryptor = cipher_class(
        cipher_algorithms.AES(_key(passphrase, salt)), cipher_modes.GCM(nonce)
    ).encryptor()
    encryptor.authenticate_additional_data(header)
    temp_path = _temporary_path(destination_path)
    try:
        with source_path.open("rb") as src, temp_path.open("wb") as dst:
            os.chmod(temp_path, 0o600)
            dst.write(header)
            while chunk := src.read(_CHUNK_BYTES):
                dst.write(encryptor.update(chunk))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp_path, destination_path)
        try:
            os.chmod(destination_path, 0o600)
        except OSError:
            pass
        return destination_path
    finally:
        temp_path.unlink(missing_ok=True)


def decrypt_file(source: str | Path, destination: str | Path, passphrase: str) -> Path:
    """Authenticate and decrypt a remote backup to *destination* atomically."""
    source_path, destination_path = Path(source), Path(destination)
    total_size = source_path.stat().st_size
    header_size = len(_MAGIC) + _SALT_BYTES + _NONCE_BYTES
    if total_size < header_size + _TAG_BYTES:
        raise BackupEncryptionError("The encrypted backup is incomplete or invalid.")
    cipher_class, cipher_algorithms, cipher_modes, invalid_tag = _cipher_api()
    with source_path.open("rb") as src:
        header = src.read(header_size)
        if len(header) != header_size or not header.startswith(_MAGIC):
            raise BackupEncryptionError("The file is not a supported encrypted backup.")
        salt_start = len(_MAGIC)
        salt = header[salt_start : salt_start + _SALT_BYTES]
        nonce = header[salt_start + _SALT_BYTES :]
        src.seek(total_size - _TAG_BYTES)
        tag = src.read(_TAG_BYTES)
        src.seek(header_size)
        ciphertext_bytes = total_size - header_size - _TAG_BYTES
        decryptor = cipher_class(
            cipher_algorithms.AES(_key(passphrase, salt)), cipher_modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        temp_path = _temporary_path(destination_path)
        remaining = ciphertext_bytes
        try:
            with temp_path.open("wb") as dst:
                os.chmod(temp_path, 0o600)
                while remaining:
                    chunk = src.read(min(_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise BackupEncryptionError("The encrypted backup is truncated.")
                    remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                try:
                    dst.write(decryptor.finalize())
                except invalid_tag as exc:
                    raise BackupEncryptionError(
                        "The passphrase is incorrect or the encrypted backup is damaged."
                    ) from exc
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temp_path, destination_path)
            try:
                os.chmod(destination_path, 0o600)
            except OSError:
                pass
            return destination_path
        finally:
            temp_path.unlink(missing_ok=True)
