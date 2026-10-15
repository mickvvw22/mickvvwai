"""Shared password gate for adding model providers via bot menu / AI chat.

Both the Telegram slash-command flow (``gateway/slash_commands_model.py``) and the
agent plugin tool (``plugins/provider-access``) enforce the SAME gate through this
module, so neither path can bypass the password check.

Settings live in the ``provider_access`` section of config.yaml::

    provider_access:
      enabled: true          # master switch for bot/chat provider onboarding
      allow_delete: false    # delete stays OFF until the owner enables it
      password_hash: <sha256>  # never a plaintext password
      sources:               # where each provider was added from (badge data)
        deepseek: chat

The password defaults to ``mickvvw`` (stored as SHA-256, never plaintext).
Five wrong attempts lock verification for 10 minutes; the counter survives
restarts via ``$HERMES_HOME/provider_access_lock.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SECTION = "provider_access"

DEFAULT_PASSWORD = "mickvvw"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 10 * 60
PENDING_TTL_SECONDS = 10 * 60

SOURCE_BOT = "bot"
SOURCE_CHAT = "chat"
SOURCE_DESKTOP = "desktop"
KNOWN_SOURCES = frozenset({SOURCE_BOT, SOURCE_CHAT, SOURCE_DESKTOP})

_lock = threading.Lock()


def _default_password_hash() -> str:
    return hashlib.sha256(DEFAULT_PASSWORD.encode("utf-8")).hexdigest()


def _lock_file() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "provider_access_lock.json"


def _read_lock_state() -> dict[str, Any]:
    try:
        raw = _lock_file().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_lock_state(state: dict[str, Any]) -> None:
    try:
        path = _lock_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("provider_access: could not persist lock state", exc_info=True)


def get_settings() -> dict[str, Any]:
    """Return the ``provider_access`` settings with safe defaults applied."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    section = cfg.get(SECTION)
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", True)),
        "allow_delete": bool(section.get("allow_delete", False)),
        "password_hash": str(section.get("password_hash") or _default_password_hash()),
        "sources": dict(section.get("sources") or {}),
    }


def save_settings(*, enabled: Optional[bool] = None, allow_delete: Optional[bool] = None,
                  password_hash: Optional[str] = None) -> dict[str, Any]:
    """Persist ``provider_access`` settings; returns the effective settings."""
    from hermes_cli.config import load_config, save_config

    with _lock:
        cfg = load_config() or {}
        section = cfg.get(SECTION)
        if not isinstance(section, dict):
            section = {}
        if enabled is not None:
            section["enabled"] = bool(enabled)
        if allow_delete is not None:
            section["allow_delete"] = bool(allow_delete)
        if password_hash:
            section["password_hash"] = str(password_hash)
        cfg[SECTION] = section
        save_config(cfg)
    return get_settings()


def set_password(old_password: str, new_password: str) -> None:
    """Change the access password (SHA-256, never plaintext).

    The old password must verify first; raises ValueError when it does not
    match (or when the new password is empty). A wrong old password counts
    toward the 5-attempt lockout, same as verify_password().
    """
    ok, reason = verify_password(old_password or "")
    if not ok:
        if reason.startswith("locked:"):
            raise ValueError("password locked, try again later")
        raise ValueError("old password is incorrect")
    if not (new_password or "").strip():
        raise ValueError("password must not be empty")
    digest = hashlib.sha256(new_password.strip().encode("utf-8")).hexdigest()
    save_settings(password_hash=digest)
    with _lock:
        _write_lock_state({"failures": 0, "locked_until": 0})


def is_enabled() -> bool:
    return bool(get_settings().get("enabled", True))


def is_delete_allowed() -> bool:
    return bool(get_settings().get("allow_delete", False))


def _is_locked(now: float) -> tuple[bool, float]:
    state = _read_lock_state()
    locked_until = float(state.get("locked_until") or 0)
    if locked_until > now:
        return True, locked_until - now
    return False, 0.0


