"""Gateway slash commands that switch or tune the model route:
/model, /codex-runtime, /reasoning, /fast, /personality.

Split out of ``gateway/slash_commands.py``; bound onto ``GatewayRunner`` through
``GatewaySlashCommandsMixin``. Origin internals are imported lazily inside the bodies to avoid
the import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from hermes_cli.config import atomic_config_write
from utils import base_url_host_matches

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

# /fast argument -> (service tier, persisted value, i18n label key; None = value.upper()).
_FAST_SELECTIONS = {
    "fast": ("priority", "fast", "gateway.fast.label_fast"),
    "on": ("priority", "fast", "gateway.fast.label_fast"),
    "normal": (None, "normal", "gateway.fast.label_normal"),
    "off": (None, "normal", "gateway.fast.label_normal"),
    "auto": ("auto", "auto", None),
    "cold": ("cold", "cold", None),
    "ultrafast": ("ultrafast", "ultrafast", None),
}


def _fast_route_supports(model: str, runtime: dict, tier: Optional[str] = None) -> bool:
    """The turn's own gate (``run_turn.py::_resolve_turn_agent_config``): a tier /fast accepts must be
    one the session's next request actually carries, so proxies and other providers are refused."""
    from hermes_cli.models import resolve_fast_mode_overrides

    return resolve_fast_mode_overrides(
        model, provider=runtime.get("provider"), base_url=runtime.get("base_url"), tier=tier) is not None


# /reasoning display-toggle arguments -> show_reasoning value.
_REASONING_DISPLAY_TOGGLES = {"show": True, "on": True, "hide": False, "off": False}


def _model_switch_skew_guard() -> Optional[str]:
    """Refuse a model switch when the gateway is running stale code: a first-time lazy import on
    a new code path can crash on a stale cached dependency. Scoped to the highest-risk trigger."""
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=t("gateway.model.err_skew", boot_rev=boot_rev, disk_rev=disk_rev),
    )


async def _persist_model_switch_to_config(result, config_path) -> None:
    """Write-through a resolved /model switch to the profile config at ``config_path``, off the
    event loop (the route comparison can do cold-start disk I/O)."""
    from hermes_cli.model_switch import persist_model_selection
    await asyncio.to_thread(persist_model_selection, result, config_path)


@dataclasses.dataclass
class _ModelSwitchContext:
    """Everything a /model switch needs beyond the target: current route + persistence policy."""

    session_key: str
    source: Any
    config_path: Any
    persist_global: bool
    one_turn: bool = False
    reasoning_effort: str = ""  # `--reasoning <level>` riding with the pick (typed path only)
    restore_snapshot: Optional[dict] = None
    current_model: str = ""
    current_provider: str = "openrouter"
    # The provider actually configured/overridden (None = unset): ``current_provider`` defaults to
    # openrouter for switch_model, which must not label the configured model in metrics.
    route_provider: Optional[str] = None
    current_base_url: str = ""
    current_api_key: str = ""
    user_provs: Any = None
    custom_provs: Any = None
    excluded_provs: list = dataclasses.field(default_factory=list)

    def read_config(self) -> None:
        """Fill the current route from ``config_path``; fail-open to the defaults."""
        from gateway.run import _load_gateway_config
        try:
            cfg = _load_gateway_config(config_path=self.config_path)
            if not cfg:
                return
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                self.current_model = model_cfg.get("default", "")
                self.current_provider = model_cfg.get("provider", self.current_provider)
                self.route_provider = model_cfg.get("provider")
                self.current_base_url = model_cfg.get("base_url", "")
            self.user_provs = cfg.get("providers")
            try:
                from hermes_cli.config import get_compatible_custom_providers
                self.custom_provs = get_compatible_custom_providers(cfg)
            except Exception:
                self.custom_provs = cfg.get("custom_providers")
            excl = cfg.get("model_catalog", {}).get("excluded_providers")
            if isinstance(excl, list):
                self.excluded_provs = excl
        except Exception:
            pass

    def apply_override(self, override: dict) -> None:
        """A session /model override supersedes the configured route."""
        if override:
            self.current_model = override.get("model", self.current_model)
            self.current_provider = override.get("provider", self.current_provider)
            self.route_provider = override.get("provider", self.route_provider)
            self.current_base_url = override.get("base_url", self.current_base_url)
            self.current_api_key = override.get("api_key", self.current_api_key)



_TEXT_LISTING_MODELS = 5


def _model_provider_listing_lines(providers) -> list[str]:
    """Text-list body for ``/model`` with no args on platforms without a picker."""
    lines: list[str] = []
    for p in providers:
        tag = t("gateway.model.current_tag") if p["is_current"] else ""
        lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
        if p["models"]:
            shown = p["models"][:_TEXT_LISTING_MODELS]  # uncapped rows arrive full; this is a preview
            model_strs = ", ".join(f"`{m}`" for m in shown)
            hidden = p["total_models"] - len(shown)
            extra = t("gateway.model.more_models_suffix", count=hidden) if hidden > 0 else ""
            lines.append(f"  {model_strs}{extra}")
        elif p.get("api_url"):
            lines.append(f"  `{p['api_url']}`")
        lines.append("")
    return lines


def _redact_secrets(text: str) -> str:
    """Desensitize API keys/tokens/passwords in log/error messages.

    Masks sk-xxx, Bearer tokens, and password-like values so the relay
    station (proxy/gateway) cannot harvest credentials from logs.
    """
    import re
    if not text:
        return text
    # sk-... (OpenAI-style keys)
    text = re.sub(r"sk-[A-Za-z0-9_\-\.]{8,}", "sk-***", text)
    # Bearer tokens
    text = re.sub(r"Bearer [A-Za-z0-9_\-\.\~\+\/]{8,}", "Bearer ***", text)
    # x-api-key header values in debug strings
    text = re.sub(r"(x-api-key[\'\"]?\s*[:=]\s*[\'\"]?)[A-Za-z0-9_\-\.]{8,}", r"\1***", text)
    return text


