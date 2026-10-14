"""Behavior tests for encrypted WebDAV backup transport."""
from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli.backup_encryption import BackupEncryptionError, decrypt_file, encrypt_file
from hermes_cli.webdav_backup import WebDAVClient, WebDAVError


class _WebDAVHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(b"alice:secret").decode("ascii")
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_PROPFIND(self):
        if not self._authorized():
            return
        entries = [
            '<d:response><d:href>/dav/backups/</d:href><d:propstat><d:prop>'
            '<d:resourcetype><d:collection/></d:resourcetype><d:getcontentlength>0</d:getcontentlength>'
            '<d:getlastmodified>Sun, 06 Nov 1994 08:49:37 GMT</d:getlastmodified>'
            '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
        ]
        for name, content in self.server.files.items():
            entries.append(
                f'<d:response><d:href>/dav/backups/{name}</d:href><d:propstat><d:prop>'
                f'<d:resourcetype/><d:getcontentlength>{len(content)}</d:getcontentlength>'
                '<d:getlastmodified>Sun, 06 Nov 1994 08:49:37 GMT</d:getlastmodified>'
                '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
            )
        body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + ''.join(entries) + '</d:multistatus>').encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self._authorized():
            return
        size = int(self.headers.get("Content-Length", "0"))
        name = self.path.rsplit("/", 1)[-1]
        self.server.files[name] = self.rfile.read(size)
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            return
        name = self.path.rsplit("/", 1)[-1]
        content = self.server.files.get(name)
        if content is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture
def webdav_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebDAVHandler)
    server.files = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/dav/backups/"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_encrypted_file_round_trip_and_wrong_passphrase(tmp_path: Path):
    source = tmp_path / "backup.zip"
    encrypted = tmp_path / "backup.hbackup"
    restored = tmp_path / "restored.zip"
    source.write_bytes(bytes(range(256)) * 9000)

    encrypt_file(source, encrypted, "correct horse battery staple")
    assert encrypted.read_bytes() != source.read_bytes()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    decrypt_file(encrypted, restored, "correct horse battery staple")
    assert restored.read_bytes() == source.read_bytes()

    wrong_target = tmp_path / "wrong.zip"
    with pytest.raises(BackupEncryptionError, match="passphrase is incorrect"):
        decrypt_file(encrypted, wrong_target, "incorrect passphrase")
    assert not wrong_target.exists()


def test_webdav_local_round_trip_and_listing(webdav_server: str, tmp_path: Path):
    client = WebDAVClient(webdav_server, "alice", "secret")
    assert client.test()["ok"] is True
    assert client.list_backups() == []

    filename = "hermes-cloud-backup-20261013T010203Z-a1b2c3d4.hbackup"
    source = tmp_path / filename
    source.write_bytes(b"authenticated encrypted payload")
    assert client.put_file(filename, source) == source.stat().st_size

    listed = client.list_backups()
    assert len(listed) == 1
    assert listed[0]["filename"] == filename
    assert listed[0]["size"] == source.stat().st_size

    downloaded = tmp_path / "downloaded.hbackup"
    assert client.get_file(filename, downloaded) == source.stat().st_size
    assert downloaded.read_bytes() == source.read_bytes()


def test_webdav_rejects_plain_http_to_public_host(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.webdav_backup.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 80))],
    )
    with pytest.raises(WebDAVError, match="private or local"):
        WebDAVClient("http://public.example/dav")


def test_full_encrypted_backup_restores_settings_and_secret_files(tmp_path: Path, capsys):
    import zipfile
    from types import SimpleNamespace

    from hermes_cli.backup import run_backup

    home = tmp_path / "hermes-home"
    home.mkdir()
    secret_values = (
        "cloud-api-key-for-backup-test",
        "oauth-refresh-token-for-backup-test",
        "vault-master-key-for-backup-test",
    )
    (home / "config.yaml").write_text(f"model:\n  api_key: {secret_values[0]}\n", encoding="utf-8")
    (home / ".env").write_text(f"OPENAI_API_KEY={secret_values[0]}\n", encoding="utf-8")
    (home / "auth.json").write_text(f'{{"refresh_token":"{secret_values[1]}"}}\n', encoding="utf-8")
    (home / "vault.key").write_bytes(secret_values[2].encode("utf-8"))

    archive = tmp_path / "backup.zip"
    assert run_backup(SimpleNamespace(output=str(archive), hermes_home=str(home), keep=0)) is True
    output = capsys.readouterr().out
    assert all(secret not in output for secret in secret_values)

    encrypted = tmp_path / "backup.hbackup"
    encrypt_file(archive, encrypted, "recovery passphrase with 24 chars")
    ciphertext = encrypted.read_bytes()
    assert all(secret.encode("utf-8") not in ciphertext for secret in secret_values)

    restored = tmp_path / "restored.zip"
    decrypt_file(encrypted, restored, "recovery passphrase with 24 chars")
    with zipfile.ZipFile(restored) as backup_zip:
        names = set(backup_zip.namelist())
        assert {"config.yaml", ".env", "auth.json", "vault.key"} <= names
        assert secret_values[0] in backup_zip.read("config.yaml").decode("utf-8")
        assert secret_values[1] in backup_zip.read("auth.json").decode("utf-8")
        assert backup_zip.read("vault.key") == secret_values[2].encode("utf-8")


@pytest.mark.platforms("linux")
def test_webdav_work_directory_is_private_and_ram_backed():
    import stat

    from hermes_cli.web_routers.webdav_backup import _ram_backed_temp_root, _secure_work_directory

    ram_root = _ram_backed_temp_root()
    assert ram_root is not None
    with _secure_work_directory("hermes-webdav-test-") as temp_dir:
        work_dir = Path(temp_dir)
        assert work_dir.resolve().is_relative_to(ram_root.resolve())
        assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700


def test_webdav_restore_does_not_start_gateway_service(tmp_path: Path, monkeypatch):
    from hermes_cli.web_routers import webdav_backup

    calls = []

    def fake_import(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(webdav_backup, "run_import", fake_import)
    assert webdav_backup._run_import(tmp_path / "restore.zip") == 0
    assert len(calls) == 1
    assert calls[0].start_gateway_after_import is False
