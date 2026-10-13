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