def verify_password(password: str) -> tuple[bool, str]:
    """Check *password* against the stored hash.

    Returns ``(ok, reason)`` where *reason* is one of ``"ok"``,
    ``"locked"`` (with remaining seconds appended as ``locked:<secs>``) or
    ``"wrong"``. Five wrong attempts lock verification for 10 minutes.
    The password value itself is never logged or echoed.
    """
    now = time.time()
    with _lock:
        locked, remaining = _is_locked(now)
        if locked:
            return False, "locked:%d" % int(remaining)
        expected = str(get_settings().get("password_hash") or _default_password_hash())
        given = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
        # hmac.compare_digest resists timing probes; equality is what matters.
        import hmac

        ok = hmac.compare_digest(given, expected)
        state = _read_lock_state()
        if ok:
            _write_lock_state({"failures": 0, "locked_until": 0})
            return True, "ok"
        failures = int(state.get("failures") or 0) + 1
        if failures >= MAX_FAILURES:
            _write_lock_state({"failures": 0, "locked_until": now + LOCKOUT_SECONDS})
            logger.warning("provider_access: password locked after %d failures", failures)
            return False, "locked:%d" % LOCKOUT_SECONDS
        _write_lock_state({"failures": failures, "locked_until": 0})
        return False, "wrong"


def check_gate() -> tuple[bool, str]:
    """Master-switch check shared by every onboarding path.

    Returns ``(allowed, reason)``; *reason* is ``"ok"`` or ``"disabled"``.
    """
    if not is_enabled():
        return False, "disabled"
    return True, "ok"


def record_source(provider_id: str, source: str) -> None:
    """Badge a provider with where it was added from (``bot`` / ``chat``)."""
    provider_id = (provider_id or "").strip()
    if not provider_id or source not in KNOWN_SOURCES:
        return
    from hermes_cli.config import load_config, save_config

    with _lock:
        cfg = load_config() or {}
        section = cfg.get(SECTION)
        if not isinstance(section, dict):
            section = {}
        sources = section.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        sources[provider_id] = source
        section["sources"] = sources
        cfg[SECTION] = section
        save_config(cfg)


def get_source(provider_id: str) -> Optional[str]:
    """Return the recorded source badge for *provider_id*, if any."""
    sources = get_settings().get("sources") or {}
    value = sources.get((provider_id or "").strip())
    return value if value in KNOWN_SOURCES else None


def clear_source(provider_id: str) -> None:
    """Drop the source badge when a provider is deleted."""
    provider_id = (provider_id or "").strip()
    if not provider_id:
        return
    from hermes_cli.config import load_config, save_config

    with _lock:
        cfg = load_config() or {}
        section = cfg.get(SECTION)
        if not isinstance(section, dict):
            return
        sources = section.get("sources")
        if isinstance(sources, dict) and provider_id in sources:
            del sources[provider_id]
            section["sources"] = sources
            cfg[SECTION] = section
            save_config(cfg)


# ── Core provider operations (shared by the gateway commands and the agent tool) ──

def _key_env_var_for_profile(profile) -> Optional[str]:
    env_vars = list(getattr(profile, "env_vars", None) or ())
    for var in env_vars:
        name = str(var or "").strip().upper()
        if name.endswith("_KEY") or name.endswith("_TOKEN"):
            return str(var).strip()
    return str(env_vars[0]).strip() if env_vars else None


def probe_builtin_key(env_var: str, api_key: str) -> str:
    """Probe a built-in provider key. Returns ``'ok'`` | ``'bad'`` | ``'unknown'``.

    ``'unknown'`` means no probe exists for this provider (or the probe could
    not run) — the caller may save with a warning instead of blocking.
    """
    try:
        from hermes_cli.web_routers import config_env as ce
    except Exception:
        return "unknown"
    probes = getattr(ce, "_CREDENTIAL_PROBES", None) or {}
    probe = probes.get(env_var)
    if not probe:
        return "unknown"
    url, auth = probe
    try:
        import httpx

        headers = {"Accept": "application/json"}
        params = {}
        if auth == "bearer":
            headers["Authorization"] = "Bearer %s" % api_key
        else:
            params["key"] = api_key
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception:
        return "unknown"
    if resp.status_code in (401, 403):
        return "bad"
    return "ok"


