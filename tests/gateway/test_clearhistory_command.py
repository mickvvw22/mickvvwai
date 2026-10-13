"""Behavior tests for the permanent, confirmation-gated gateway history wipe."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", chat_type="dm")


@pytest.mark.asyncio
async def test_clearhistory_is_gated_and_deletes_without_export(monkeypatch, tmp_path: Path):
    from gateway.run import GatewayRunner
    import gateway.slash_commands_session as session_commands
    import gateway.run as gateway_run

    source = _source()
    old_entry = SessionEntry(
        session_key="telegram:dm:c1:u1", session_id="old-session",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm",
    )
    new_entry = SessionEntry(
        session_key=old_entry.session_key, session_id="new-session",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm",
    )
    store = MagicMock()
    store._routing_home = tmp_path / "home"
    store.sessions_dir = tmp_path / "sessions"
    store.get_or_create_session.side_effect = [old_entry, new_entry]
    store.peek_session_id.return_value = old_entry.session_id
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = None
    db = MagicMock()
    db.delete_session = AsyncMock(return_value=1)
    runner._session_db = db
    runner._session_key_for_source = lambda _source: old_entry.session_key
    runner._is_session_running = lambda _key: False

    @asynccontextmanager
    async def _no_profile_scope(_source):
        yield

    runner._async_profile_scope_for_source = _no_profile_scope
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._invalidate_session_run_generation = MagicMock()
    runner._release_running_agent_state = MagicMock()
    runner._evict_cached_agent = MagicMock()
    runner._clear_conversation_scope = MagicMock()
    runner._fire_session_reset_hooks = AsyncMock()
    runner._is_telegram_topic_lane = lambda _source: False
    captured = {}

    async def _capture_gate(**kwargs):
        captured.update(kwargs)
        return "confirmation requested"

    runner._maybe_confirm_destructive_slash = AsyncMock(side_effect=_capture_gate)
    monkeypatch.setattr(gateway_run, "_profile_sessions_dir", lambda _launch: tmp_path / "sessions")
    monkeypatch.setattr(session_commands, "_reset_process_scoped_tool_state", lambda: None)

    event = MessageEvent(text="/clearhistory", source=source, message_id="m1")
    assert await GatewayRunner._handle_clearhistory_command(runner, event) == "confirmation requested"
    assert captured["command"] == "clearhistory"
    assert db.delete_session.await_count == 0  # no delete before confirmation

    assert await captured["execute"]()  # user confirms once
    db.delete_session.assert_awaited_once_with(
        old_entry.session_id,
        sessions_dir=tmp_path / "sessions",
        exclude_active_write_guards=True,
        include_compression_chain=True,
    )
    store.remove_by_session_id.assert_called_once_with(old_entry.session_id)
    assert store.get_or_create_session.call_count == 2
    runner._fire_session_reset_hooks.assert_not_awaited()
