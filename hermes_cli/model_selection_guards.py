"""Unified selection-time guard registry for model switching surfaces.

Guard modules (``model_cost_guard``, ``model_data_policy_guard``) keep their public APIs — existing
tests and mock patch points remain valid; this module only aggregates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from agent.models_dev import ModelInfo


@dataclass(frozen=True)
class SelectionWarning:
    """A selection-time warning a surface must confirm before applying."""

    kind: str  # "cost" | "data_policy" | "context_cache" | future guard kinds
    title: str
    model: str
    provider: str
    message: str


@dataclass(frozen=True)
class SelectionContext:
    """Live-session facts a surface threads into the registry. Model-only guards (cost, data-policy)
    ignore it; guards about the *switch itself* (context-cache) need the size of the conversation at
    stake and the model it is currently on. Surfaces without a live agent omit it and those guards
    stay silent."""

    context_tokens: Optional[int] = None
    current_model: Optional[str] = None


def selection_context_for_agent(agent: object) -> Optional[SelectionContext]:
    """:class:`SelectionContext` from a live ``AIAgent``.

    Uses the compressor's measured ``last_prompt_tokens`` (what the provider billed
    on the latest turn). ``session_prompt_tokens`` is a lifetime sum and is not live
    occupancy, so it is not a fallback (#126343). ``None`` when no positive
    measurement exists — the guard stays silent rather than guess."""
    if agent is None:
        return None
    try:
        cc = getattr(agent, "context_compressor", None)
        tokens = int(getattr(cc, "last_prompt_tokens", 0) or 0) if cc else 0
    except Exception:
        tokens = 0
    if tokens <= 0:
        return None
    return SelectionContext(context_tokens=tokens, current_model=getattr(agent, "model", "") or None)


def _wrap(kind: str, title: str, warning, model_name: str, provider: Optional[str]):
    """Lift a raw guard payload into a :class:`SelectionWarning` (None passes through). Duck-typed:
    payloads may carry only ``.message``."""
    if warning is None:
        return None
    return SelectionWarning(
        kind=kind, title=title, model=getattr(warning, "model", model_name),
        provider=getattr(warning, "provider", provider or ""), message=warning.message)


def _cost_guard(
    model_name: str, provider: Optional[str], base_url: Optional[str], api_key: Optional[str],
    model_info: Optional[ModelInfo], ctx: Optional[SelectionContext] = None) -> Optional[SelectionWarning]:
    from hermes_cli.model_cost_guard import expensive_model_warning

    warning = expensive_model_warning(
        model_name, provider=provider, base_url=base_url, api_key=api_key, model_info=model_info)
    return _wrap("cost", "Expensive Model Warning", warning, model_name, provider)


def _data_policy_guard(
    model_name: str, provider: Optional[str], base_url: Optional[str], api_key: Optional[str],
    model_info: Optional[ModelInfo], ctx: Optional[SelectionContext] = None) -> Optional[SelectionWarning]:
    from hermes_cli.model_data_policy_guard import data_training_warning

    warning = data_training_warning(model_name, provider=provider, base_url=base_url)
    return _wrap("data_policy", "Data-Training Tier Warning", warning, model_name, provider)


# Context-token threshold above which a mid-session switch asks for confirmation: providers key
# prompt caches per model, so the first call after a switch re-reads the whole context uncached.
DEFAULT_CONTEXT_CACHE_SWITCH_THRESHOLD = 100_000


def _context_cache_threshold() -> int:
    """``model.switch_context_confirm_tokens`` from config.yaml (0 disables), else the default."""
    try:
        from hermes_cli.config import load_config

        model_cfg = (load_config() or {}).get("model", {})
        raw = model_cfg.get("switch_context_confirm_tokens") if isinstance(model_cfg, dict) else None
        if raw is not None:
            return max(0, int(raw))
    except Exception:
        pass
    return DEFAULT_CONTEXT_CACHE_SWITCH_THRESHOLD


def _context_cache_guard(
    model_name: str, provider: Optional[str], base_url: Optional[str], api_key: Optional[str],
    model_info: Optional[ModelInfo], ctx: Optional[SelectionContext] = None) -> Optional[SelectionWarning]:
    """Confirm a mid-session switch that abandons a large cached context. Fires only when the surface
    supplied live facts showing the active context at/above the threshold; smaller sessions, sessions
    with no measured size and same-model re-selects (cache stays warm) are silent."""
    if ctx is None or not ctx.context_tokens:
        return None
    target = (model_name or "").strip()
    current = (ctx.current_model or "").strip()
    if not target or (current and target == current):
        return None
    threshold = _context_cache_threshold()
    tokens = int(ctx.context_tokens)
    if threshold <= 0 or tokens < threshold:
        return None
    message = "\n".join([
        "!!! LARGE CONTEXT MODEL SWITCH !!!",
        "",
        f"This session holds ~{tokens:,} tokens of context.",
        f"Switching to {target} makes the next reply re-read all of it uncached (providers key "
        "prompt caches per model) — a one-time full-price input cost.",
        "",
        f"Threshold: model.switch_context_confirm_tokens (currently {threshold:,}; 0 disables this check).",
        "Confirm only if you intend to switch now."])
    return SelectionWarning(
        kind="context_cache", title="Large Context Switch Warning", model=target,
        provider=(provider or "").strip(), message=message)


# Registry, evaluated in order. Add new guard classes here — never at the
# individual surfaces.
_GUARDS = (_cost_guard, _data_policy_guard, _context_cache_guard)


# ---------------------------------------------------------------- 非对话模型（2026-10-19）
# 为什么需要：视频/图像/音频/向量模型和中转站的聊天模型混在同一个 /v1/models 列表里
# （例：qwen-image-3.0、wan2.2-t2v-plus、wan2.2-i2v-flash）。它们走的是 /v1/images、
# /v1/video、/v1/audio 端点，**切到对话上会卡住或收不到回复**（实测卡 1~2 分钟）。
# 处理方式：① 选择器按钮上打标 ② 切过去前给一次确认（本守卫）。
_NON_CHAT_KINDS = (
    ("视频生成", ("t2v", "i2v", "text-to-video", "image-to-video", "veo-", "veo3", "sora", "kling",
                 "hailuo", "runway", "seedance", "wan2.2", "wanx", "minimax-video")),
    ("图像生成", ("dall-e", "dalle", "flux", "stable-diffusion", "sdxl", "imagen", "seedream",
                 "ideogram", "midjourney", "-image", "image-", "qwen-image")),
    ("语音/音频", ("tts", "whisper", "speech", "music", "-audio", "audio-", "voice-clone")),
    ("向量/排序", ("embed", "rerank", "moderation")),
)
_NON_CHAT_MARKERS = {"视频生成": "🎬", "图像生成": "🖼", "语音/音频": "🔊", "向量/排序": "🧮"}


def non_chat_model_kind(model_id: str) -> Optional[str]:
    """非对话模型的能力归类（"视频生成"/"图像生成"/"语音/音频"/"向量/排序"）；聊天模型返回 None。

    纯字符串判断、无 IO、不抛异常 —— 选择器与守卫共用同一份判定，避免两处规则漂移。
    """
    try:
        mid = (model_id or "").strip().lower()
        if not mid:
            return None
        for kind, tokens in _NON_CHAT_KINDS:
            if any(tok in mid for tok in tokens):
                return kind
        return None
    except Exception:
        return None


def non_chat_model_marker(model_id: str) -> str:
    """选择器按钮前缀标记（非对话模型专用），聊天模型返回空串。"""
    kind = non_chat_model_kind(model_id)
    return _NON_CHAT_MARKERS.get(kind or "", "")


@dataclass(frozen=True)
class _RawGuard:
    """守卫原始载荷：``_wrap`` 只要求 ``.message``，这里多带 model/provider 便于展示。"""

    message: str
    model: str = ""
    provider: str = ""


def _capability_guard(
    model_name: str, provider: Optional[str], base_url: Optional[str], api_key: Optional[str],
    model_info: Optional[ModelInfo], ctx: Optional[SelectionContext] = None) -> Optional[SelectionWarning]:
    """切到「看着不是聊天模型」的模型时提示确认（不阻止，只提醒）。"""
    kind = non_chat_model_kind(model_name)
    if not kind:
        return None
    payload = _RawGuard(
        message=(f"⚠️ 这个模型看着不是聊天模型（{kind}）：`{model_name}`\n"
                 f"它走的是生成端点（图像/视频/音频），切到对话上会卡住或收不到回复。\n"
                 f"要用它请走 image_generate / video_generate 工具。确定还要切换吗？\n"
                 f"（This does not look like a chat model — it will stall the conversation.）"),
        model=model_name, provider=provider or "")
    return _wrap("capability", "Non-chat Model Warning", payload, model_name, provider)


_GUARDS = (_capability_guard, _cost_guard, _data_policy_guard, _context_cache_guard)


def selection_warnings(
    model_name: str, *, provider: Optional[str] = None, base_url: Optional[str] = None,
    api_key: Optional[str] = None, model_info: Optional[ModelInfo] = None,
    include_kinds: Optional[Iterable[str]] = None,
    selection_context: Optional[SelectionContext] = None) -> list[SelectionWarning]:
    """Warnings from every registered guard (empty in the common case). ``include_kinds`` restricts
    which kinds are returned; ``selection_context`` carries live-session facts for switch-aware guards.
    Guard exceptions are swallowed — never break model selection."""
    wanted = set(include_kinds) if include_kinds is not None else None
    results: list[SelectionWarning] = []
    for guard in _GUARDS:
        try:
            warning = guard(model_name, provider, base_url, api_key, model_info, selection_context)
        except Exception:
            continue
        if warning is not None and (wanted is None or warning.kind in wanted):
            results.append(warning)
    return results


def combined_message(warnings: list[SelectionWarning]) -> str:
    """One confirm-prompt body for several warnings (one prompt beats two sequential ones)."""
    return "\n\n".join(w.message for w in warnings)


def combined_selection_warning(
    model_name: str, *, provider: Optional[str] = None, base_url: Optional[str] = None,
    api_key: Optional[str] = None, model_info: Optional[ModelInfo] = None,
    selection_context: Optional[SelectionContext] = None,
) -> Optional[SelectionWarning]:
    """Drop-in for ``expensive_model_warning`` call sites: ``None``, the single warning, or a merged
    ``kind="multiple"`` warning stacking every message."""
    warnings = selection_warnings(
        model_name, provider=provider, base_url=base_url, api_key=api_key, model_info=model_info,
        selection_context=selection_context)
    if not warnings:
        return None
    if len(warnings) == 1:
        return warnings[0]
    return SelectionWarning(
        kind="multiple", title="Model Selection Warning", model=warnings[0].model,
        provider=warnings[0].provider, message=combined_message(warnings))
