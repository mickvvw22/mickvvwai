import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from gateway.desktop_control import request_desktop_restart

TOKEN = "d" * 64

class _Handler(BaseHTTPRequestHandler):
    status = 202
    payload = {"ok": True, "reason": "restart_requested"}
    expected_token = TOKEN

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/restart":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

def _descriptor(home: Path, port: int, **overrides):
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "host": "127.0.0.1",
        "port": port,
        "pid": os.getpid(),
        "token": TOKEN,
        **overrides,
    }
    target = state / "desktop-control.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    target.chmod(0o600)
    return target

def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

def test_requests_restart_from_local_authenticated_descriptor(tmp_path):
    server = _server()
    try:
        _descriptor(tmp_path, server.server_port)
        assert request_desktop_restart(hermes_home=tmp_path) == {
            "ok": True,
            "reason": "restart_requested",
        }
    finally:
        server.shutdown()
        server.server_close()

def test_maps_active_work_rejection_without_retrying(tmp_path):
    server = _server()
    old_status, old_payload = _Handler.status, _Handler.payload
    _Handler.status = 409
    _Handler.payload = {"ok": False, "reason": "active_work"}
    try:
        _descriptor(tmp_path, server.server_port)
        assert request_desktop_restart(hermes_home=tmp_path) == {
            "ok": False,
            "reason": "active_work",
        }
    finally:
        _Handler.status, _Handler.payload = old_status, old_payload
        server.shutdown()
        server.server_close()

def test_fails_closed_for_missing_or_untrusted_descriptors(tmp_path):
    assert request_desktop_restart(hermes_home=tmp_path) == {"ok": False, "reason": "unavailable"}
    target = _descriptor(tmp_path, 5555, host="0.0.0.0")
    assert request_desktop_restart(hermes_home=tmp_path) == {"ok": False, "reason": "unavailable"}
    target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    assert request_desktop_restart(hermes_home=tmp_path) == {"ok": False, "reason": "unavailable"}