def add_builtin_key(provider_name: str, api_key: str, source: str) -> dict[str, Any]:
    """Persist a built-in provider's API key. Returns a result dict."""
    from providers import get_provider_profile

    provider_name = (provider_name or "").strip()
    profile = get_provider_profile(provider_name)
    if profile is None:
        return {"ok": False, "error": "unknown_provider", "provider": provider_name}
    env_var = _key_env_var_for_profile(profile)
    if not env_var:
        return {"ok": False, "error": "no_key_env", "provider": provider_name}
    if not (api_key or "").strip():
        return {"ok": False, "error": "empty_key", "provider": provider_name}
    probe = probe_builtin_key(env_var, api_key.strip())
    if probe == "bad":
        return {"ok": False, "error": "key_rejected", "provider": profile.name}
    from hermes_cli.credential_lifecycle import save_provider_env_credential

    save_provider_env_credential(env_var, api_key.strip())
    record_source(profile.name, source)
    return {"ok": True, "provider": profile.name,
            "display_name": profile.display_name or profile.name,
            "verified": probe == "ok"}


def add_custom_endpoint(name: str, base_url: str, api_key: str, model: str,
                        source: str) -> dict[str, Any]:
    """Persist a custom OpenAI-compatible endpoint. Returns a result dict."""
    from hermes_cli.config import load_config, save_config
    from hermes_cli.web_models import CustomEndpointUpdate
    from hermes_cli.web_routers.config_env import _write_custom_endpoint

    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    model = (model or "").strip() or name  # model is required downstream; default to the endpoint name
    if not name or not base_url or not (api_key or "").strip():
        return {"ok": False, "error": "missing_fields"}
    body = CustomEndpointUpdate(name=name, base_url=base_url,
                                model=model,
                                api_key=api_key.strip())
    cfg = load_config() or {}
    endpoint_id, entry = _write_custom_endpoint(cfg, body)
    # _write_custom_endpoint already stored entry in cfg["providers"] and wrote
    # the key to .env; tag the same dict object in place.
    entry["added_via"] = source
    save_config(cfg)
    record_source(endpoint_id, source)
    return {"ok": True, "endpoint_id": endpoint_id, "name": name}


def delete_provider_entry(target: str) -> dict[str, Any]:
    """Delete a custom endpoint or a built-in provider key. Returns a result dict."""
    from hermes_cli.config import coerce_provider_id, custom_endpoint_key_env, load_config, \
        remove_env_value, save_config
    from hermes_cli.web_routers.config_env import _resolve_custom_endpoint_entry

    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "missing_target"}
    cfg = load_config() or {}
    providers = cfg.get("providers")
    stored_key, entry = _resolve_custom_endpoint_entry(
        providers if isinstance(providers, dict) else {}, target)
    if entry is not None and isinstance(providers, dict):
        provider_key = coerce_provider_id(stored_key)
        providers.pop(stored_key, None)
        cfg["providers"] = providers
        remove_env_value(custom_endpoint_key_env(provider_key))
        save_config(cfg)
        clear_source(provider_key)
        clear_source(target)
        return {"ok": True, "target": target, "kind": "custom"}
    from providers import get_provider_profile

    profile = get_provider_profile(target)
    if profile is None:
        return {"ok": False, "error": "unknown_provider", "target": target}
    env_var = _key_env_var_for_profile(profile)
    if not env_var:
        return {"ok": False, "error": "no_key_env", "target": target}
    from hermes_cli.credential_lifecycle import remove_provider_env_credential

    result = remove_provider_env_credential(env_var) or {}
    if not result.get("found"):
        return {"ok": False, "error": "not_configured", "target": target}
    clear_source(profile.name)
    return {"ok": True, "target": target, "kind": "builtin"}


def list_providers_with_sources() -> list[dict[str, Any]]:
    """List built-in + custom providers with their source badges."""
    from hermes_cli.config import load_config
    from providers import list_providers, unlisted_provider_names

    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    hidden = set()
    try:
        hidden = unlisted_provider_names()
    except Exception:
        pass
    rows: list[dict[str, Any]] = []
    for profile in sorted(list_providers(), key=lambda p: (p.display_name or p.name).lower()):
        if profile.name in hidden:
            continue
        rows.append({"id": profile.name, "name": profile.display_name or profile.name,
                     "kind": "builtin", "source": get_source(profile.name)})
    custom = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    for endpoint_id, entry in sorted(custom.items()):
        if not isinstance(entry, dict):
            continue
        source = entry.get("added_via") or get_source(endpoint_id)
        rows.append({"id": endpoint_id, "name": entry.get("name") or endpoint_id,
                     "kind": "custom",
                     "source": source if source in KNOWN_SOURCES else None})
    return rows