def _detect_protocol(base_url: str, api_key: Optional[str] = None):
    """Auto-detect provider protocol by pulling the model list.

    Two rounds:
      Round 1: {base}/v1/models (OpenAI standard)
      Round 2: {base}/models (no /v1 prefix, for gateways like newapi)
    The protocol is determined by which round successfully returns a model
    list — not just endpoint existence.

    Returns (protocol, models) where protocol is "openai"/"anthropic"/None
    and models is a list of model id strings (may be empty).
    Never raises.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return None, []

    def _try_models_list(url):
        """GET url, return list of model ids or None on failure."""
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key or ''}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    return None
                models = []
                for it in items:
                    mid = it.get("id") if isinstance(it, dict) else None
                    if mid:
                        models.append(str(mid))
                return models
        except Exception:
            return None

    # Round 1: standard OpenAI path
    models = _try_models_list(base + "/v1/models")
    if models is not None:
        return "openai", models
    # Round 2: no /v1 prefix
    models = _try_models_list(base + "/models")
    if models is not None:
        return "openai", models

    # Fallback: probe Anthropic Messages API (both paths) to at least
    # identify the protocol when no models endpoint exists.
    for _path in ("/v1/messages", "/messages"):
        try:
            payload = json.dumps({
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            req = urllib.request.Request(
                base + _path,
                data=payload,
                headers={
                    "x-api-key": api_key or "",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10):
                return "anthropic", []
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                return "anthropic", []
        except Exception:
            pass
    return None, []


def _hermes_home_path(*parts) -> str:
    """按 HERMES_HOME 拼路径。

    2026-10-19：本机 HERMES_HOME 是 /home/ubuntu/.hermes，但这一层自定义命令里
    多处写死了 /root/.hermes/config.yaml（那是打包机 root 用户的家目录）——
    结果 /addadmin、/listadmin、/pfailover 等命令全部打不开文件。统一走这里。
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("HERMES_HOME") or (_P.home() / ".hermes")
    return str(_P(base).joinpath(*parts))


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime, /reasoning, /fast, /personality)."""

    async def _send_inline_submenu(self, event: MessageEvent, menu_key: str) -> bool:
        """Try to pop an inline-button submenu for a Telegram command.

        Returns True when the submenu was sent, False on any failure so the
        caller falls back to the original plain-text flow. Never raises.
        """
        try:
            chat_id = getattr(getattr(event, "source", None), "chat_id", None)
            if not chat_id:
                return False
            # Only Telegram supports inline keyboards; other platforms fall back.
            platform = getattr(event, "platform", "unknown")
            try:
                platform_val = str(getattr(platform, "value", platform)).lower()
            except Exception:
                platform_val = "unknown"
            if platform_val not in ("telegram", "unknown"):
                # "unknown" = MessageEvent without platform attr (be lenient).
                return False
            from gateway.telegram_menu import send_submenu_inline
            return await send_submenu_inline(chat_id, menu_key)
        except Exception as e:
            logger.debug("_send_inline_submenu failed for key=%s: %s", menu_key, e)
            return False

    # ----------------------------------------------------------------- /model

    async def _perform_model_switch(
        self, ctx: _ModelSwitchContext, raw_input: str, explicit_provider, source
    ):
        """Resolve a /model switch off-loop. Returns ``(result, None)`` or ``(None, error_text)``."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import switch_model

        skew_error = _model_switch_skew_guard()
        if skew_error:
            return None, skew_error
        # Off-loop: switch_model() can hit a synchronous models.dev fetch (15s) on a cold cache.
        result = await asyncio.to_thread(
            switch_model, raw_input=raw_input, current_provider=ctx.current_provider,
            current_model=ctx.current_model, current_base_url=ctx.current_base_url,
            current_api_key=ctx.current_api_key, is_global=ctx.persist_global,
            explicit_provider=explicit_provider, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs,
        )
        if not result.success:
            return None, t("gateway.model.error_prefix", error=result.error_message)
        try:
            from hermes_cli.context_switch_guard import enrich_model_switch_warnings_for_gateway
            # Off-loop: merge_preflight_compression_warning() runs the sync provider probe ladder.
            await asyncio.to_thread(
                enrich_model_switch_warnings_for_gateway, result, self, session_key=ctx.session_key,
                source=source, custom_providers=ctx.custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)
        return result, None

    def _switch_cached_agent_model(self, result, ctx: _ModelSwitchContext, picker: bool) -> Optional[str]:
        """In-place swap on the cached agent; returns the error reply when it failed.

        The agent rolls back to the OLD model/client and re-raises; the commit (DB, override,
        eviction, config) is aborted so the next message doesn't rebuild a broken agent.
        """
        cached_agent = self._cached_agent_for(ctx.session_key)
        if cached_agent is None:
            return None
        try:
            cached_agent.switch_model(
                new_model=result.new_model, new_provider=result.target_provider,
                api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode,
                capabilities=getattr(result, "runtime_capabilities", None),
            )
        except Exception as exc:
            logger.warning(
                "%s model switch failed for cached agent: %s", "Picker" if picker else "In-place", exc
            )
            return t(
                "gateway.model.error_prefix",
                error=t("gateway.model.err_switch_failed", model=result.new_model, error=exc, current=ctx.current_model),
            )
        return None

    async def _record_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        """Persist a committed switch: session DB, next-turn note, config write-through, override map.

        Returns the warning for a ``--global`` switch whose ``config.yaml`` write or stale-override
        cleanup failed (the switch then stays a session override), else ``None``.
        """
        from hermes_cli.model_switch import format_model_for_display

        # Persist the new model to the session DB so the dashboard shows the updated model (#34850).
        _sess_db = getattr(self, "_session_db", None)
        if _sess_db is not None:  # so the dashboard shows the updated model
            try:
                _sess_entry = await self.async_session_store.get_or_create_session(source)
                # Typed path: consume the auto-reset flag so the next message's cleanup does not
                # wipe the override stored below.
                if not picker and getattr(_sess_entry, "was_auto_reset", False):
                    # See #48031.
                    _sess_entry.was_auto_reset = False
                await _sess_db.update_session_model(
                    _sess_entry.session_id, result.new_model, provider=result.target_provider,
                    base_url=result.base_url, api_mode=result.api_mode,
                )
            except Exception as exc:
                logger.debug("Failed to persist model switch to DB: %s", exc)
        # Prepended to the next user message (no system messages mid-history). Display form strips
        # opaque Palantir RID prefixes; the override map keeps the full ID for the wire.
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[ctx.session_key] = (
            f"[Note: model was just switched from {format_model_for_display(ctx.current_model)} to "
            f"{format_model_for_display(result.new_model)} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        self._session_model_overrides[ctx.session_key] = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        if one_turn:
            # A repeated --once before the turn runs must keep the EARLIEST snapshot: the later
            # command's snapshot is the first temporary model, not the user's standing override.
            self._claim_one_turn_restore(ctx.session_key, ctx.restore_snapshot)
        elif not picker and hasattr(self, "_pending_one_turn_model_restores"):
            self._pending_one_turn_model_restores.pop(ctx.session_key, None)
        # A --global switch has ONE durable authority: config.yaml. Write it first; on success drop
        # the session override (memory + store) — a redundant copy would shadow every later global
        # change after a restart (#100314: a stale override resumed `gpt-5.6-sol-900k` as the base
        # 272K model). On failure keep the override so the switch truthfully survives as session-only.
        global_error: Optional[str] = None
        if ctx.persist_global:
            try:
                await _persist_model_switch_to_config(result, ctx.config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        # Precedence is session > channel_overrides > config.yaml: in a chat with a channel_overrides
        # model/provider the session override must stay, or the next turn runs the channel model.
        if ctx.persist_global and global_error is None and self._channel_override_for(source) is None:
            try:
                await self.async_session_store.set_model_override(ctx.session_key, None)
            except Exception as e:
                # Store still holds the stale copy: keep memory in agreement and report it (#100314).
                logger.warning("Failed to clear persisted session model override: %s", e)
                global_error = t("gateway.model.err_stale_override", error=e)
            else:
                self._session_model_overrides.pop(ctx.session_key, None)
        # Non-secret write-through so the override survives a restart (api_key/api_mode are
        # re-resolved on rehydration); a --once override must NOT outlive a restart.
        # Write-through the non-secret parts (model/provider/base_url) to the session store so the override
        # survives a gateway restart. api_key/api_mode are never persisted — they are re-resolved via
        # runtime provider resolution on rehydration. /model --once is intentionally EXCLUDED from the
        # write-through: a one-turn override must never survive a restart. The persisted value stays at the
        # pre-once state (the prior session override, or nothing), which is exactly what the finally-restore
        # reverts the in-memory dict to. (#29923 review defect: the original implementation wrote through,
        # so a crash before the restore rehydrated the once-model permanently.)
        elif not one_turn:
            try:
                await self.async_session_store.set_model_override(
                    ctx.session_key, self._session_model_overrides[ctx.session_key]
                )
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        self._evict_cached_agent(ctx.session_key)  # next turn builds fresh from the override
        return global_error

    async def _model_switch_confirmation(
        self, result, ctx: _ModelSwitchContext, *, one_turn: bool, picker: bool,
        global_error: Optional[str] = None,
    ) -> str:
        """Confirmation text with full metadata (display form shortens opaque Palantir IDs)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import format_model_for_display, resolve_display_context_length_async

        lines = [
            t("gateway.model.switched", model=format_model_for_display(result.new_model)),
            t("gateway.model.provider_label", provider=result.provider_label or result.target_provider),
        ]
        # Provider-aware chain: Codex OAuth, Copilot and Nous caps win over the raw models.dev entry.
        mi = result.model_info
        model_cfg: dict = {}
        config_ctx = None
        with contextlib.suppress(Exception):  # fail-open on config read errors
            model_cfg = _load_gateway_config().get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("context_length") is not None:
                config_ctx = int(model_cfg["context_length"])
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        ctx_len = await resolve_display_context_length_async(
            result.new_model, result.target_provider,
            base_url=result.base_url or ctx.current_base_url or "",
            api_key=result.api_key or ctx.current_api_key or "", model_info=mi,
            custom_providers=ctx.custom_provs, config_context_length=config_ctx,
            configured_model=model_cfg.get("default") or model_cfg.get("model"),
            configured_provider=model_cfg.get("provider"),
            configured_base_url=model_cfg.get("base_url"),
        )
        if ctx_len:
            lines.append(t("gateway.model.context_label", tokens=f"{ctx_len:,}"))
        if mi and mi.max_output:
            lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
        if mi:
            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
        openrouter_claude = (
            base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower()
        )
        if not picker and (openrouter_claude or result.api_mode == "anthropic_messages"):
            lines.append(t("gateway.model.prompt_caching_enabled"))
        if result.warning_message:
            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
        if ctx.persist_global and global_error is not None:
            # Never claim a clean global commit the disk did not take (#100314).
            lines.append(t("gateway.model.warning_prefix", warning=global_error))
            lines.append(t("gateway.model.session_only_hint"))
        elif ctx.persist_global:
            lines.append(t("gateway.model.saved_global"))
        elif one_turn:
            lines.append(t("gateway.model.next_turn_only"))
        else:
            lines.append(t("gateway.model.session_only_hint"))
        return "\n".join(lines)

    async def _commit_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool = False
    ) -> str:
        """Apply a resolved switch (cached agent, session, config) and build the confirmation; shared
        by the typed path and the picker callback (``picker=True`` never carries --once).

        Entry for the picker / cost-confirm callbacks, which fire outside ``_handle_model_command``
        and take the switch lock themselves; the typed path already holds it."""
        async with self._model_switch_lock():
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=picker)

    def _channel_override_for(self, source):
        """This chat's ``channel_overrides`` entry (model/provider), or None."""
        from gateway.run import _get_channel_override
        cfg = getattr(self, "config", None)
        if not cfg or source is None:
            return None
        return _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )

    def _model_switch_lock(self) -> asyncio.Lock:
        """Runner-wide lock over a /model command's read-resolve-commit. Slash commands bypass the busy
        guard while no agent runs, so a second /model on the same (or another) session otherwise
        interleaves with the first's store/config awaits: two ``--global`` picks left config.yaml on
        whichever thread wrote last and a ``--global`` cleanup wiped a session pick issued after it
        (#100314). Lazy: the runner is built without __init__ in tests."""
        lock = self.__dict__.get("_model_switch_lock_obj")
        if lock is None:
            lock = self.__dict__["_model_switch_lock_obj"] = asyncio.Lock()
        return lock

    def _record_switch_metrics(self, result, ctx: _ModelSwitchContext, source) -> None:
        """Slash dispatch does not install the routed profile's scope, so a multiplexed runner binds
        the owning home for the switch row and its switch_away friction."""
        from hermes_cli.observability.shared_metrics_events import record_model_switch
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with contextlib.suppress(Exception):
                home = self._resolve_profile_home_for_source(source)
        session_id = None
        with contextlib.suppress(Exception):
            session_id = getattr(self._cached_agent_for(ctx.session_key), "session_id", None)
        token = set_hermes_home_override(str(home)) if home else None
        try:
            record_model_switch(
                from_provider=ctx.route_provider, to_provider=result.target_provider, surface="gateway",
                from_model=ctx.current_model, session_id=session_id)
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    async def _commit_model_switch_locked(self, result, ctx: _ModelSwitchContext, *, source, picker: bool) -> str:
        one_turn = False if picker else ctx.one_turn
        error = self._switch_cached_agent_model(result, ctx, picker)
        if error is not None:
            return error
        self._record_switch_metrics(result, ctx, source)
        global_error = await self._record_model_switch(result, ctx, source=source, one_turn=one_turn, picker=picker)
        reply = await self._model_switch_confirmation(
            result, ctx, one_turn=one_turn, picker=picker, global_error=global_error,
        )
        if ctx.reasoning_effort and not one_turn:
            # `/model X --reasoning <level>`: same applier as /reasoning, same scope as the pick.
            # The record step already evicted the cached agent, so the pin lands on the rebuild.
            from gateway.run import _platform_config_key
            reply += "\n" + self._apply_reasoning_selection(
                ctx.session_key, _platform_config_key(source.platform), ctx.reasoning_effort,
                persist_global=ctx.persist_global and global_error is None)
        return reply

    async def _send_model_picker(self, event: MessageEvent, source, adapter, session_key: str, listing_kwargs: dict, on_model_selected) -> bool:
        """Send the interactive /model picker; False when nothing was sent (text fallback). *source*
        is session-key-normalized so the picker's thread metadata lands where the next turn reads."""
        from hermes_cli.model_switch_providers import list_picker_providers
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(
                list_picker_providers, max_models=50, include_moa=True, **listing_kwargs
            )
        except Exception:
            providers = []
        if not providers:
            return False
        result = await adapter.send_model_picker(
            chat_id=source.chat_id, providers=providers,
            current_model=listing_kwargs["current_model"],
            current_provider=listing_kwargs["current_provider"], session_key=session_key,
            on_model_selected=on_model_selected,
            metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
        )
        return bool(result.success)

    async def _model_listing_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home
    ) -> Optional[str]:
        """``/model`` with no args: interactive picker where supported, else the text list."""
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.providers import get_label

        listing_kwargs = dict(
            current_provider=ctx.current_provider, current_base_url=ctx.current_base_url,
            current_model=ctx.current_model, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs, excluded_providers=ctx.excluded_provs,
            # Chat `/model` is a read path: catalogs come from the disk cache and stale ones warm
            # in the background, and only the selected custom endpoint is probed live, so one
            # degraded provider can't stall the reply (#74003). Mirrors the GUI read path.
            non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=True,
        )
        adapter = self._delivery_adapter_for(ctx.source)
        if adapter is not None and getattr(type(adapter), "send_model_picker", None) is not None:
            async def _picker_switch(model_id: str, provider_slug: str) -> str:
                # The picker callback binds the raw event source (pre-normalization).
                result, error = await self._perform_model_switch(ctx, model_id, provider_slug, event.source)
                if error is not None:
                    return error
                return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

            async def _on_model_selected(_chat_id: str, model_id: str, provider_slug: str) -> str:
                if profile_home is None:
                    return await _picker_switch(model_id, provider_slug)
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(profile_home):
                    return await _picker_switch(model_id, provider_slug)

            if await self._send_model_picker(event, ctx.source, adapter, ctx.session_key, listing_kwargs, _on_model_selected):
                return None  # Picker sent — adapter handles the response

        lines = [t("gateway.model.current_label", model=ctx.current_model or t("gateway.shared.unknown_value"),
                   provider=get_label(ctx.current_provider)), ""]
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(list_authenticated_providers, max_models=_TEXT_LISTING_MODELS, **listing_kwargs)
            lines.extend(_model_provider_listing_lines(providers))
        except Exception:
            pass
        lines.append(t("gateway.model.usage_switch_model"))
        lines.append(t("gateway.model.usage_switch_provider"))
        lines.append(t("gateway.model.usage_persist"))
        return "\n".join(lines)

    async def _model_selection_guard_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, result
    ) -> tuple[bool, Optional[str]]:
        """Selection-guard confirmation for the typed path (pickers confirm via their own UI).

        The unified registry (cost + data-policy guards) runs off-loop — pricing lookups may hit
        models.dev on a cache miss. Returns ``(fired, reply)``; the reply is None when the platform
        rendered confirm buttons itself.
        """
        try:
            from hermes_cli.model_selection_guards import (
                combined_selection_warning, selection_context_for_agent)
            warning = await asyncio.to_thread(
                combined_selection_warning, result.new_model, provider=result.target_provider,
                base_url=result.base_url or ctx.current_base_url or "",
                api_key=result.api_key or ctx.current_api_key or "", model_info=result.model_info,
                selection_context=selection_context_for_agent(self._cached_agent_for(ctx.session_key)),
            )
        except Exception:
            warning = None
        if warning is None:
            return False, None

        async def _on_cost_confirm(choice: str) -> str:
            if choice == "cancel":
                return t("gateway.model.switch_cancelled", model=ctx.current_model or t("gateway.shared.unknown_value"))
            # "once" and "always" both proceed — selection guards have no persistent opt-out.
            return await self._commit_model_switch(result, ctx, source=ctx.source)

        _p = self._typed_command_prefix_for(event.source.platform)
        message = t("gateway.model.guard_confirm", title=warning.title, message=warning.message, prefix=_p)
        return True, await self._request_slash_confirm(
            event=event, command="model", title=warning.title, message=message, handler=_on_cost_confirm,
        )

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model. Taken under the switch lock BEFORE the first await so
        concurrent commands commit in issue order (see ``_model_switch_lock``)."""
        # Telegram: bare /model opens the built-in provider → model picker.
        # [mickvvw 2026-10-18] The custom 「模型切换」submenu no longer intercepts bare
        # /model: its two buttons only re-injected /model (which re-sent the same
        # submenu - a dead loop) and /models (not a registered command), so the
        # built-in provider picker could never appear. Re-enable the block below to
        # restore the old behaviour.
        try:
            args = (event.get_command_args() or "").strip()
        except Exception:
            args = ""
        # if not args:
        #     try:
        #         if await self._send_inline_submenu(event, "model"):
        #             return None
        #     except Exception:
        #         pass
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        _model_args = self._strip_button_label("/model", event.get_command_args()).strip()
        request = parse_model_switch_args(_model_args)  # single-owner parser
        if request.errors:
            return f"❌ {request.error_messages()[0]}"  # gateway decoration over canonical copy
        if request.force_refresh:  # bust the disk cache so the picker shows live data
            with contextlib.suppress(Exception):
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
        # Normalize like a message turn (Telegram DM topic recovery) before deriving the override
        # key, so the override lands under the key the next turn reads.
        # Check for session override. See #30479.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        ctx = _ModelSwitchContext(
            # Gateway routing columns — forward ALL of them at CREATE time, same fix as the
            # compression-rotation bug in agent/conversation_compression.py. Without these, the branched
            # child row has NULL routing columns until switch_session() below calls
            # _record_gateway_session_peer() — a crash/kill anywhere between here and there (most plausibly
            # mid-history-copy, since each append_message call a few lines down is independently
            # best-effort) leaves the branch permanently unroutable: unreachable by chat/thread lookup, and
            # unreachable via /resume's IDOR guard too (which requires the row's chat_id/thread_id to match
            # the caller's). user_id is critical for the fallback lookup path (hermes_state.py:1994-2009)
            # that searches by the complete peer tuple when session_key doesn't match. origin_json and
            # display_name complete the identity (same shape as the reset path's db_create_kwargs in
            # gateway/session.py, #82633) so consumers that read routing/presentation data from state.db
            # (mcp_serve, mirror, channel directory) see the branch row fully formed with zero backfill gap.
            session_key=session_key,
            source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml",
            persist_global=resolve_persist_behavior(
                request.is_global, request.is_session, is_once=request.is_once,
                explicit_provider=request.explicit_provider,
            ),
            one_turn=request.is_once,
            reasoning_effort=request.reasoning_effort,
            restore_snapshot=self._snapshot_session_model_override(session_key) if request.is_once else None,
        )
        ctx.read_config()
        ctx.apply_override(self._session_model_overrides.get(session_key, {}))
        if not request.target and not request.explicit_provider:
            return await self._model_listing_reply(event, ctx, profile_home)
        result, error = await self._perform_model_switch(ctx, request.target, request.explicit_provider, source)
        if error is not None:
            return error
        guard_fired, guard_reply = await self._model_selection_guard_reply(event, ctx, result)
        if guard_fired:
            return guard_reply
        return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)

    # -------------------------------------------------- /codex-runtime, /personality

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str:
        """Handle /codex-runtime; a real change evicts the cached agent so the new api_mode applies
        on the next message (avoids prompt-cache invalidation mid-session)."""
        from hermes_cli import codex_runtime_switch as crs

        new_value, errors = crs.parse_args(event.get_command_args().strip() if event else "")
        if errors:
            return "❌ " + "\n❌ ".join(errors)
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return t("gateway.codex_runtime.config_load_failed", error=exc)
        result = crs.apply(
            load_config(), new_value, persist_callback=(save_config if new_value is not None else None),
        )
        if result.success and new_value is not None and result.requires_new_session:
            try:
                self._evict_cached_agent(self._session_key_for_source(event.source))
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change", exc_info=True)
        return f"{'✓' if result.success else '✗'} {result.message}"

    async def _handle_addmodel_command(self, event: MessageEvent) -> str:
        """Direct users to the credential-safe provider setup surface."""
        return t("gateway.addmodel.instructions")


    # ------------------------------------------------------- /addprovider
    # Add a model provider from chat: password-gated, DM-only, admin-only.
    # Secrets travel as dedicated sub-commands (/pkey, /ppass, /padd) so they
    # never enter the agent transcript; each secret message is best-effort
    # deleted right after it is read and never echoed back. All persistence
    # goes through hermes_cli.provider_access, shared with the agent tool.

    _PROVIDER_ACCESS_CUSTOM_CHOICE = "__custom__"

    def _pa_pending_store(self) -> dict:
        store = getattr(self, "_provider_access_pending", None)
        if not isinstance(store, dict):
            store = {}
            self._provider_access_pending = store
        return store

    def _pa_pending_key(self, source) -> tuple:
        return (str(getattr(source, "platform", "")),
                str(getattr(source, "chat_id", "")),
                str(getattr(source, "user_id", "")))

    @staticmethod
    def _strip_button_label(command: str, args: str) -> str:
        """底部按钮标签（"/psoul 提示词管理"）带的说明文字，按「无参数」处理。

        不这么做的话，点按钮等于给命令塞了一个假参数：/psoulnew 会把「添加提示词」
        当成提示词名字直接建一个；/addprovider 会去找名为「添加供应商」的供应商。
        """
        text = (args or "").strip()
        if not text:
            return ""
        try:
            from gateway.telegram_menu import button_label_text
            if text == button_label_text(command):
                return ""
        except Exception:
            pass
        return text

    def _pa_get_pending(self, source) -> Optional[dict]:
        import time

        store = self._pa_pending_store()
        entry = store.get(self._pa_pending_key(source))
        if not isinstance(entry, dict):
            return None
        if entry.get("expires", 0) < time.time():
            store.pop(self._pa_pending_key(source), None)
            return None
        return entry

    def _pa_set_pending(self, source, state: dict) -> None:
        import time

        from hermes_cli.provider_access import PENDING_TTL_SECONDS

        state = dict(state)
        state["expires"] = time.time() + PENDING_TTL_SECONDS
        self._pa_pending_store()[self._pa_pending_key(source)] = state

    def _pa_clear_pending(self, source) -> None:
        self._pa_pending_store().pop(self._pa_pending_key(source), None)

    def _pa_guard(self, event: MessageEvent) -> Optional[str]:
        """DM-only + admin + master-switch gate. Returns an error text or None."""
        source = event.source
        if str(getattr(source, "chat_type", "")).strip().lower() not in {"dm", "direct", "private"}:
            return t("gateway.addprovider.dm_only")
        from gateway.slash_access import policy_for_runner_source

        policy = policy_for_runner_source(self, source)
        # 2026-10-18 修正：原写法是 `if not policy.enabled or not policy.is_admin(...)`，
        # 在「没配 allow_admin_from」的安装上 gating 是关闭的 → 条件成立 → **一律拒绝**，
        # 于是 /addprovider 及其子菜单全部回「只有网关管理员可以使用」。
        # 而 SlashAccessPolicy.is_admin 的约定是「gating 关闭 => 人人都算 admin」，
        # 所以按其它处的写法只在 gating 开启时才要求 admin。
        if policy.enabled and not policy.is_admin(getattr(source, "user_id", None)):
            return t("gateway.addprovider.admin_only")
        from hermes_cli import provider_access as pa

        allowed, _reason = pa.check_gate()
        if not allowed:
            return t("gateway.addprovider.disabled")
        return None

    def _pa_dm_only_guard(self, event: MessageEvent) -> Optional[str]:
        """DM-only gate WITHOUT the admin check.

        Used by /ppass: the user runs a password-gated operation first and
        is then prompted to send "/ppass <password>". Requiring admin here
        would block the password step itself for non-admin users who are
        allowed to attempt it; the password verification is the real gate.
        Returns an error text or None. Never raises.
        """
        try:
            source = event.source
            if str(getattr(source, "chat_type", "")).strip().lower() not in {"dm", "direct", "private"}:
                return t("gateway.addprovider.dm_only")
        except Exception:
            return t("gateway.addprovider.dm_only")
        return None

    async def _pa_try_delete_message(self, event: MessageEvent) -> None:
        """Best-effort delete of the user's secret message.

        Telegram bots can delete user messages in groups (as admin) but NOT in
        private chats, so the user is always also told to delete it themselves.
        """
        try:
            adapter = self._delivery_adapter_for(event.source)
            delete = getattr(adapter, "delete_message", None)
            message_id = getattr(event, "message_id", None)
            if delete is not None and message_id:
                await delete(chat_id=event.source.chat_id, message_id=str(message_id))
        except Exception:
            logger.debug("addprovider: message delete failed", exc_info=True)

    def _pa_provider_choices(self) -> list:
        from hermes_cli import provider_access as pa

        choices = [{"value": row["id"], "label": row["name"], "is_current": False}
                   for row in pa.list_providers_with_sources() if row["kind"] == "builtin"]
        choices.append({"value": self._PROVIDER_ACCESS_CUSTOM_CHOICE,
                        "label": t("gateway.addprovider.choice_custom"), "is_current": False})
        return choices

    async def _handle_addprovider_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /addprovider — add / list / delete model providers from chat."""
        denied = self._pa_guard(event)
        if denied:
            return denied
        args = self._strip_button_label("/addprovider", event.get_command_args()).strip().split()
        if args and args[0].lower() == "list":
            return await self._pa_list_providers(event)
        if args and args[0].lower() in {"del", "delete", "remove"}:
            return await self._pa_start_delete(event, " ".join(args[1:]).strip())
        skip_submenu = False
        if args and args[0].lower() in {"new", "add", "添加", "新增"}:
            args = []            # 从子菜单「添加供应商」来的
            skip_submenu = True  # 别再弹回同一个子菜单（否则就是原地打转）
        if args:
            return await self._pa_begin_for_provider(event, " ".join(args))

        # 无参数：先弹供应商管理子菜单，失败则走原逻辑
        if not skip_submenu:
            try:
                if await self._send_inline_submenu(event, "provider"):
                    return ""
            except Exception:
                pass

        async def _on_choice(_chat_id: str, value: str) -> str:
            return await self._pa_begin_for_provider(event, value)

        session_key = self._session_key_for_source(event.source)
        sent = await self._try_send_choice_picker(
            event, session_key,
            title=t("gateway.addprovider.picker_title"),
            choices=self._pa_provider_choices(),
            on_choice_selected=_on_choice,
        )
        if sent:
            return None
        # Text fallback for platforms without a choice picker.
        lines = [t("gateway.addprovider.pick_text_header")]
        for choice in self._pa_provider_choices():
            lines.append(f"  `/addprovider {choice['value']}` — {choice['label']}")
        return "\n".join(lines)

    async def _pa_begin_for_provider(self, event: MessageEvent, value: str) -> str:
        denied = self._pa_guard(event)
        if denied:
            return denied
        value = (value or "").strip()
        if value == self._PROVIDER_ACCESS_CUSTOM_CHOICE or value.lower() == "custom":
            self._pa_set_pending(event.source, {"step": "await_custom"})
            return t("gateway.addprovider.custom_instructions")
        from providers import get_provider_profile

        profile = get_provider_profile(value)
        if profile is None:
            return t("gateway.addprovider.unknown_provider", name=value)
        self._pa_set_pending(event.source, {"step": "await_password", "provider": profile.name,
                                            "next": "await_key"})
        label = profile.display_name or profile.name
        return t("gateway.addprovider.password_prompt", provider=label)

    async def _handle_ppass_command(self, event: MessageEvent) -> str:
        """Handle /ppass — verify the access password for a pending operation.

        DM-only, deliberately NOT admin-gated: the user first runs a
        password-gated operation and is then prompted to send
        "/ppass <password>". The password verification below is the real gate.
        """
        denied = self._pa_dm_only_guard(event)
        if denied:
            return denied
        pending = self._pa_get_pending(event.source)
        if not pending or pending.get("step") != "await_password":
            return t("gateway.addprovider.no_pending")
        password = (event.get_command_args() or "").strip()
        await self._pa_try_delete_message(event)
        if not password:
            return t("gateway.addprovider.password_empty")
        from hermes_cli import provider_access as pa

        ok, reason = pa.verify_password(password)
        if not ok:
            if reason.startswith("locked:"):
                return t("gateway.addprovider.password_locked", seconds=reason.split(":", 1)[1])
            return t("gateway.addprovider.password_wrong")
        self._pa_clear_pending(event.source)
        nxt = pending.get("next")
        if nxt == "await_key":
            self._pa_set_pending(event.source, {"step": "await_key", "provider": pending.get("provider")})
            return t("gateway.addprovider.key_prompt")
        if nxt == "await_custom_key":
            return await self._pa_save_custom(event, pending)
        if nxt == "await_delete":
            target = pending.get("target")
            self._pa_set_pending(event.source, {"step": "await_delete_confirm", "target": target})
            return t("gateway.addprovider.delete_confirm", target=target)
        if nxt == "soul_new":
            return await self._pa_soul_new(event, pending, password)
        if nxt == "soul_switch":
            return await self._pa_soul_switch(event, pending, password)
        if nxt == "soul_del":
            return await self._pa_soul_del(event, pending, password)
        if nxt == "soul_set":
            return await self._pa_soul_set(event, pending, password)
        if nxt == "soul_add":
            return await self._pa_soul_add(event, pending, password)
        if nxt == "backup_now":
            return await self._pa_backup_now(event, pending, password)
        if nxt == "backup_restore":
            return await self._pa_backup_restore_verified(event, pending, password)
        return t("gateway.addprovider.no_pending")

    async def _handle_pkey_command(self, event: MessageEvent) -> str:
        """Handle /pkey — receive the API key for a pending built-in provider."""
        denied = self._pa_guard(event)
        if denied:
            return denied
        pending = self._pa_get_pending(event.source)
        if not pending or pending.get("step") != "await_key":
            return t("gateway.addprovider.no_pending")
        api_key = (event.get_command_args() or "").strip()
        await self._pa_try_delete_message(event)
        if not api_key:
            return t("gateway.addprovider.key_empty")
        provider = pending.get("provider")
        self._pa_clear_pending(event.source)
        from hermes_cli import provider_access as pa

        result = await asyncio.to_thread(pa.add_builtin_key, provider, api_key, pa.SOURCE_BOT)
        if not result.get("ok"):
            return t("gateway.addprovider.save_failed_detail", error=result.get("error") or "")
        label = result.get("display_name") or provider
        if not result.get("verified"):
            return t("gateway.addprovider.saved_unverified", provider=label)
        return t("gateway.addprovider.saved", provider=label)

    async def _handle_padd_command(self, event: MessageEvent) -> str:
        """Handle /padd — one-shot custom provider: /padd <name> | <base_url> | <key> | <model1,model2>."""
        denied = self._pa_guard(event)
        if denied:
            return denied
        raw = (event.get_command_args() or "").strip()
        if not raw:
            return t("gateway.addprovider.custom_format")
        await self._pa_try_delete_message(event)
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
            return t("gateway.addprovider.custom_format")
        name, base_url, api_key = parts[0], parts[1], parts[2]
        # Models: comma-separated, optional
        models_raw = parts[3] if len(parts) > 3 else ""
        models = [m.strip() for m in models_raw.split(",") if m.strip()] if models_raw else []
        self._pa_clear_pending(event.source)
        self._pa_set_pending(event.source, {"step": "await_password", "next": "await_custom_key",
                                            "name": name, "base_url": base_url, "models": models,
                                            "api_key": api_key})
        return t("gateway.addprovider.password_prompt", provider=name)

    async def _pa_save_custom(self, event: MessageEvent, pending: dict) -> str:
        """Validate + persist a custom endpoint, then badge it."""
        from hermes_cli import provider_access as pa
        from hermes_cli.web_models import CustomEndpointUpdate
        from hermes_cli.web_routers.config_env import validate_custom_endpoint

        models = pending.get("models") or []
        # For validation, use first model or empty
        model_for_validation = models[0] if models else ""
        body = CustomEndpointUpdate(name=pending.get("name") or "", base_url=pending.get("base_url") or "",
                                    model=model_for_validation, api_key=pending.get("api_key") or "")
        try:
            result = await validate_custom_endpoint(body)
        except Exception as exc:
            logger.warning("addprovider: custom validation error: %s", exc)
            result = {"ok": False, "reachable": False, "message": _redact_secrets(str(exc))}
        if not result.get("ok") and result.get("reachable"):
            return t("gateway.addprovider.endpoint_rejected", message=result.get("message") or "")
        try:
            saved = await asyncio.to_thread(
                pa.add_custom_endpoint, pending.get("name"), pending.get("base_url"),
                pending.get("api_key"), model_for_validation, pa.SOURCE_BOT)
        except Exception as exc:
            logger.warning("addprovider: failed to save custom endpoint: %s", exc)
            return t("gateway.addprovider.save_failed")
        if not saved.get("ok"):
            return t("gateway.addprovider.save_failed_detail", error=saved.get("error") or "")
        if not result.get("ok"):
            return t("gateway.addprovider.saved_unverified", provider=pending.get("name"))

        # If models were specified, save them directly and done
        if models:
            models_str = ",".join(models)
            try:
                # Update the endpoint with the selected models
                await asyncio.to_thread(
                    pa.add_custom_endpoint, pending.get("name"), pending.get("base_url"),
                    pending.get("api_key"), models_str, pa.SOURCE_BOT)
            except Exception as exc:
                logger.warning("addprovider: failed to set models: %s", exc)
            model_list = "\n".join(f"- {m}" for m in models)
            return t("gateway.addprovider.saved", provider=pending.get("name")) + \
                f"\n\n已添加模型 ({len(models)}):\n{model_list}"

        # 没指定模型：拉列表 → 判协议 → 逐个试通 → 能用的自动加，不能用的问用户（2026-10-19）
        base_url = pending.get("base_url") or ""
        api_key = pending.get("api_key") or ""
        name = pending.get("name") or ""
        fetched = await self._pa_fetch_models_sorted(base_url, api_key)
        if not fetched:
            return t("gateway.addprovider.saved", provider=name) + "\n\n" + \
                t("gateway.addprovider.models_fetch_failed")
        all_ids = [m["id"] for m in fetched if m.get("id")]
        probe_ids = all_ids[: self._PA_PROBE_MAX]
        mode, mode_note = await self._pa_detect_api_mode(base_url, api_key, probe_ids)
        if mode != "chat_completions":
            self._pa_set_provider_api_mode(name, mode)
        results = await self._pa_probe_many(base_url, api_key, probe_ids, mode)
        good = [m for m in probe_ids if results.get(m, (False, ""))[0]]
        bad = [m for m in probe_ids if not results.get(m, (False, ""))[0]]

        # 能用的直接写进配置
        if good:
            try:
                await asyncio.to_thread(pa.add_custom_endpoint, name, base_url, api_key,
                                        ",".join(good), pa.SOURCE_BOT)
            except Exception as exc:
                logger.warning("addprovider: save probed models failed: %s", exc)

        lines = [t("gateway.addprovider.saved", provider=name), ""]
        lines.append(t("gateway.addprovider.proto_detected", mode=mode, note=mode_note))
        lines.append(t("gateway.addprovider.probe_summary", ok=len(good), bad=len(bad)))
        if good:
            lines.append("")
            lines.append(t("gateway.addprovider.probe_ok_list"))
            lines.append("\n".join(f"- {m}" for m in good[:30]))
        if bad:
            self._pa_set_pending(event.source, {
                "step": "await_failed_models_confirm",
                "name": name, "base_url": base_url, "api_key": api_key,
                "good": good, "bad": bad,
            })
            lines.append("")
            lines.append(t("gateway.addprovider.probe_bad_list"))
            for i, m in enumerate(bad[:30], 1):
                reason = (results.get(m, (False, ""))[1] or "")[:60]
                lines.append(f"{i}. {m} — {reason}")
            lines.append("")
            lines.append(t("gateway.addprovider.probe_ask"))
        elif not good:
            lines.append("")
            lines.append(t("gateway.addprovider.probe_all_failed", n=len(probe_ids)))
        if len(all_ids) > len(probe_ids):
            lines.append("")
            lines.append(t("gateway.addprovider.probe_truncated", n=len(probe_ids)))
        return "\n".join(lines)

    async def _pa_fetch_models_sorted(self, base_url: str, api_key: str) -> list:
        """Fetch models (two rounds: /v1/models then /models) and sort by rate."""
        import urllib.request
        import json as _json
        try:
            b = (base_url or "").rstrip("/")
            if not b or not api_key:
                return []
            data = None
            # Round 1: standard /v1/models
            # Round 2: no /v1 prefix (for gateways like newapi)
            for _path in ("/v1/models", "/models"):
                try:
                    q = urllib.request.Request(
                        b + _path,
                        headers={"Authorization": "Bearer " + api_key})
                    with urllib.request.urlopen(q, timeout=15) as r:
                        if r.status == 200:
                            data = _json.load(r).get("data", [])
                            break
                except Exception:
                    continue
            if data is None:
                return []
            models = []
            for m in data:
                mid = m.get("id")
                if not mid:
                    continue
                # Try to extract rate/pricing info
                rate = None
                # Common fields: pricing, price, rate, multiplier
                pricing = m.get("pricing") or {}
                if isinstance(pricing, dict):
                    # Try prompt price as rate indicator
                    prompt_price = pricing.get("prompt") or pricing.get("input")
                    if prompt_price:
                        try:
                            rate = float(str(prompt_price).replace("$", ""))
                        except (ValueError, TypeError):
                            pass
                if rate is None:
                    rate = m.get("rate") or m.get("price") or m.get("multiplier")
                    if rate is not None:
                        try:
                            rate = float(rate)
                        except (ValueError, TypeError):
                            rate = None
                models.append({"id": mid, "rate": rate})
            # Sort by rate (None last), then by id
            models.sort(key=lambda x: (x["rate"] is None, x["rate"] or 0, x["id"]))
            return models
        except Exception as exc:
            logger.warning("addprovider: fetch models error: %s", exc)
            return []

    # ---- 2026-10-19：模型自动检测（拉列表 → 判协议 → 逐个试通 → 失败的问用户）----
    _PA_PROBE_MAX = 20          # 最多自动试通多少个模型（防呆：供应商可能返回上百个）
    _PA_PROBE_TIMEOUT = 20.0
    _PA_PROBE_CONCURRENCY = 4   # 并发试通数（别太猛，上游容易 5xx）

    def _pa_http_json(self, url: str, api_key: str, payload: dict | None, timeout: float,
                      extra_headers: dict | None = None) -> tuple[bool, str]:
        """同步发一次请求 → (是否 2xx, 说明)。说明里的响应体已脱敏、截断。"""
        import json as _json
        import urllib.error
        import urllib.request
        headers = {"Authorization": "Bearer " + (api_key or ""), "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        data = _json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, f"HTTP {r.status}"
        except urllib.error.HTTPError as he:
            detail = ""
            try:
                detail = he.read(600).decode("utf-8", "replace")
            except Exception:
                pass
            detail = _redact_secrets(detail).replace("\n", " ")[:180]
            return False, f"HTTP {he.code} {detail}".strip()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {_redact_secrets(str(exc))[:120]}"

    async def _pa_probe_model(self, base_url: str, api_key: str, model: str,
                              api_mode: str = "chat_completions") -> tuple[bool, str]:
        """一次最小调用测某个模型通不通（只发 1 个 token，尽量便宜）。"""
        b = (base_url or "").rstrip("/")
        if not b or not model:
            return False, "缺少地址或模型名"
        if api_mode == "codex_responses":
            url = f"{b}/responses"
            payload = {"model": model, "input": "hi", "max_output_tokens": 16}
            extra = None
        elif api_mode == "anthropic_messages":
            url = f"{b}/messages"
            payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            extra = {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
        else:
            url = f"{b}/chat/completions"
            payload = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1, "stream": False}
            extra = None
        return await asyncio.to_thread(self._pa_http_json, url, api_key, payload,
                                       self._PA_PROBE_TIMEOUT, extra)

    async def _pa_detect_api_mode(self, base_url: str, api_key: str,
                                  models: list) -> tuple[str, str]:
        """依次试三种协议，返回第一个真能通的：(api_mode, 说明)。

        抗抖动：每个组合失败后隔 1 秒重试一次 —— 上游偶发 5xx/429 不能被当成"协议不对"。
        """
        tried = []
        for mode in ("chat_completions", "codex_responses", "anthropic_messages"):
            for m in (models or [])[:5]:
                ok, note = await self._pa_probe_model(base_url, api_key, m, mode)
                if not ok:
                    await asyncio.sleep(1.0)
                    ok, note = await self._pa_probe_model(base_url, api_key, m, mode)
                if ok:
                    return mode, f"{m} 通过（{note}）"
                tried.append(f"{mode}/{m}: {note}")
        return "chat_completions", "三种协议都没试通（默认按 chat_completions 保存）：" + "；".join(tried[:2])

    async def _pa_probe_many(self, base_url: str, api_key: str, models: list,
                             api_mode: str) -> dict:
        """并发试通多个模型 → {模型名: (是否可用, 说明)}。失败项会重试一次（抗上游抖动）。"""
        sem = asyncio.Semaphore(self._PA_PROBE_CONCURRENCY)
        out: dict = {}

        async def one(m: str) -> None:
            async with sem:
                ok, note = await self._pa_probe_model(base_url, api_key, m, api_mode)
                if not ok:
                    await asyncio.sleep(0.8)
                    ok, note = await self._pa_probe_model(base_url, api_key, m, api_mode)
                out[m] = (ok, note)

        await asyncio.gather(*(one(m) for m in models))
        return out

    def _pa_set_provider_api_mode(self, name: str, mode: str) -> bool:
        """把探测到的协议写进 providers.<name>.api_mode（只动这一个键）。"""
        try:
            from hermes_cli.config import load_config, save_config
            cfg = load_config() or {}
            providers = cfg.get("providers") or {}
            for key, entry in providers.items():
                if isinstance(entry, dict) and (key == name or entry.get("name") == name):
                    if entry.get("api_mode") != mode:
                        entry["api_mode"] = mode
                        save_config(cfg)
                        logger.info("addprovider: %s api_mode → %s", key, mode)
                    return True
            return False
        except Exception as exc:
            logger.warning("addprovider: set api_mode failed: %s", exc)
            return False

    async def _handle_paddforce_command(self, event: MessageEvent) -> str:
        """Handle /paddforce — 对「试不通的模型」做决定：yes 都加 / no 不加 / 1,3 只加指定序号。"""
        from hermes_cli import provider_access as pa

        denied = self._pa_guard(event)
        if denied:
            return denied
        pending = self._pa_get_pending(event.source)
        if not pending or pending.get("step") != "await_failed_models_confirm":
            return t("gateway.addprovider.no_pending")
        ans = (event.get_command_args() or "").strip().lower()
        name = pending.get("name") or ""
        good = list(pending.get("good") or [])
        bad = list(pending.get("bad") or [])
        if ans in {"yes", "y", "all", "都加", "全加", "加"}:
            chosen = bad
        elif ans in {"no", "n", "skip", "不加", "跳过"}:
            chosen = []
        else:
            idx = []
            for part in ans.replace("，", ",").replace(" ", ",").split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(bad):
                    idx.append(int(part) - 1)
            if not idx:
                return t("gateway.addprovider.probe_ask_invalid", n=len(bad))
            chosen = [bad[i] for i in idx]
        self._pa_clear_pending(event.source)
        final = good + chosen
        try:
            await asyncio.to_thread(pa.add_custom_endpoint, name, pending.get("base_url") or "",
                                    pending.get("api_key") or "", ",".join(final), pa.SOURCE_BOT)
        except Exception as exc:
            logger.warning("addprovider: paddforce save failed: %s", exc)
            return t("gateway.addprovider.save_failed")
        lines = [t("gateway.addprovider.probe_final", provider=name, n=len(final), extra=len(chosen))]
        if final:
            lines.append("\n".join(f"- {m}" for m in final))
        return "\n".join(lines)

    async def _handle_pmodels_command(self, event: MessageEvent) -> str:
        """Handle /pmodels — select models after fetching: /pmodels 1,3,5 | all | skip."""
        from hermes_cli import provider_access as pa

        denied = self._pa_guard(event)
        if denied:
            return denied
        pending = self._pa_get_pending(event.source)
        if not pending or pending.get("step") != "await_model_select":
            return t("gateway.addprovider.no_pending")
        text = (event.get_command_args() or "").strip()
        await self._pa_try_delete_message(event)
        name = pending.get("name") or ""
        models = pending.get("models") or []
        self._pa_clear_pending(event.source)

        if text.lower() in ("skip", "跳过"):
            return f"✅ 供应商 [{name}] 已保存，未添加模型。"

        selected = []
        if text.lower() in ("all", "全部"):
            selected = [m["id"] for m in models]
        else:
            # Parse comma-separated indices (1-based)
            try:
                indices = []
                for part in text.split(","):
                    part = part.strip()
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(models):
                            indices.append(idx)
                selected = [models[i]["id"] for i in indices]
            except Exception:
                pass

        if not selected:
            return "❌ 未选择有效模型，请重新 /padd 或手动添加。"
        models_str = ",".join(selected)
        try:
            await asyncio.to_thread(
                pa.add_custom_endpoint, name,
                pending.get("base_url") or "", pending.get("api_key") or "",
                models_str, pa.SOURCE_BOT)
        except Exception as exc:
            logger.warning("addprovider: failed to set models: %s", exc)
            return t("gateway.addprovider.save_failed")
        model_list = "\n".join(f"- {m}" for m in selected)
        return f"✅ 供应商 [{name}] 已添加 {len(selected)} 个模型:\n{model_list}"

    async def _pa_list_providers(self, event: MessageEvent) -> str:
        """List providers with their source badges."""
        from hermes_cli import provider_access as pa

        badges = {pa.SOURCE_BOT: t("gateway.addprovider.badge_bot"),
                  pa.SOURCE_CHAT: t("gateway.addprovider.badge_chat"),
                  pa.SOURCE_DESKTOP: t("gateway.addprovider.badge_desktop")}
        lines = [t("gateway.addprovider.list_header")]
        for row in pa.list_providers_with_sources():
            badge = f" {badges[row['source']]}" if row.get("source") in badges else ""
            status = "✅" if row.get("enabled", True) else "❌"
        lines.append(f"• {row['name']}{badge} {status}")
        lines.append("")
        lines.append(t("gateway.addprovider.list_hint"))
        return "\n".join(lines)

    async def _pa_start_delete(self, event: MessageEvent, target: str) -> str:
        """Begin the delete flow — requires allow_delete + password."""
        from hermes_cli import provider_access as pa

        if not target:
            return t("gateway.addprovider.delete_usage")
        if not pa.is_delete_allowed():
            return t("gateway.addprovider.delete_disabled")
        self._pa_set_pending(event.source, {"step": "await_password", "next": "await_delete",
                                            "target": target})
        return t("gateway.addprovider.delete_password_prompt", target=target)

    async def _pa_do_delete(self, event: MessageEvent, target: str) -> str:
        """Delete a custom endpoint or a built-in provider key (allow_delete already checked)."""
        from hermes_cli import provider_access as pa

        try:
            result = await asyncio.to_thread(pa.delete_provider_entry, target)
        except Exception as exc:
            logger.warning("addprovider: delete failed for %s: %s", target, exc)
            return t("gateway.addprovider.save_failed")
        if not result.get("ok"):
            error = result.get("error") or ""
            if error == "unknown_provider":
                return t("gateway.addprovider.unknown_provider", name=target)
            if error == "not_configured":
                return t("gateway.addprovider.not_configured", target=target)
            return t("gateway.addprovider.save_failed_detail", error=error)
        return t("gateway.addprovider.deleted", target=target)


    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """Handle /personality — list or set a personality (hermes_cli.personality owns the state)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.personality import (
            active_personality_name,
            available_personalities,
            describe_personality,
            persist_personality,
            resolve_personality,
        )

        args = event.get_command_args().strip()
        try:
            config = _load_gateway_config()
        except Exception:
            config = {}
        personalities = available_personalities(config)
        if not args:
            current = active_personality_name(config)
            lines = [t("gateway.personality.header"), t("gateway.personality.none_option")]
            for name, prompt in personalities.items():
                marker = " ✓" if name == current else ""
                lines.append(
                    t("gateway.personality.item", name=f"{name}{marker}", preview=describe_personality(prompt))
                )
            lines.append(t("gateway.personality.usage"))
            return "\n".join(lines)
        try:
            name, _new_prompt = resolve_personality(args, config)
        except ValueError:
            available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
            return t("gateway.personality.unknown", name=args.lower(), available=available)
        # Persists the selection only (never agent.system_prompt, a user-owned overlay) into the
        # routed profile's config.yaml; the next turn re-resolves the prompt — no process-global state.
        if not persist_personality(name):
            return t("gateway.personality.save_failed", error=t("gateway.personality.err_config_write"))
        if not name:
            return t("gateway.personality.cleared")
        return t("gateway.personality.set_to", name=name)

    # ----------------------------------------------------------- /reasoning, /fast

    def _save_gateway_config_key(self, key_path: str, value) -> bool:
        """Save a dot-separated key to config.yaml (shared by /reasoning, /fast and their pickers)."""
        from gateway.slash_commands import _nested_dict
        from gateway.run import _gateway_config_home
        from hermes_cli.config import read_user_config_raw
        config_path = _gateway_config_home() / "config.yaml"
        try:
            user_config = read_user_config_raw(config_path)  # raw: never persist merged defaults
            *parents, leaf = key_path.split(".")
            _nested_dict(user_config, *parents)[leaf] = value
            atomic_config_write(config_path, user_config)
            return True
        except Exception as e:
            logger.error("Failed to save config key %s: %s", key_path, e)
            return False

    def _set_reasoning_override(self, session_key: str, value) -> None:
        """Store (or clear with None) the session reasoning override and drop the cached agent."""
        self._set_session_reasoning_override(session_key, value)
        self._evict_cached_agent(session_key)

    def _apply_reasoning_selection(
        self, session_key: str, platform_key: str, value: str, persist_global: bool = False,
    ) -> str:
        """Apply a /reasoning argument (typed or picked) and return the reply."""
        from hermes_constants import parse_reasoning_effort

        value = (value or "").strip().lower()
        show = _REASONING_DISPLAY_TOGGLES.get(value)
        if show is not None:  # per-platform display toggle
            self._show_reasoning = show
            self._save_gateway_config_key(f"display.platforms.{platform_key}.show_reasoning", show)
            key = "gateway.reasoning.display_set_on" if show else "gateway.reasoning.display_set_off"
            return t(key, platform=platform_key)
        if value == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            self._set_session_reasoning_override(session_key, None)
            self._reasoning_config = self._load_reasoning_config()
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.reset_done")

        parsed = parse_reasoning_effort(value)
        if parsed is None:
            return t("gateway.reasoning.unknown_arg", arg=value)
        self._reasoning_config = parsed
        if persist_global:
            if self._save_gateway_config_key("agent.reasoning_effort", value):
                self._set_reasoning_override(session_key, None)
                return t("gateway.reasoning.set_global", effort=value)
            self._set_reasoning_override(session_key, parsed)
            return t("gateway.reasoning.set_global_save_failed", effort=value)
        self._set_reasoning_override(session_key, parsed)
        return t("gateway.reasoning.set_session", effort=value)

    async def _try_send_choice_picker(
        self, event: MessageEvent, session_key: str, title: str, choices: list, on_choice_selected,
    ) -> bool:
        """Send an interactive choice picker when the adapter *type* supports it (the /model gate);
        a failed send returns False (text fallback) instead of erroring."""
        adapter = self._delivery_adapter_for(event.source)
        if adapter is None or getattr(type(adapter), "send_choice_picker", None) is None:
            return False
        try:
            result = await adapter.send_choice_picker(
                chat_id=event.source.chat_id, title=title, choices=choices, session_key=session_key,
                on_choice_selected=on_choice_selected, metadata=self._reply_metadata(event),
            )
            return bool(getattr(result, "success", False))
        except Exception as e:
            logger.warning("send_choice_picker failed, falling back to text: %s", e)
            return False

    async def _handle_reasoning_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reasoning command — manage reasoning effort and display toggle."""
        from gateway.run import _platform_config_key
        from hermes_constants import VALID_REASONING_EFFORTS

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        # Normalize (Telegram DM topic recovery) so the override key matches the next turn's.
        # See #30479.
        _reasoning_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        # Effective model (session /model override wins) so per-model reasoning_overrides display.
        _session_model = str(
            ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}).get("model") or ""
        )
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source, session_key=session_key, model=_session_model,
        )
        platform_key = _platform_config_key(event.source.platform)
        if raw_args:  # typed path — same applier the picker uses
            return self._apply_reasoning_selection(session_key, platform_key, args, persist_global=persist_global)
        rc = self._reasoning_config
        # Labels tell the truth about the route: a Xinyuan-internal step (``ultra``) that the wire
        # clamps is shown as "ultra (sends max on this route)" instead of a distinct level (#61634).
        from agent.reasoning_effort import effort_display_label
        from gateway.run import _load_gateway_config
        from hermes_cli.codex_runtime_switch import get_current_runtime
        _session_route = ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {})
        _model_cfg = {}
        with contextlib.suppress(Exception):  # fail-open on config read errors, like /model does
            _model_cfg = _load_gateway_config(config_path=self.config_path).get("model", {}) or {}
        _route = (
            _session_route.get("provider") or _model_cfg.get("provider"),
            _session_model or _model_cfg.get("default") or _model_cfg.get("model"),
            get_current_runtime({"model": _model_cfg}),
        )
        if rc is None:
            level, current_effort = t("gateway.reasoning.level_default"), "medium"
        elif rc.get("enabled") is False:
            level, current_effort = t("gateway.reasoning.level_disabled"), "none"
        else:
            current_effort = rc.get("effort", "medium")
            level = effort_display_label(current_effort, *_route)
        display_state = t("gateway.reasoning.display_on") if self._show_reasoning else t("gateway.reasoning.display_off")
        has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
        scope = t("gateway.reasoning.scope_session") if has_session_override else t("gateway.reasoning.scope_global")

        async def _on_reasoning_choice(_chat_id: str, value: str) -> str:
            return self._apply_reasoning_selection(session_key, platform_key, value)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.reasoning.picker_title", level=level, scope=scope, display=display_state),
            choices=[
                {"value": "none", "label": t("gateway.reasoning.choice_none"), "is_current": current_effort == "none"},
                *({"value": lv, "label": t(f"gateway.reasoning.choice_{lv}"), "is_current": lv == current_effort}
                  for lv in VALID_REASONING_EFFORTS),
                *({"value": v, "label": t(f"gateway.reasoning.choice_{v}"), "is_current": False}
                  for v in ("reset", "show", "hide")),
            ],
            on_choice_selected=_on_reasoning_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.reasoning.status", level=level, scope=scope, display=display_state)

    def _apply_fast_selection(self, session_key: str, value: str, persist: bool = False) -> str:
        """Apply a /fast argument (typed or picked) and return the reply."""
        selection = _FAST_SELECTIONS.get(value)
        if selection is None:
            return t("gateway.fast.unknown_arg", arg=value)
        tier, saved_value, label_key = selection
        label = t(label_key) if label_key else value.upper()
        self._service_tier = tier
        if persist and self._save_gateway_config_key("agent.service_tier", saved_value):
            self._set_session_service_tier_override(session_key, None, clear=True)  # global wins
            self._evict_cached_agent(session_key)
            return t("gateway.fast.saved", label=label)
        # Session override — also the fallback after a failed config write (as /reasoning --global).
        self._set_session_service_tier_override(session_key, tier)
        self._evict_cached_agent(session_key)
        return t("gateway.fast.session_only", label=label)

    async def _handle_fast_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /fast — the CLI Priority Processing toggle; session-scoped unless ``--global``
        (persists agent.service_tier, parity with /model)."""
        from agent.fast_mode import FAST_ARG_ALIASES, service_tier_word
        from gateway.run import _load_gateway_config, _resolve_gateway_model

        # The /reasoning parser strips --global (any position) and normalizes unicode dashes.
        args, persist_global = self._parse_reasoning_command_args(event.get_command_args().strip().lower())
        # Normalized like /model and /reasoning (#30479): the tier override must land under the key
        # the next turn reads, and the eligibility check must see that turn's route.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        self._service_tier = self._resolve_session_service_tier(session_key=session_key)
        user_config = _load_gateway_config()
        try:
            # Off the loop: credential resolution can refresh an OAuth token, as the turn does.
            model, runtime = await asyncio.to_thread(
                self._resolve_session_agent_runtime, source=source, session_key=session_key, user_config=user_config)
        except Exception:
            # No usable route at all (the turn fails too): judge the configured model alone.
            model, runtime = _resolve_gateway_model(user_config), {}
        if not _fast_route_supports(model, runtime):
            return t("gateway.fast.not_supported")
        args = FAST_ARG_ALIASES.get(args, args)
        from agent.codex_service_tiers import ultrafast_offered
        ultrafast = ultrafast_offered(model, provider=runtime.get("provider"), base_url=runtime.get("base_url"),
                                      access_token=runtime.get("api_key"))
        if args == "ultrafast" and not ultrafast:
            return t("gateway.fast.ultrafast_not_supported", model=model)
        if args and args != "status":
            return self._apply_fast_selection(session_key, args, persist=persist_global)
        mode = service_tier_word(self._service_tier)
        status = {"fast": t("gateway.fast.status_fast"), "normal": t("gateway.fast.status_normal")}.get(mode, mode)

        async def _on_fast_choice(_chat_id: str, value: str) -> str:
            return self._apply_fast_selection(session_key, value, persist=persist_global)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.fast.picker_title", mode=status),
            choices=[
                {"value": v, "label": t(f"gateway.fast.choice_{v}"), "is_current": mode == v}
                for v in ("fast", "normal", "auto", "cold", *(("ultrafast",) if ultrafast else ()))
            ],
            on_choice_selected=_on_fast_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.fast.status", mode=status)
    async def _handle_addadmin_command(self, event: MessageEvent) -> str:
        import hermes_yaml as yaml
        a=(event.get_command_args() or "").strip()
        if not a:
            return "添加 Telegram 管理员：/addadmin <用户ID>"
        p=_hermes_home_path("config.yaml")
        c=yaml.safe_load(open(p)) or {}
        t=c.get("platforms",{}).get("telegram",{})
        lst=[str(x) for x in (t.get("allow_admin_from") or [])]
        if a in lst:
            return "该用户已是管理员：" + a
        lst.append(a)
        c["platforms"]["telegram"]["allow_admin_from"]=lst
        yaml.safe_dump(c,open(p,"w"),allow_unicode=True)
        return "已添加管理员：" + a

    async def _handle_removeadmin_command(self, event: MessageEvent) -> str:
        import hermes_yaml as yaml
        a=(event.get_command_args() or "").strip()
        if not a:
            return "删除 Telegram 管理员：/removeadmin <用户ID>"
        p=_hermes_home_path("config.yaml")
        c=yaml.safe_load(open(p)) or {}
        t=c.get("platforms",{}).get("telegram",{})
        lst=[str(x) for x in (t.get("allow_admin_from") or [])]
        if a not in lst:
            return "该用户不是管理员：" + a
        if len(lst)<=1:
            return "不能删除最后一个管理员"
        lst.remove(a)
        c["platforms"]["telegram"]["allow_admin_from"]=lst
        yaml.safe_dump(c,open(p,"w"),allow_unicode=True)
        return "已删除管理员：" + a

    async def _handle_listadmin_command(self, event: MessageEvent) -> str:
        import hermes_yaml as yaml
        p=_hermes_home_path("config.yaml")
        c=yaml.safe_load(open(p)) or {}
        lst=c.get("platforms",{}).get("telegram",{}).get("allow_admin_from") or []
        if not lst:
            return "暂无管理员"
        r="管理员列表："
        for x in lst:
            r+=chr(10)+"- "+str(x)
        return r

    async def _handle_ptest_command(self, event: MessageEvent) -> str:
        args = (event.get_command_args() or "").strip()
        if not args or "|" not in args:
            return t("gateway.ptest.usage")
        parts = [p.strip() for p in args.split("|", 1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return t("gateway.ptest.usage")
        base_url, api_key = parts[0], parts[1]
        try:
            from hermes_cli.web_routers.config_env import (
                validate_custom_endpoint, CustomEndpointUpdate,
            )
            body = CustomEndpointUpdate(
                name="ptest", base_url=base_url, api_key=api_key, model="",
            )
            result = await validate_custom_endpoint(body)
        except Exception as exc:
            return t("gateway.ptest.failed", error=_redact_secrets(str(exc)))
        _emsg = result.get("message", "")
        if not result.get("reachable"):
            return t("gateway.ptest.unreachable", message=_emsg)
        if not result.get("ok"):
            return t("gateway.ptest.invalid", message=_emsg)
        models = result.get("models") or []
        msg = t("gateway.ptest.passed")
        if models:
            msg += t("gateway.ptest.models_header", count=len(models))
            msg += "\n".join(f"• {m}" for m in sorted(models)[:20])
            if len(models) > 20:
                msg += t("gateway.ptest.more", count=len(models)-20)
        return msg

    async def _handle_penable_command(self, event: MessageEvent) -> str:
        """Enable a provider."""
        args = (event.get_command_args() or "").strip()
        if not args:
            return t("gateway.penable.usage")
        return await self._set_provider_enabled(args, True)

    async def _handle_pdisable_command(self, event: MessageEvent) -> str:
        """Disable a provider."""
        args = (event.get_command_args() or "").strip()
        if not args:
            return t("gateway.pdisable.usage")
        return await self._set_provider_enabled(args, False)

    async def _set_provider_enabled(self, name: str, enabled: bool) -> str:
        """Set provider enabled flag in config.yaml."""
        import hermes_yaml as yaml
        cfg_path = _hermes_home_path("config.yaml")
        try:
            cfg = yaml.safe_load(open(cfg_path)) or {}
        except Exception:
            return t("gateway.penable.read_failed")
        providers = cfg.get("providers") or {}
        # find by name or id
        target_key = None
        for k, v in providers.items():
            if k == name or (isinstance(v, dict) and v.get("name") == name):
                target_key = k
                break
        if target_key is None:
            return t("gateway.penable.not_found", name=name)
        providers[target_key]["enabled"] = enabled
        try:
            yaml.safe_dump(cfg, open(cfg_path, "w"), allow_unicode=True)
        except Exception:
            return t("gateway.penable.write_failed")
        key = "gateway.penable.enabled" if enabled else "gateway.penable.disabled"
        return t(key, name=name)

    async def _handle_pdelconfirm_command(self, event: MessageEvent) -> str:
        """Confirm provider deletion."""
        pending = self._pa_get_pending(event.source)
        if not pending or pending.get("step") != "await_delete_confirm":
            return t("gateway.addprovider.no_pending")
        target = pending.get("target")
        self._pa_clear_pending(event.source)
        return await self._pa_do_delete(event, target)

    async def _handle_phealth_command(self, event: MessageEvent) -> str:
        """Check health of all providers."""
        import hermes_yaml as yaml
        import urllib.request
        import json
        cfg_path = _hermes_home_path("config.yaml")
        try:
            cfg = yaml.safe_load(open(cfg_path)) or {}
        except Exception:
            return t("gateway.phealth.read_failed")
        providers = cfg.get("providers") or {}
        if not providers:
            return t("gateway.phealth.no_providers")
        lines = [t("gateway.phealth.header")]
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or pid
            if not entry.get("enabled", True):
                lines.append(f"⏸️ {name}：{t('gateway.phealth.disabled')}")
                continue
            base_url = (entry.get("base_url") or "").rstrip("/")
            api_key = entry.get("api_key") or ""
            if not base_url:
                lines.append(f"❌ {name}：{t('gateway.phealth.no_url')}")
                continue
            try:
                req = urllib.request.Request(
                    f"{base_url}/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode())
                        count = len(data.get("data", []))
                        lines.append(f"✅ {name}：{t('gateway.phealth.ok', count=count)}")
                    else:
                        lines.append(f"❌ {name}：HTTP {resp.status}")
            except Exception as e:
                lines.append(f"❌ {name}：{_redact_secrets(str(e))[:50]}")
        return "\n".join(lines)


    async def _handle_pgateway_command(self, event: MessageEvent) -> str:
        """处理 /pgateway —— 默认就是「重启网关」（发提示 → 重启 → 回来后通知），`status` 查状态。

        2026-10-18 改动（两道真 bug）：
        1. 原来无参数时发「网关管理」子菜单，而子菜单的「重启网关」按钮又转回 /pgateway
           —— 死循环，永远点不到真正的重启。
        2. 原来的重启实现写死了一个本机不存在的脚本
           （/opt/mickvvwai-extracted/.../gateway/start_safe.sh），Popen 静默失败，
           所以在你这台机器上 /pgateway restart 从来没有真的重启过。
        现在改为复用内置 /restart 的完整流程：写 .restart_notify.json（重启回来会通知发起人）
        + request_restart()（在 PM2 监管下也能正确拉起）。
        """
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            _raw_args = self._strip_button_label("/pgateway", event.get_command_args()).strip().split()
            sub = _raw_args[0].lower() if _raw_args else ""
            if sub not in ("status", "restart", "reload", "状态", "重启"):
                sub = ""  # 认不出的参数（多是底部按钮带来的说明）→ 当无参数
            if not sub:
                # 母菜单：弹「网关管理」子菜单（子菜单按钮走 /pgateway restart、/pgateway status）
                try:
                    if await self._send_inline_submenu(event, "gateway"):
                        return None
                except Exception:
                    pass
                sub = "status"
            if sub in ("status", "状态"):
                import subprocess
                r = subprocess.run(["pgrep", "-f", "gateway.run"],
                                   capture_output=True, text=True, timeout=5)
                pids = r.stdout.strip().split()
                if pids:
                    return f"✅ 网关运行中 (pid: {', '.join(pids)})"
                return "❌ 网关未运行"
            if sub in ("restart", "reload", "重启"):
                # 2026-10-18：内置 /restart 的「重启中」是 ephemeral，而且它在返回前就
                # request_restart —— 进程可能在回复送达之前就退出，实测用户**收不到重启前提示**。
                # 所以这里自己先发一条持久提示，等它落地（2 秒）再触发重启。
                try:
                    import asyncio as _asyncio

                    adapter = self._delivery_adapter_for(event.source)
                    if adapter is not None:
                        await adapter.send(
                            str(event.source.chat_id),
                            "🔄 网关正在重启，预计 10~20 秒。\n"
                            "重启期间我这边会短暂掉线，回来后我会再发一条提示。",
                            metadata=self._thread_metadata_for_source(event.source))
                        await _asyncio.sleep(2.0)  # 让这条先落地，再动手重启
                except Exception as exc:
                    import logging
                    logging.getLogger("gateway.run").debug("pgateway pre-restart notice failed: %s", exc)
                # 触发真正的重启（写通知标记 + request_restart，PM2 会兜底拉起）
                await self._handle_restart_command(event)
                return None  # 不再重复发内置那条英文 ephemeral 提示
            return "用法: /pgateway [restart|status]"
        except Exception as e:
            return f"❌ 执行失败: {str(e)[:100]}"


    # ------------------------------------------------------------------
    # WebDAV backup commands (/pbackup, /prestore)
    # ------------------------------------------------------------------

    def _backup_webdav_client(self):
        """Build a WebDAVClient from env/.env. Returns (client, error). Never raises."""
        try:
            import os
            from pathlib import Path

            def _read_env_file(path):
                out = {}
                try:
                    for line in Path(path).read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        out[k.strip()] = v.strip().strip('"').strip("'")
                except Exception:
                    pass
                return out

            home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            env_file = _read_env_file(home / ".env")
            url = (os.environ.get("WEBDAV_URL") or env_file.get("WEBDAV_URL") or "").strip()
            user = os.environ.get("WEBDAV_USER") or env_file.get("WEBDAV_USER") or ""
            passwd = os.environ.get("WEBDAV_PASS") or env_file.get("WEBDAV_PASS") or ""
            if not url:
                return None, "未配置 WebDAV：请在 ~/.hermes/.env 里设置 WEBDAV_URL、WEBDAV_USER、WEBDAV_PASS。"
            from hermes_cli.webdav_backup import WebDAVClient

            return WebDAVClient(url, user, passwd), ""
        except Exception as e:
            return None, f"WebDAV 配置无效：{str(e)[:80]}"

    async def _handle_pbackup_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /pbackup —— 母菜单：无参数弹「备份管理」子菜单；`/pbackup now` 真备份。

        2026-10-18（按用户要求）：
        * 恢复两层菜单：无参数 → 子菜单（一键备份 / 备份列表 / 恢复备份）。
          子菜单按钮分别走 `/pbackup now`、`/pbackup list`、`/prestore`，不会再回到本命令（无死循环）。
        * 备份就是 WebDAV：**没配置时提示用户去配置**（不再偷偷做本地备份）。
        """
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            _raw_args = self._strip_button_label("/pbackup", event.get_command_args()).strip().split()
            sub = _raw_args[0].lower() if _raw_args else ""
            if sub in ("list", "列表", "ls"):
                return await self._pa_backup_list(event)
            if sub not in ("now", "backup", "备份", "立刻", "立即", ""):
                sub = ""  # 认不出的参数（多是底部按钮带来的说明）→ 当无参数
            if not sub:
                # 母菜单：弹子菜单；失败则继续往下（直接走备份流程）
                try:
                    if await self._send_inline_submenu(event, "backup"):
                        return None
                except Exception:
                    pass
            client, _err = self._backup_webdav_client()
            if client is None:
                return ("⚠️ 还没配置 WebDAV —— 备份是存到你自己的 WebDAV 网盘，需要先设置。\n\n"
                        "在 `~/.hermes/.env` 里加上三行：\n"
                        "  `WEBDAV_URL=https://你的网盘/dav/目标目录`\n"
                        "  `WEBDAV_USER=用户名`\n"
                        "  `WEBDAV_PASS=密码`\n\n"
                        "填好后发 /pbackup 就会上传；/pbackup list 可看远程已有的备份。\n"
                        "（想临时在本机留一份完整存档，命令行执行 `hermes backup` 也行）")
            self._pa_set_pending(event.source, {"step": "await_password", "next": "backup_now"})
            return "一键备份需要密码验证。\n请输入：/ppass <密码>"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pbackup failed: %s", e)
            return "备份失败，请重试。"

    async def _pa_backup_list(self, event) -> str:
        """List remote backups. Read-only, no password needed."""
        try:
            client, err = self._backup_webdav_client()
            if client is None:
                # 按用户要求：备份就是 WebDAV，没配就提示去配置（不做本地兜底）
                return ("⚠️ 还没配置 WebDAV，看不到备份列表。\n\n"
                        "在 `~/.hermes/.env` 里加上：\n"
                        "  `WEBDAV_URL=https://你的网盘/dav/目标目录`\n"
                        "  `WEBDAV_USER=用户名`\n"
                        "  `WEBDAV_PASS=密码`\n\n"
                        "填好后发 /pbackup list 再试。")
            import asyncio
            items = await asyncio.to_thread(client.list_backups)
            if not items:
                return "远程没有备份文件。"
            lines = ["远程备份列表："]
            for it in items[:20]:
                name = it.get("filename", "")
                size = it.get("size", 0)
                kb = f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
                lines.append(f"  • {name} ({kb})")
            lines.append("\n恢复：/prestore <文件名>")
            return "\n".join(lines)
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("backup list failed: %s", e)
            return "列出备份失败，请检查 WebDAV 配置与网络。"

    async def _pa_backup_now(self, event, pending, password) -> str:
        """Do the backup after password verified. Never raises."""
        try:
            import asyncio
            import io
            import os
            import secrets
            import tempfile
            import zipfile
            from datetime import datetime, timezone
            from pathlib import Path

            client, err = self._backup_webdav_client()
            if client is None:
                return err
            home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            # Build zip in memory
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel in ("config.yaml", "SOUL.md"):
                    p = home / rel
                    if p.exists() and p.is_file():
                        zf.write(p, rel)
                souls = home / "souls"
                if souls.exists() and souls.is_dir():
                    for fp in sorted(souls.glob("*.md")):
                        if fp.is_file():
                            zf.write(fp, f"souls/{fp.name}")
            data = buf.getvalue()
            if not data:
                return "没有可备份的内容。"
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"hermes-cloud-backup-{ts}-{secrets.token_hex(4)}.hbackup"
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".hbackup", delete=False) as tf:
                    tf.write(data)
                    tmp = tf.name
                size = await asyncio.to_thread(client.put_file, filename, tmp)
                kb = f"{size / 1024:.1f}KB"
                return f"✅ 备份成功：\n{filename}\n大小：{kb}"
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("backup_now failed: %s", e)
            return "备份上传失败，请检查 WebDAV 配置与网络。"

    async def _handle_prestore_command(self, event: MessageEvent) -> str:
        """Handle /prestore <filename> — restore from WebDAV. Password + confirm."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            args = (event.get_command_args() or "").strip()
            if not args:
                return "用法：/prestore <文件名>\n先用 /pbackup list 查看可恢复的备份。"
            filename = args.split()[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if not filename.endswith(".hbackup") or not filename:
                return "备份文件名无效。"
            self._pa_set_pending(event.source, {"step": "await_password", "next": "backup_restore",
                                                "filename": filename})
            return f"恢复备份需要密码验证。\n目标：{filename}\n请输入：/ppass <密码>"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("prestore failed: %s", e)
            return "恢复失败，请重试。"

    async def _pa_backup_restore_verified(self, event, pending, password) -> str:
        """After password verified: ask for explicit confirmation."""
        try:
            filename = pending.get("filename", "")
            self._pa_set_pending(event.source, {"step": "await_restore_confirm",
                                                "filename": filename})
            return (f"⚠️ 确认恢复备份？\n{filename}\n"
                    "这将覆盖本地的 config.yaml、SOUL.md 和 souls/。\n"
                    "确认请发送：/prestoreconfirm")
        except Exception:
            return "操作失败，请重试。"

    async def _handle_prestoreconfirm_command(self, event: MessageEvent) -> str:
        """Confirm backup restore."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            pending = self._pa_get_pending(event.source)
            if not pending or pending.get("step") != "await_restore_confirm":
                return "没有待确认的恢复操作。"
            filename = pending.get("filename", "")
            self._pa_clear_pending(event.source)
            return await self._pa_do_restore(event, filename)
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("prestoreconfirm failed: %s", e)
            return "恢复失败，请重试。"

    async def _pa_do_restore(self, event, filename: str) -> str:
        """Download and unpack the backup. Never raises."""
        try:
            import asyncio
            import io
            import os
            import tempfile
            import zipfile
            from pathlib import Path

            client, err = self._backup_webdav_client()
            if client is None:
                return err
            home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            fd, tmp = tempfile.mkstemp(suffix=".hbackup")
            os.close(fd)
            try:
                await asyncio.to_thread(client.get_file, filename, tmp)
                with open(tmp, "rb") as f:
                    data = f.read()
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        name = info.filename
                        if name.startswith("/") or ".." in name.split("/"):
                            return "备份内容非法，已拒绝恢复。"
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        target = home / info.filename
                        try:
                            target.resolve().relative_to(home.resolve())
                        except ValueError:
                            return "备份内容非法，已拒绝恢复。"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(target, "wb") as dst:
                            while chunk := src.read(1024 * 1024):
                                dst.write(chunk)
                return f"✅ 已从备份恢复：\n{filename}\n网关重启后生效。"
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("do_restore failed: %s", e)
            return "下载/解压备份失败，请检查文件名与网络。"


    async def _handle_psessions_command(self, event: MessageEvent) -> str:
        """Handle /psessions - list recent chat sessions across platforms."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            import contextlib
            import json as _json
            from datetime import datetime
            sessions = []
            try:
                from hermes_state_registry import acquire, release_or_close
                db = acquire()
                try:
                    lister = getattr(db, "list_gateway_sessions", None)
                    rows = lister(active_only=False) or [] if callable(lister) else []
                finally:
                    release_or_close(db)
                for row in rows:
                    try:
                        origin = None
                        with contextlib.suppress(TypeError, ValueError):
                            origin = _json.loads(row["origin_json"]) if row.get("origin_json") else None
                        platform = row.get("platform") or "?"
                        chat_name = (origin or {}).get("chat_name") or row.get("display_name") or row.get("chat_id") or "?"
                        chat_type = row.get("chat_type") or "dm"
                        last = row.get("last_active") or row.get("updated_at") or ""
                        uid = str(row.get("user_id") or "")
                        uid_short = ("..." + uid[-4:]) if len(uid) > 4 else uid
                        sessions.append((str(last), platform, chat_name, chat_type, uid_short))
                    except Exception:
                        continue
            except Exception as e:
                return "X 读取会话失败: " + _redact_secrets(str(e))[:80]
            if not sessions:
                return "暂无聊天会话记录"
            sessions.sort(reverse=True)
            lines = ["📋 最近聊天会话 (10):", ""]
            for last, platform, name, ctype, uid in sessions[:10]:
                try:
                    ts = datetime.fromtimestamp(float(last)).strftime("%m-%d %H:%M") if last else "?"
                except Exception:
                    ts = "?"
                lines.append("• [" + platform + "] " + name + " (" + ctype + uid + ") - " + ts)
            return "\n".join(lines)
        except Exception as e:
            return "X 执行失败: " + _redact_secrets(str(e))[:100]


    async def _handle_psend_command(self, event: MessageEvent) -> str:
        """Handle /psend - admin sends message to user: /psend <chat_id> <msg>."""
        try:
            denied = self._pa_guard(event)
            if denied:
                return denied
            raw = (event.get_command_args() or "").strip()
            if not raw or " " not in raw:
                return "用法: /psend <序号|chat_id> <消息内容>（序号看 /pusers）"
            target_tok, _, message = raw.partition(" ")
            target_tok = target_tok.strip()
            message = message.strip()
            if not target_tok or not message:
                return "用法: /psend <序号|chat_id> <消息内容>（序号看 /pusers）"
            # 2026-10-19：支持用 /pusers 里的序号或名字片段，不必手打 chat_id
            chat_id = target_tok
            try:
                row = self._pa_resolve_user(target_tok)
                if row:
                    chat_id = row["chat_id"]
            except Exception:
                pass
            import urllib.request
            import json as _json
            from gateway.telegram_menu import _get_bot_token  # 2026-10-18：原来写成 from telegram_menu（顶层模块名不存在，一调用就 ImportError）
            token = _get_bot_token()
            if not token:
                return "X 未配置 Bot Token"
            payload = _json.dumps({"chat_id": chat_id, "text": "[管理员消息]\n" + message}).encode()
            req = urllib.request.Request(
                "https://api.telegram.org/bot" + token + "/sendMessage",
                data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
                if data.get("ok"):
                    return "✅ 已发送到 " + chat_id
                return "X 发送失败: " + _redact_secrets(str(data.get("description", ""))[:80])
        except Exception as e:
            return "X 执行失败: " + _redact_secrets(str(e))[:100]


    # ---------------- 在线人员管理（2026-10-19 重做：真持久化 + 真拦截） ----------------

    ONLINE_WINDOW = 300  # 5 分钟内活跃算"在线"

    def _pa_user_rows(self) -> list:
        """在线人员：会话表 + users.json + 禁言 + 专属提示词 合并，按活跃时间倒序。"""
        import contextlib
        import json as _json
        import time as _t
        from gateway import user_admin as ua

        rows, seen = [], set()
        try:
            from hermes_state_registry import acquire, release_or_close
            db = acquire()
            try:
                lister = getattr(db, "list_gateway_sessions", None)
                sessions = (lister(active_only=False) or []) if callable(lister) else []
            finally:
                release_or_close(db)
        except Exception:
            sessions = []
        for row in sessions:
            try:
                cid = str(row.get("chat_id") or row.get("user_id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                origin = None
                with contextlib.suppress(Exception):
                    origin = _json.loads(row.get("origin_json") or "{}")
                rows.append({
                    "chat_id": cid,
                    "name": (origin or {}).get("chat_name") or row.get("display_name") or "",
                    "platform": row.get("platform") or "",
                    "chat_type": row.get("chat_type") or "",
                    "last_active": float(row.get("last_active") or row.get("updated_at") or 0),
                })
            except Exception:
                continue
        for cid, meta in ua.get_users().items():
            if cid in seen:
                continue
            rows.append({"chat_id": cid, "name": meta.get("name") or "",
                         "platform": meta.get("platform") or "", "chat_type": "",
                         "last_active": float(meta.get("last_seen") or 0)})
        rows.sort(key=lambda r: r["last_active"], reverse=True)
        now = _t.time()
        blocks, souls = ua.list_blocks(), ua.soul_map()
        for r in rows:
            r["online"] = bool(r["last_active"] and (now - r["last_active"]) < self.ONLINE_WINDOW)
            r["block"] = blocks.get(r["chat_id"])
            r["soul"] = souls.get(r["chat_id"])
        return rows

    def _pa_resolve_user(self, token: str):
        """把「序号 / chat_id / 名字片段」解析成一个用户行；解析不到返回 None。"""
        t = (token or "").strip()
        if not t:
            return None
        rows = self._pa_user_rows()
        if t.isdigit() and len(t) <= 3:      # 序号优先（/pusers 里的编号）
            i = int(t)
            if 1 <= i <= len(rows):
                return rows[i - 1]
        for r in rows:                        # chat_id 精确匹配
            if r["chat_id"] == t:
                return r
        low = t.lower()
        for r in rows:                        # 名字片段
            if low and low in (r.get("name") or "").lower():
                return r
        return None

    async def _handle_pusers_command(self, event: MessageEvent) -> str:
        """/pusers —— 在线人员一览（在线状态 / 禁言 / 专属提示词）。

        2026-10-19：权限从 `_pa_dm_only_guard` 收紧为 `_pa_guard`（管理员专属）。
        原因：这是家长控制面板（谁在线、谁被禁言），普通家庭成员不该看到全名单。
        配了 `allow_admin_from` 时非管理员会被拒；没配时 `_pa_guard` 默认放行。
        """
        try:
            denied = self._pa_guard(event)
            if denied:
                return denied
            import time as _t
            from gateway import user_admin as ua

            rows = self._pa_user_rows()
            if not rows:
                return "还没有记录到任何用户（等家人各发一条消息就会出现）。"
            online = [r for r in rows if r["online"]]
            lines = [f"👥 在线人员：{len(online)} 在线 / 共 {len(rows)} 人", ""]
            for i, r in enumerate(rows, 1):
                name = (r.get("name") or "").strip() or r["chat_id"]
                dot = "🟢" if r["online"] else "⚪"
                if r["last_active"]:
                    mins = int((_t.time() - r["last_active"]) / 60)
                    ago = "刚刚" if mins < 1 else (f"{mins} 分钟前" if mins < 60 else f"{mins // 60} 小时前")
                else:
                    ago = "无记录"
                bits = [f"{i}. {dot} {name}", f"[{r.get('platform') or '?'}] {ago}"]
                if r.get("soul"):
                    bits.append(f"🧬{r['soul']}")
                blk = r.get("block")
                if blk:
                    bits.append(f"⛔禁言中(剩 {ua.human_duration(blk['remaining'])})")
                lines.append(" · ".join(bits))
            lines.append("")
            lines.append("管理：/pblock <序号> <时长>　/psend <序号> <消息>　/psoulgive <序号> <提示词名>")
            return "\n".join(lines)
        except Exception as e:
            return "X 读取在线人员失败: " + _redact_secrets(str(e))[:100]

    async def _handle_pblock_command(self, event: MessageEvent) -> str:
        """/pblock —— 强制下线（限时）：/pblock <序号|chat_id|名字> <时长|forever|off>。

        2026-10-19：从子菜单「下线成员」按钮进来（无参数）时，给一个**成员选择器** ——
        点一下即下线 60 分钟（最常用），想改时长/原因就发命令。
        """
        denied = self._pa_guard(event)
        if denied:
            return denied
        from gateway import user_admin as ua

        raw = self._strip_button_label("/pblock", event.get_command_args()).strip()
        if not raw:
            rows, choices = self._pa_member_choices()
            blocked = ua.list_blocks()
            if not rows:
                return self._pa_member_list_text(rows, "还没有可下线的成员。")

            async def _on_choice(_chat_id: str, value: str) -> str:
                return await self._pblock_apply(event, value, "60", "")

            head = "下线成员 —— 点一下即生效（默认 60 分钟）"
            if blocked:
                head += f"\n（当前有 {len(blocked)} 人正被下线/拉黑，可用「恢复成员」放行）"
            try:
                if await self._try_send_choice_picker(
                        event, self._session_key_for_source(event.source), title=head,
                        choices=choices, on_choice_selected=_on_choice):
                    return None
            except Exception:
                pass
            return self._pa_member_list_text(
                rows, "要下线谁？发：/pblock <序号> <时长>（30=30分钟 / 2h / 1d / forever=永久）")
        parts = raw.split()
        arg = parts[1].strip() if len(parts) > 1 else ""
        reason = " ".join(parts[2:]).strip()
        return await self._pblock_apply(event, parts[0], arg, reason)

    async def _pblock_apply(self, event, token: str, arg: str, reason: str) -> str:
        """下线/解禁的实际执行（命令路径与选择器回调共用同一套逻辑）。"""
        from gateway import user_admin as ua

        row = self._pa_resolve_user(token)
        if row is None:
            return f"找不到成员「{token}」——先发 /pusers 看序号。"
        cid = row["chat_id"]
        name = (row.get("name") or "").strip() or cid
        if str(cid) in self._pa_admin_ids():
            return f"{name} 是管理员，不会被下线（管理员永不被禁）。"
        low = (arg or "").strip().lower()
        if not low:
            st = ua.block_status(cid)
            if not st:
                return f"{name} 当前没有被下线。用法：/pblock {token} <时长>（30 / 2h / 1d / forever）"
            left = "永久" if st.get("permanent") else ua.human_duration(st["remaining"])
            return f"{name} 正在被下线中（剩 {left}）。放行：/punblock {token}"
        if low in {"off", "解禁", "解除", "unban", "恢复"}:
            existed = ua.clear_block(cid)
            return f"✅ 已恢复 {name}，可以正常聊了。" if existed else f"{name} 本来就没被下线。"
        if low in {"forever", "permanent", "永久", "拉黑", "无限"}:
            ua.set_block(cid, None, reason=reason, by=str(getattr(event.source, "user_id", "")))
            tail = f"（原因：{reason}）" if reason else ""
            return (f"🚫 已拉黑 {name}{tail} —— 永久生效，必须你手动恢复。\n"
                    f"放行：/punblock {token}（或点「恢复成员」）")
        seconds = ua.parse_duration(arg)
        if not seconds:
            return "时长看不懂：用 30（分钟）/ 2h / 1d / forever（永久），或 off 恢复。"
        until = ua.set_block(cid, seconds, reason=reason,
                             by=str(getattr(event.source, "user_id", "")))
        import time as _t
        tail = f"（原因：{reason}）" if reason else ""
        return (f"🚫 已让 {name} 下线 {ua.human_duration(seconds)}{tail}"
                f"（到 {_t.strftime('%m-%d %H:%M', _t.localtime(until))} 自动恢复）。\n"
                f"这期间他发消息只会收到提示，不进入 AI 对话。提前恢复：/punblock {token}")

    async def _handle_pmembers_command(self, event: MessageEvent) -> Optional[str]:
        """/pmembers —— 成员管理母菜单（底部常驻第 8 个按钮）：弹出四个子功能。"""
        denied = self._pa_guard(event)
        if denied:
            return denied
        try:
            if await self._send_inline_submenu(event, "members"):
                return None
        except Exception:
            pass
        return ("成员管理：\n"
                "  /pusers —— 最近在线成员\n"
                "  /pblack —— 拉黑成员（永久，需手动恢复）\n"
                "  /pblock —— 下线成员（限时）\n"
                "  /punblock —— 恢复成员")

    async def _handle_pblack_command(self, event: MessageEvent) -> Optional[str]:
        """/pblack —— 拉黑成员（永久下线）：/pblack <序号|chat_id|名字> [原因]。"""
        denied = self._pa_guard(event)
        if denied:
            return denied
        raw = self._strip_button_label("/pblack", event.get_command_args()).strip()
        if not raw:
            rows, choices = self._pa_member_choices()
            if not rows:
                return self._pa_member_list_text(rows, "还没有可拉黑的成员。")

            async def _on_choice(_chat_id: str, value: str) -> str:
                return await self._pblack_apply(event, value, "")

            try:
                if await self._try_send_choice_picker(
                        event, self._session_key_for_source(event.source),
                        title="拉黑成员（永久，直到你用「恢复成员」解除）—— 点一下即生效",
                        choices=choices, on_choice_selected=_on_choice):
                    return None
            except Exception:
                pass
            return self._pa_member_list_text(rows, "要拉黑谁？发：/pblack <序号>  [原因]")
        parts = raw.split()
        return await self._pblack_apply(event, parts[0], " ".join(parts[1:]).strip())

    async def _pblack_apply(self, event, token: str, reason: str) -> str:
        from gateway import user_admin as ua

        row = self._pa_resolve_user(token)
        if row is None:
            return f"找不到成员「{token}」—— 先发 /pusers 看序号。"
        cid = row["chat_id"]
        name = (row.get("name") or "").strip() or cid
        if str(cid) in self._pa_admin_ids():
            return f"{name} 是管理员，拉黑不了（管理员永不被禁）。"
        ua.set_block(cid, None, reason=reason, by=str(getattr(event.source, "user_id", "")))
        tail = f"（原因：{reason}）" if reason else ""
        return (f"🚫 已拉黑 {name}{tail} —— 永久生效：他发消息只会看到「已被强制下线」的提示，不进 AI 对话。\n"
                f"放行：/punblock {token}（或点「恢复成员」）")

    async def _handle_punblock_command(self, event: MessageEvent) -> Optional[str]:
        """/punblock —— 恢复成员：/punblock <序号|chat_id|名字|all>。"""
        denied = self._pa_guard(event)
        if denied:
            return denied
        from gateway import user_admin as ua

        raw = self._strip_button_label("/punblock", event.get_command_args()).strip()
        if not raw:
            blocked = ua.blocked_members()
            if not blocked:
                return "当前没有被下线/拉黑的成员，大家都正常。"
            choices = []
            for b in blocked:
                st = b["status"]
                left = "永久" if st.get("permanent") else ua.human_duration(st.get("remaining"))
                choices.append({"value": b["chat_id"], "label": f"{b['name']}（{left}）", "is_current": False})

            async def _on_choice(_chat_id: str, value: str) -> str:
                return self._punblock_apply(value)

            try:
                if await self._try_send_choice_picker(
                        event, self._session_key_for_source(event.source),
                        title="恢复成员 —— 点一下就解除", choices=choices,
                        on_choice_selected=_on_choice):
                    return None
            except Exception:
                pass
            lines = ["正在被下线/拉黑的成员："]
            for i, b in enumerate(blocked, 1):
                st = b["status"]
                left = "永久" if st.get("permanent") else ua.human_duration(st.get("remaining"))
                lines.append(f"{i}. {b['name']}（剩 {left}）")
            lines.append("")
            lines.append("发 /punblock <名字或 chat_id> 恢复；全部恢复：/punblock all")
            return "\n".join(lines)
        if raw.lower() in {"all", "全部", "所有人", "都恢复"}:
            n = 0
            for cid in list(ua.list_blocks().keys()):
                if ua.clear_block(cid):
                    n += 1
            return f"✅ 已恢复全部 {n} 个成员。"
        return self._punblock_apply(raw)

    def _punblock_apply(self, token: str) -> str:
        from gateway import user_admin as ua

        cid = token
        row = self._pa_resolve_user(token)
        if row:
            cid = row["chat_id"]
        existed = ua.clear_block(cid)
        name = (row or {}).get("name") or cid
        return f"✅ 已恢复 {name}，可以正常聊了。" if existed else f"{name} 本来就没被下线。"

    def _pa_admin_ids(self) -> set:
        """管理员 id 集合（这些人不进「可下线」名单，避免把自己或家人管理员关在外面）。"""
        try:
            import os as _os
            from pathlib import Path as _P

            import hermes_yaml as yaml

            home = _os.environ.get("HERMES_HOME") or str(_P.home() / ".hermes")
            cfg = yaml.safe_load(open(str(_P(home) / "config.yaml"))) or {}
            tg = ((cfg.get("platforms") or {}).get("telegram") or {})
            return {str(x) for x in (tg.get("allow_admin_from") or [])}
        except Exception:
            return set()

    def _pa_member_choices(self) -> tuple:
        """(成员行, 选择器选项)。

        value 必须用 **chat_id** 而不是序号：选择器这里过滤掉了管理员，
        而 ``_pa_resolve_user`` 是按「含管理员的全表序号」解析的 —— 用序号会错位
        （实测：点"儿子"被解析成管理员，动作被拒）。
        """
        from gateway import user_admin as ua

        admins = self._pa_admin_ids()
        blocked = set(ua.list_blocks().keys())
        rows = [r for r in self._pa_user_rows() if str(r.get("chat_id")) not in admins]
        choices = []
        for r in rows:
            name = (r.get("name") or r.get("chat_id") or "?")[:24]
            dot = "🟢" if r.get("online") else "⚪"
            mark = " ⛔" if str(r.get("chat_id")) in blocked else ""
            choices.append({"value": str(r.get("chat_id")), "label": f"{dot} {name}{mark}",
                            "is_current": False})
        return rows, choices

    def _pa_member_list_text(self, rows: list, howto: str) -> str:
        if not rows:
            return "还没有记录到任何成员（家人各发一条消息就会出现）。"
        lines = ["成员（按活跃倒序）："]
        for i, r in enumerate(rows, 1):
            dot = "🟢" if r.get("online") else "⚪"
            lines.append(f"{i}. {dot} {r.get('name') or r.get('chat_id')}")
        lines.append("")
        lines.append(howto)
        return "\n".join(lines)

    async def _handle_pguard_command(self, event: MessageEvent) -> str:
        """/pguard —— 补丁自愈守护：一键开关 + 状态 + 立刻检查。

        为什么有它：用户要"守护默认开着，退出再打开也一键开"。
        守护本体由 systemd 托管（`mickvvw-patch-guard.service`，enable + Restart=always），默认就是开的；
        本命令用于看状态，或万一被关掉后一键恢复。

        - 不带参数 → 状态；on / off → 开关；now → 立刻检查并修复；freeze → 更新"已知良好"清单
        """
        denied = self._pa_guard(event)
        if denied:
            return denied
        import json as _json
        import subprocess as _sp

        raw = self._strip_button_label("/pguard", event.get_command_args()).strip().lower()
        svc = "mickvvw-patch-guard"
        guard = "/home/ubuntu/.hermes/scripts/patch-guard.py"
        manifest = "/home/ubuntu/.hermes/patch-guard-manifest.json"
        logfile = "/home/ubuntu/.hermes/logs/patch-guard.log"

        def run(cmd):
            try:
                r = _sp.run(cmd, capture_output=True, text=True, timeout=60)
                return r.returncode, (r.stdout or r.stderr or "").strip()
            except Exception as exc:  # noqa: BLE001
                return 1, f"{type(exc).__name__}: {exc}"

        _rc, unit_info = run(["systemctl", "cat", svc, "--no-pager"])
        unit_missing = ("No files found" in unit_info) or ("could not be found" in unit_info)

        if raw in ("on", "enable", "开", "开启", "启用"):
            if unit_missing:
                return "这台机器还没装守护服务 —— 跑最新辅助包的 install-extras.sh 就会装上。"
            rc, out = run(["sudo", "-n", "systemctl", "enable", "--now", svc])
            if rc != 0:
                return f"开启失败：{out[:120]}"
            return "✅ 补丁自愈守护已开启（开机也会自动开）。"
        if raw in ("off", "disable", "关", "关闭", "停"):
            rc, out = run(["sudo", "-n", "systemctl", "disable", "--now", svc])
            if rc != 0:
                return f"关闭失败：{out[:120]}"
            return "⏸ 已关闭守护（补丁被改动就不会自动修了）。恢复：/pguard on"
        if raw in ("now", "check", "检查", "立即"):
            rc, out = run(["/usr/bin/python3", guard, "--heal"])
            last = out.splitlines()[-1] if out else f"退出码 {rc}"
            return f"检查结果：{last}"
        if raw in ("freeze", "冻结", "认定"):
            rc, out = run(["/usr/bin/python3", guard, "--freeze"])
            last = out.splitlines()[-1] if out else f"退出码 {rc}"
            return f"清单已更新：{last}"

        active = run(["systemctl", "is-active", svc])[1] or "unknown"
        enabled = run(["systemctl", "is-enabled", svc])[1] or "unknown"
        lines = ["🛡 补丁自愈守护", f"  运行: {active}｜开机自启: {enabled}"]
        if unit_missing:
            lines[1] = "  运行: 未安装（用最新辅助包的 install-extras.sh 装上）"
        try:
            from pathlib import Path as _P
            m = _json.loads(_P(manifest).read_text(encoding="utf-8"))
            lines.append(f"  清单: {len(m.get('files') or {})} 个补丁文件（冻结于 {m.get('frozen_at', '?')}）")
            lines.append(f"  副本: {str(m.get('overlay', '')).split('/')[-2]}｜基座 {str(m.get('base_commit', ''))[:8]}")
        except Exception:
            lines.append("  ⚠️ 清单未冻结（守护会拒绝工作）→ 发 /pguard freeze")
        try:
            from pathlib import Path as _P
            tail = _P(logfile).read_text(encoding="utf-8").splitlines()
            if tail:
                lines.append(f"  上次检查: {tail[-1][:78]}")
        except Exception:
            pass
        lines.append("")
        lines.append("一键：/pguard on｜off｜now（立刻检查）｜freeze（更新清单）")
        return "\n".join(lines)

    async def _handle_psoulgive_command(self, event: MessageEvent) -> str:
        """/psoulgive —— 给某人绑定专属提示词：/psoulgive <序号|chat_id> <提示词名|off>。"""
        denied = self._pa_guard(event)
        if denied:
            return denied
        from gateway import user_admin as ua

        raw = (event.get_command_args() or "").strip()
        souls = ua.available_souls()
        if not raw:
            cur = ua.soul_map()
            lines = ["可用提示词：" + (", ".join(souls) if souls else "（souls/ 下还没有，先用 /psoulnew 建）"), ""]
            if cur:
                for cid, s in cur.items():
                    row = self._pa_resolve_user(cid)
                    who = (row or {}).get("name") if row else cid
                    lines.append(f"• {who or cid} → {s}")
            else:
                lines.append("还没有人绑定专属提示词。")
            lines.append("用法：/psoulgive <序号|chat_id> <提示词名>；取消：/psoulgive <序号> off")
            return "\n".join(lines)
        parts = raw.split()
        row = self._pa_resolve_user(parts[0])
        if row is None:
            return f"找不到用户「{parts[0]}」——先发 /pusers 看序号。"
        if len(parts) < 2:
            return "用法：/psoulgive <序号|chat_id> <提示词名>；提示词名用 /psoullist list 看。"
        name = (row.get("name") or "").strip() or row["chat_id"]
        pick = parts[1].strip()
        if pick.lower() in {"off", "取消", "none", "0"}:
            ua.set_user_soul(row["chat_id"], None)
            return f"✅ 已取消 {name} 的专属提示词，改用全局提示词。"
        if pick not in souls:
            return f"没有提示词「{pick}」。可用的：" + (", ".join(souls) if souls else "（还没有）")
        ua.set_user_soul(row["chat_id"], pick)
        return (f"✅ {name} 的对话现在用专属提示词「{pick}」（下一条消息生效）。\n"
                "其他家人各自的提示词互不影响。")

    async def _handle_pfailover_command(self, event: MessageEvent) -> str:
        """Toggle auto-failover."""
        import hermes_yaml as yaml
        cfg_path = _hermes_home_path("config.yaml")
        try:
            cfg = yaml.safe_load(open(cfg_path)) or {}
        except Exception:
            return t("gateway.pfailover.read_failed")
        args = event.text.split()[1:] if hasattr(event, "text") else []
        gw = cfg.get("gateway") or {}
        if not args:
            cur = gw.get("auto_failover", False)
            return t("gateway.pfailover.status", status=t("gateway.pfailover.on") if cur else t("gateway.pfailover.off"))
        val = args[0].lower()
        if val in ("on", "\u5f00", "\u5f00\u542f", "true", "1"):
            new_val = True
        elif val in ("off", "\u5173", "\u5173\u95ed", "false", "0"):
            new_val = False
        else:
            return t("gateway.pfailover.usage")
        gw["auto_failover"] = new_val
        cfg["gateway"] = gw
        try:
            yaml.safe_dump(cfg, open(cfg_path, "w"))
        except Exception:
            return t("gateway.pfailover.save_failed")
        return t("gateway.pfailover.done", status=t("gateway.pfailover.on") if new_val else t("gateway.pfailover.off"))

    async def _handle_pedit_command(self, event: MessageEvent) -> str:
        """Edit provider field (key/url/model/name)."""
        import hermes_yaml as yaml
        cfg_path = _hermes_home_path("config.yaml")
        try:
            cfg = yaml.safe_load(open(cfg_path)) or {}
        except Exception:
            return t("gateway.pedit.read_failed")
        parts = event.text.split(None, 3) if hasattr(event, "text") else []
        if len(parts) < 4:
            return t("gateway.pedit.usage")
        _, pname, field, value = parts
        providers = cfg.get("providers") or {}
        target_pid = None
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            if pid == pname or (entry.get("name") or "") == pname:
                target_pid = pid
                break
        if not target_pid:
            return t("gateway.pedit.not_found", name=pname)
        entry = providers[target_pid]
        field = field.lower()
        if field in ("key", "apikey", "api_key", "\u5bc6\u94a5"):
            key_env = entry.get("key_env")
            if key_env:
                env_path = _hermes_home_path(".env")
                try:
                    lines = open(env_path).read().split("\n")
                except Exception:
                    lines = []
                found = False
                for i, l in enumerate(lines):
                    if l.startswith(key_env + "="):
                        lines[i] = key_env + "=" + value
                        found = True
                        break
                if not found:
                    lines.append(key_env + "=" + value)
                open(env_path, "w").write("\n".join(lines))
            else:
                entry["api_key"] = value
            saved = t("gateway.pedit.saved_key")
        elif field in ("url", "base_url", "baseurl", "\u5730\u5740"):
            entry["base_url"] = value.rstrip("/")
            saved = t("gateway.pedit.saved_url")
        elif field in ("model", "\u6a21\u578b"):
            entry["model"] = value
            saved = t("gateway.pedit.saved_model")
        elif field in ("name", "\u540d\u79f0"):
            entry["name"] = value
            saved = t("gateway.pedit.saved_name")
        else:
            return t("gateway.pedit.usage")
        try:
            yaml.safe_dump(cfg, open(cfg_path, "w"))
        except Exception:
            return t("gateway.pedit.save_failed")
        return t("gateway.pedit.done", name=entry.get("name") or target_pid, field=saved)
    # ----------------------------------------------------------------- soul
    # Telegram prompt (soul) management commands.
    # Backend logic lives in hermes_cli/soul_access.py; these handlers wire
    # it to Telegram slash commands with password gates.

    def _soul_hermes_home(self):
        """Resolve HERMES_HOME directory. Never raises."""
        try:
            import os
            from pathlib import Path
            return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        except Exception:
            from pathlib import Path
            return Path.home() / ".hermes"

    def _soul_dir(self):
        """Path to the souls/ directory. Never raises."""
        try:
            d = self._soul_hermes_home() / "souls"
            return d
        except Exception:
            from pathlib import Path
            return Path.home() / ".hermes" / "souls"

    def _soul_password_prompt(self, event, next_step, data=None):
        """Set pending password state and prompt for /ppass. Never raises."""
        try:
            self._pa_clear_pending(event.source)
            state = {"step": "await_password", "next": next_step}
            if data:
                state.update(data)
            self._pa_set_pending(event.source, state)
            return "请输入密码：/ppass <密码>"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("soul password prompt failed: %s", e)
            return "操作失败，请重试。"

    async def _handle_pmenu_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /pmenu —— 常驻菜单的「收纳 / 展开」开关（reply keyboard）。

        2026-10-19：**不带参数 = 翻转**（点一下收起，再点一下展开），
        这样把 /pmenu 放在 Telegram 左侧 ≡ 菜单最上面，就是"点它收纳、再点显示"的一键开关。
        也保留显式写法：/pmenu on（展开）/ /pmenu off（收起）。

        为什么用 reply keyboard 而不是 inline 按钮：inline 要走 callback_query → 再注入命令，
        链路长、容易受配置影响；reply keyboard 的按钮点一下就等于用户手打了那条命令。
        """
        try:
            args = (event.get_command_args() or "").strip().lower()
            args = self._strip_button_label("/pmenu", args).strip().lower()
            chat_id = getattr(getattr(event, "source", None), "chat_id", None)
            if not chat_id:
                return "拿不到 chat_id，无法设置菜单。"
            from gateway.telegram_menu import menu_shown, send_reply_keyboard

            if args in ("off", "hide", "0", "关闭", "收起"):
                show = False
            elif args in ("on", "show", "1", "显示", "展开"):
                show = True
            else:
                show = not menu_shown(chat_id)      # 不带参数 → 翻转（一键收纳/展开）
            if not await send_reply_keyboard(chat_id, show=show):
                return "菜单设置失败（该平台可能不支持常驻按钮）。"
            return None
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pmenu failed: %s", e)
            return "设置菜单失败。"

    async def _handle_psoul_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /psoul —— 母菜单：无参数弹「提示词管理」子菜单；`/psoul view` 直接看内容。

        2026-10-18 恢复两层菜单结构（用户明确要求），同时修掉死循环：
        子菜单按钮不再指向本命令，而是带子命令（view / psoulset / psouladd / psouldel），
        所以「查看提示词」→ `/psoul view` 会真的显示内容，而不是又弹一次菜单。
        """
        try:
            args = self._strip_button_label("/psoul", event.get_command_args()).strip()
            if not args:
                # 母菜单：弹子菜单；发送失败则退化为直接显示内容
                try:
                    if await self._send_inline_submenu(event, "soul"):
                        return None
                except Exception:
                    pass
            elif args.lower() not in ("view", "show", "看", "查看", "正文"):
                # 认不出的参数：同样当无参数，弹菜单（避免被当成提示词名字之类）
                try:
                    if await self._send_inline_submenu(event, "soul"):
                        return None
                except Exception:
                    pass
            from hermes_cli import soul_access as sa
            result = sa.read_soul()
            if not result.get("ok"):
                return "读取提示词失败。"
            if not result.get("exists"):
                return ("当前还没有设置提示词。\n\n————\n管理命令：\n"
                        "  /psoullist 列出全部\n"
                        "  /psoulnew <名称> 新建\n"
                        "  /psoulswitch <名称> 切换\n"
                        "  /psoulset <内容> 替换当前\n"
                        "  /psouladd <内容> 追加\n"
                        "  /psouldel <名称> 删除")
            content = result.get("content", "")
            # Truncate if too long for Telegram
            if len(content) > 3500:
                content = content[:3500] + "\n\n...（内容过长已截断）"
            # 2026-10-18：以前这里只丢一段正文，按钮叫「提示词管理」却看不到能做什么。
            # 现在把管理命令一起列出来。
            hints = ("\n\n————\n管理命令：\n"
                     "  /psoullist 列出全部\n"
                     "  /psoulnew <名称> 新建\n"
                     "  /psoulswitch <名称> 切换\n"
                     "  /psoulset <内容> 替换当前\n"
                     "  /psouladd <内容> 追加\n"
                     "  /psouldel <名称> 删除")
            return f"当前提示词：\n\n{content}{hints}"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psoul failed: %s", e)
            return "读取提示词失败。"

    async def _handle_psoulnew_command(self, event: MessageEvent) -> str:
        """Handle /psoulnew <name> — create new soul. Requires password."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            args = self._strip_button_label("/psoulnew", event.get_command_args()).strip()
            if args:
                first = args.split()[0].lower()
                if first in {"ask", "new", "add", "新建", "添加"}:
                    # 子菜单「新建提示词」按钮来的：给可照做的指引（不再弹回同一子菜单）
                    return ("新建提示词：把名称跟在命令后面发过来 👇\n"
                            "  `/psoulnew <名称>`  例：`/psoulnew 翻译助手`\n"
                            "发完会再问你要内容和密码。")
            if not args:
                # 底部「添加提示词」按钮来的：弹对应子菜单（新建提示词 / 查看现有）
                try:
                    if await self._send_inline_submenu(event, "soul_add"):
                        return None
                except Exception:
                    pass
                return "用法：/psoulnew <名称>"
            name = args.split()[0]
            # Validate name (no path traversal)
            import re
            if not re.match(r'^[\w\-\u4e00-\u9fff]+$', name):
                return "名称只能包含字母、数字、下划线、横线和中文。"
            soul_dir = self._soul_dir()
            target = soul_dir / f"{name}.md"
            if target.exists():
                return f"提示词「{name}」已存在。"
            return self._soul_password_prompt(event, "soul_new", {"name": name})
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psoulnew failed: %s", e)
            return "创建提示词失败。"

    async def _handle_psoullist_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /psoullist —— 母菜单：无参数弹「切换提示词」子菜单；`/psoullist list` 直接列。"""
        try:
            args = self._strip_button_label("/psoullist", event.get_command_args()).strip().lower()
            if args not in ("list", "列表", "ls", "全部"):
                # 母菜单/按钮：弹子菜单（子菜单的「提示词列表」走 /psoullist list）
                try:
                    if await self._send_inline_submenu(event, "soul_list"):
                        return None
                except Exception:
                    pass
            # 文本列表
            soul_dir = self._soul_dir()
            if not soul_dir.exists():
                return "还没有保存的提示词。"
            names = sorted([p.stem for p in soul_dir.glob("*.md")])
            if not names:
                return "还没有保存的提示词。"
            # Check which is active by comparing with SOUL.md
            try:
                from hermes_cli import soul_access as sa
                current = sa.read_soul().get("content", "")
            except Exception:
                current = ""
            lines = ["已保存的提示词："]
            for n in names:
                try:
                    text = (soul_dir / f"{n}.md").read_text(encoding="utf-8")
                    mark = " ← 当前" if text == current and current else ""
                except Exception:
                    mark = ""
                lines.append(f"  • {n}{mark}")
            lines.append("\n切换：/psoulswitch <名称>")
            return "\n".join(lines)
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psoullist failed: %s", e)
            return "列出提示词失败。"

    async def _handle_psoulswitch_command(self, event: MessageEvent) -> str:
        """Handle /psoulswitch <name> — switch soul. Requires password."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            args = (event.get_command_args() or "").strip()
            if not args:
                return "用法：/psoulswitch <名称>"
            name = args.split()[0]
            soul_dir = self._soul_dir()
            target = soul_dir / f"{name}.md"
            if not target.exists():
                return f"提示词「{name}」不存在。先用 /psoullist 查看。"
            return self._soul_password_prompt(event, "soul_switch", {"name": name})
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psoulswitch failed: %s", e)
            return "切换提示词失败。"

    async def _handle_psouldel_command(self, event: MessageEvent) -> str:
        """Handle /psouldel <name> — delete soul. Requires password + confirm."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            args = (event.get_command_args() or "").strip()
            if not args:
                return "删除提示词：`/psouldel <名称>`（先发 `/psoullist list` 看有哪些名字）"
            name = args.split()[0]
            soul_dir = self._soul_dir()
            target = soul_dir / f"{name}.md"
            if not target.exists():
                return f"提示词「{name}」不存在。"
            return self._soul_password_prompt(event, "soul_del", {"name": name})
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psouldel failed: %s", e)
            return "删除提示词失败。"

    async def _handle_psouldelconfirm_command(self, event: MessageEvent) -> str:
        """Confirm soul deletion."""
        try:
            pending = self._pa_get_pending(event.source)
            if not pending or pending.get("step") != "await_soul_del_confirm":
                return "没有待确认的删除操作。"
            name = pending.get("name")
            self._pa_clear_pending(event.source)
            if not name:
                return "删除失败：名称丢失。"
            soul_dir = self._soul_dir()
            target = soul_dir / f"{name}.md"
            try:
                target.unlink()
                return f"已删除提示词「{name}」。"
            except FileNotFoundError:
                return f"提示词「{name}」不存在。"
            except Exception:
                return "删除失败。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psouldelconfirm failed: %s", e)
            return "删除失败。"

    async def _handle_psoulset_command(self, event: MessageEvent) -> str:
        """Handle /psoulset <content> — replace current soul. Requires password."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            content = (event.get_command_args() or "").strip()
            if not content:
                return "编辑提示词：把新内容跟在命令后面 👉 `/psoulset <新的提示词内容>`"
            return self._soul_password_prompt(event, "soul_set", {"content": content})
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psoulset failed: %s", e)
            return "修改提示词失败。"

    async def _handle_psouladd_command(self, event: MessageEvent) -> str:
        """Handle /psouladd <content> — append to soul. Requires password."""
        try:
            denied = self._pa_dm_only_guard(event)
            if denied:
                return denied
            content = (event.get_command_args() or "").strip()
            if not content:
                return "追加内容：`/psouladd <要追加的内容>`"
            return self._soul_password_prompt(event, "soul_add", {"content": content})
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("psouladd failed: %s", e)
            return "追加提示词失败。"

    # --- soul password-continuation handlers (called from _handle_ppass_command) ---

    async def _pa_soul_new(self, event, pending, password) -> str:
        """Create souls/{name}.md after password verified."""
        try:
            from hermes_cli import provider_access as pa
            ok, reason = pa.verify_password(password)
            if not ok:
                if reason.startswith("locked:"):
                    return f"密码错误次数过多，已锁定 {reason.split(':', 1)[1]} 秒。"
                return "密码错误。"
            name = pending.get("name")
            if not name:
                return "创建失败：名称丢失。"
            soul_dir = self._soul_dir()
            soul_dir.mkdir(parents=True, exist_ok=True)
            target = soul_dir / f"{name}.md"
            if target.exists():
                return f"提示词「{name}」已存在。"
            target.write_text("", encoding="utf-8")
            return f"已创建提示词「{name}」，当前为空。用 /psoulset 修改内容。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pa_soul_new failed: %s", e)
            return "创建失败。"

    async def _pa_soul_switch(self, event, pending, password) -> str:
        """Copy souls/{name}.md to SOUL.md after password verified."""
        try:
            from hermes_cli import provider_access as pa
            ok, reason = pa.verify_password(password)
            if not ok:
                if reason.startswith("locked:"):
                    return f"密码错误次数过多，已锁定 {reason.split(':', 1)[1]} 秒。"
                return "密码错误。"
            name = pending.get("name")
            if not name:
                return "切换失败：名称丢失。"
            soul_dir = self._soul_dir()
            target = soul_dir / f"{name}.md"
            if not target.exists():
                return f"提示词「{name}」不存在。"
            from hermes_cli import soul_access as sa
            content = target.read_text(encoding="utf-8")
            result = sa.set_soul(content, password)
            if not result.get("ok"):
                return "切换失败。"
            return f"已切换到提示词「{name}」。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pa_soul_switch failed: %s", e)
            return "切换失败。"

    async def _pa_soul_del(self, event, pending, password) -> str:
        """Ask for delete confirmation after password verified."""
        try:
            from hermes_cli import provider_access as pa
            ok, reason = pa.verify_password(password)
            if not ok:
                if reason.startswith("locked:"):
                    return f"密码错误次数过多，已锁定 {reason.split(':', 1)[1]} 秒。"
                return "密码错误。"
            name = pending.get("name")
            if not name:
                return "删除失败：名称丢失。"
            self._pa_set_pending(event.source, {"step": "await_soul_del_confirm", "name": name})
            return f"确认删除提示词「{name}」？\n发送 /psouldelconfirm 确认，任意其他消息取消。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pa_soul_del failed: %s", e)
            return "删除失败。"

    async def _pa_soul_set(self, event, pending, password) -> str:
        """Replace SOUL.md after password verified."""
        try:
            from hermes_cli import soul_access as sa
            content = pending.get("content", "")
            result = sa.set_soul(content, password)
            if not result.get("ok"):
                err = result.get("error", "")
                if "password" in str(err).lower() or "gate" in str(err).lower():
                    return "密码错误。"
                return "修改失败。"
            return "提示词已更新。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pa_soul_set failed: %s", e)
            return "修改失败。"

    async def _pa_soul_add(self, event, pending, password) -> str:
        """Append to SOUL.md after password verified."""
        try:
            from hermes_cli import soul_access as sa
            content = pending.get("content", "")
            result = sa.append_soul(content, password)
            if not result.get("ok"):
                err = result.get("error", "")
                if "password" in str(err).lower() or "gate" in str(err).lower():
                    return "密码错误。"
                return "追加失败。"
            return "已追加到提示词。"
        except Exception as e:
            import logging
            logging.getLogger("gateway.run").debug("pa_soul_add failed: %s", e)
            return "追加失败。"
