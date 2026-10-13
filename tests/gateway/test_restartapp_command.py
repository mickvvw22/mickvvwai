from types import SimpleNamespace

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin

class _Policy:
    enabled = True

    @staticmethod
    def is_admin(user_id):
        return user_id == "owner"

@pytest.mark.asyncio
async def test_restartapp_rejects_group_chat_before_admin_check(monkeypatch):
    policy_called = False

    def policy_for_runner_source(*_args):
        nonlocal policy_called
        policy_called = True
        return _Policy()

    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", policy_for_runner_source)
    runner = SimpleNamespace()
    event = SimpleNamespace(source=SimpleNamespace(chat_type="group", user_id="owner"))

    reply = await GatewaySlashCommandsMixin._handle_restartapp_command(runner, event)

    assert "private chat" in reply.lower() or "私聊" in reply
    assert not policy_called

@pytest.mark.asyncio
async def test_restartapp_requires_an_explicit_enabled_admin_policy(monkeypatch):
    class DisabledPolicy:
        enabled = False

        @staticmethod
        def is_admin(_user_id):
            return True

    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", lambda *_: DisabledPolicy())
    runner = SimpleNamespace()
    event = SimpleNamespace(source=SimpleNamespace(chat_type="dm", user_id="owner"))

    reply = await GatewaySlashCommandsMixin._handle_restartapp_command(runner, event)

    assert "configured gateway admin" in reply.lower() or "管理员" in reply

@pytest.mark.asyncio
async def test_restartapp_refuses_while_gateway_has_running_agent(monkeypatch):
    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", lambda *_: _Policy())
    confirmed = False

    async def confirm(**_kwargs):
        nonlocal confirmed
        confirmed = True
        return "unexpected"

    runner = SimpleNamespace(_running_agent_count=lambda: 1, _maybe_confirm_destructive_slash=confirm)
    event = SimpleNamespace(source=SimpleNamespace(chat_type="dm", user_id="owner"))

    reply = await GatewaySlashCommandsMixin._handle_restartapp_command(runner, event)

    assert "task is active" in reply.lower() or "任务运行" in reply
    assert not confirmed

@pytest.mark.asyncio
async def test_restartapp_routes_through_confirmation_and_local_client(monkeypatch):
    monkeypatch.setattr("gateway.slash_access.policy_for_runner_source", lambda *_: _Policy())
    monkeypatch.setattr(
        "gateway.desktop_control.request_desktop_restart",
        lambda: {"ok": True, "reason": "restart_requested"},
    )
    seen = {}

    async def confirm(**kwargs):
        seen.update(kwargs)
        return await kwargs["execute"]()

    runner = SimpleNamespace(_running_agent_count=lambda: 0, _maybe_confirm_destructive_slash=confirm)
    event = SimpleNamespace(source=SimpleNamespace(chat_type="dm", user_id="owner"))

    reply = await GatewaySlashCommandsMixin._handle_restartapp_command(runner, event)

    assert seen["command"] == "restartapp"
    assert "Mickvvw" in seen["title"]
    assert "local gateway" in seen["detail"].lower() or "本机网关" in seen["detail"]
    assert "restart accepted" in reply.lower() or "已接受" in reply
