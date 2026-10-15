"""Tests for hermes_cli.provider_access — the shared password gate."""

import hashlib
import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)


def _mod():
    import hermes_cli.provider_access as pa

    return pa


def test_defaults_when_no_config():
    pa = _mod()
    settings = pa.get_settings()
    assert settings["enabled"] is True
    assert settings["allow_delete"] is False
    assert settings["password_hash"] == hashlib.sha256(b"mickvvw").hexdigest()


def test_default_password_verifies():
    pa = _mod()
    ok, reason = pa.verify_password("mickvvw")
    assert ok is True
    assert reason == "ok"


def test_wrong_password_fails_without_lockout_first():
    pa = _mod()
    ok, reason = pa.verify_password("nope")
    assert ok is False
    assert reason == "wrong"


def test_five_failures_lock_for_ten_minutes(monkeypatch):
    pa = _mod()
    import time

    real_time = time.time
    for _ in range(4):
        ok, _ = pa.verify_password("wrong")
        assert ok is False
    ok, reason = pa.verify_password("wrong")
    assert ok is False
    assert reason == "locked:600"
    # Still locked for the right password too.
    ok, _ = pa.verify_password("mickvvw")
    assert ok is False
    # After the lockout window the default password works again.
    monkeypatch.setattr(time, "time", lambda: real_time() + 601)
    ok, reason = pa.verify_password("mickvvw")
    assert ok is True
    assert reason == "ok"


def test_set_password_changes_hash():
    pa = _mod()
    pa.set_password("s3cret-new")
    ok, _ = pa.verify_password("mickvvw")
    assert ok is False
    ok, reason = pa.verify_password("s3cret-new")
    assert ok is True
    assert reason == "ok"


def test_set_password_rejects_empty():
    pa = _mod()
    with pytest.raises(ValueError):
        pa.set_password("   ")


def test_gate_respects_enabled_flag():
    pa = _mod()
    ok, _ = pa.check_gate()
    assert ok is True
    pa.save_settings(enabled=False)
    ok, reason = pa.check_gate()
    assert ok is False
    assert reason == "disabled"
    assert pa.is_enabled() is False


def test_delete_defaults_off_and_can_be_enabled():
    pa = _mod()
    assert pa.is_delete_allowed() is False
    pa.save_settings(allow_delete=True)
    assert pa.is_delete_allowed() is True


def test_source_badge_roundtrip():
    pa = _mod()
    assert pa.get_source("deepseek") is None
    pa.record_source("deepseek", "bot")
    assert pa.get_source("deepseek") == "bot"
    pa.record_source("deepseek", "chat")
    assert pa.get_source("deepseek") == "chat"
    pa.clear_source("deepseek")
    assert pa.get_source("deepseek") is None


def test_source_badge_rejects_unknown_source():
    pa = _mod()
    pa.record_source("deepseek", "nope")
    assert pa.get_source("deepseek") is None


def test_lock_state_survives_reimport(tmp_path, monkeypatch):
    """The failure counter lives on disk, not just in memory."""
    import importlib

    pa = _mod()
    for _ in range(5):
        pa.verify_password("wrong")
    import hermes_cli.provider_access as pa2

    importlib.reload(pa2)
    ok, _ = pa2.verify_password("mickvvw")
    assert ok is False
