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
import logging
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


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime, /reasoning, /fast, /personality)."""

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
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        request = parse_model_switch_args(event.get_command_args().strip())  # single-owner parser
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
        if not policy.enabled or not policy.is_admin(getattr(source, "user_id", None)):
            return t("gateway.addprovider.admin_only")
        from hermes_cli import provider_access as pa

        allowed, _reason = pa.check_gate()
        if not allowed:
            return t("gateway.addprovider.disabled")
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
        args = (event.get_command_args() or "").strip().split()
        if args and args[0].lower() == "list":
            return await self._pa_list_providers(event)
        if args and args[0].lower() in {"del", "delete", "remove"}:
            return await self._pa_start_delete(event, " ".join(args[1:]).strip())
        if args:
            return await self._pa_begin_for_provider(event, " ".join(args))

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
        """Handle /ppass — verify the access password for a pending operation."""
        denied = self._pa_guard(event)
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
            return await self._pa_do_delete(event, pending.get("target"))
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
        """Handle /padd — one-shot custom provider: /padd <name> | <base_url> | <key> | <model>."""
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
        model = parts[3] if len(parts) > 3 else ""
        self._pa_clear_pending(event.source)
        self._pa_set_pending(event.source, {"step": "await_password", "next": "await_custom_key",
                                            "name": name, "base_url": base_url, "model": model,
                                            "api_key": api_key})
        return t("gateway.addprovider.password_prompt", provider=name)

    async def _pa_save_custom(self, event: MessageEvent, pending: dict) -> str:
        """Validate + persist a custom endpoint, then badge it."""
        from hermes_cli import provider_access as pa
        from hermes_cli.web_models import CustomEndpointUpdate
        from hermes_cli.web_routers.config_env import validate_custom_endpoint

        body = CustomEndpointUpdate(name=pending.get("name") or "", base_url=pending.get("base_url") or "",
                                    model=pending.get("model") or "", api_key=pending.get("api_key") or "")
        try:
            result = await validate_custom_endpoint(body)
        except Exception as exc:
            logger.warning("addprovider: custom validation error: %s", exc)
            result = {"ok": False, "reachable": False, "message": str(exc)}
        if not result.get("ok") and result.get("reachable"):
            return t("gateway.addprovider.endpoint_rejected", message=result.get("message") or "")
        try:
            saved = await asyncio.to_thread(
                pa.add_custom_endpoint, pending.get("name"), pending.get("base_url"),
                pending.get("api_key"), pending.get("model"), pa.SOURCE_BOT)
        except Exception as exc:
            logger.warning("addprovider: failed to save custom endpoint: %s", exc)
            return t("gateway.addprovider.save_failed")
        if not saved.get("ok"):
            return t("gateway.addprovider.save_failed_detail", error=saved.get("error") or "")
        if not result.get("ok"):
            return t("gateway.addprovider.saved_unverified", provider=pending.get("name"))
        # AutoFetch
        _mt=""
        try:
            import urllib.request,json
            _b=(pending.get("base_url") or "").rstrip("/")
            _k=pending.get("api_key") or ""
            if _b and _k:
                _q=urllib.request.Request(_b+"/v1/models",headers={"Authorization":"Bearer "+_k})
                with urllib.request.urlopen(_q,timeout=15) as _r:
                    _d=json.load(_r).get("data",[])
                _ids=[m.get("id") for m in _d if m.get("id")]
                if _ids:
                    _mt=chr(10)+chr(10)+"可用模型："
                    for _m in sorted(_ids)[:20]:
                        _mt+=chr(10)+"- "+_m
        except Exception:
            pass
        return t("gateway.addprovider.saved",provider=pending.get("name"))+_mt

    async def _pa_list_providers(self, event: MessageEvent) -> str:
        """List providers with their source badges."""
        from hermes_cli import provider_access as pa

        badges = {pa.SOURCE_BOT: t("gateway.addprovider.badge_bot"),
                  pa.SOURCE_CHAT: t("gateway.addprovider.badge_chat"),
                  pa.SOURCE_DESKTOP: t("gateway.addprovider.badge_desktop")}
        lines = [t("gateway.addprovider.list_header")]
        for row in pa.list_providers_with_sources():
            badge = f" {badges[row['source']]}" if row.get("source") in badges else ""
            lines.append(f"• {row['name']}{badge}")
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
                *({"value": lv, "label": effort_display_label(lv, *_route), "is_current": lv == current_effort}
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
        import yaml
        a=(event.get_command_args() or "").strip()
        if not a:
            return "用法：/addadmin <用户ID>"
        p="/root/.hermes/config.yaml"
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
        import yaml
        a=(event.get_command_args() or "").strip()
        if not a:
            return "用法：/removeadmin <用户ID>"
        p="/root/.hermes/config.yaml"
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
        import yaml
        p="/root/.hermes/config.yaml"
        c=yaml.safe_load(open(p)) or {}
        lst=c.get("platforms",{}).get("telegram",{}).get("allow_admin_from") or []
        if not lst:
            return "暂无管理员"
        r="管理员列表："
        for x in lst:
            r+=chr(10)+"- "+str(x)
        return r
