"""Optional context-compaction helpers for the Anthropic→OpenAI request path.

Background
----------
Claude Code sends large, mostly-stable system + tools blocks on every
request. The Anthropic backend de-duplicates them via ``cache_control``
markers (Anthropic prompt caching). On the OpenAI Responses API path
(used by claude-codex-hybrid for GPT-5.x) those markers are stripped
because OpenAI ignores them.

Trace analysis (see ``bench/CONTEXT_COMPACTION_DESIGN.md``) showed that:

* The OpenAI Responses API already does **automatic prefix caching**
  and we observe ~35% cached_tokens on real workloads (p50 ≈ 50% per
  request).
* The provider-side ``instructions`` block is already stable (1 unique
  shape across all sampled requests).
* The provider-side ``tools`` array shape is also reused heavily.

So the proxy does not need to invent its own cache. What it *can* do
is help OpenAI's automatic cache by:

1. Adding a stable ``prompt_cache_key`` derived from ``instructions +
   tools``. This is a routing hint to keep requests with the same long
   prefix on the same backend shard, improving the hit rate.
2. Optionally stripping a known-volatile leading "billing header"
   block that Claude Code prepends (``x-anthropic-billing-header``).
   This block is already invisible to the provider in normal flow
   (the Codex plugin replaces ``instructions`` with its own template),
   but stripping it from the Anthropic-side ``system`` array reduces
   noise in trace logs and keeps the per-request hash stable for
   anyone hashing the raw client payload.

Both behaviours are **opt-in** via ``CCPROXY_CONTEXT_COMPACTION=1`` and
are safe to enable on demand. Default OFF.

This module only ships pure helper functions (no I/O, no globals besides
the env-var read) so it can be unit-tested without ccproxy installed.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

# Public env switch
ENV_FLAG = "CCPROXY_CONTEXT_COMPACTION"
# Optional namespace prefix for the routing hint, so multiple users sharing a
# tenant don't accidentally collide when their prefixes happen to hash the same.
ENV_NS = "CCPROXY_CONTEXT_COMPACTION_NS"
# Re-routing prefix passed to OpenAI as ``prompt_cache_key``.
DEFAULT_NS = "claude-codex-hybrid"

# Aggressive opt-in: drop the entire `<skill>...</skill>` injected catalogue
# from system blocks when the *current* user request is short and contains no
# skill-routing tokens (no `/`, no "skill"). This is dangerous-by-default
# because the harness still expects the Skill tool to dispatch — so the tool
# definitions themselves are kept; only the long *catalogue text* is dropped.
# Default OFF.
ENV_SKILL_LAZY_LOAD = "CCPROXY_SKILL_LAZY_LOAD"

# Heuristic threshold for the skill lazy-filter: requests shorter than this
# (in user-text bytes) and free of skill-routing markers are considered
# "skill-irrelevant" and have their `<skill>...</skill>` blocks stripped.
SKILL_LAZY_LOAD_MAX_USER_BYTES = 200

# WP-AT: opt-in latency knobs. Background — see ``bench/CODEX_BEAT_REPORT.md``
# (WP-AK multi-turn finding): once the input prefix is cached, wall-clock
# latency is dominated by *output* token generation, not input. Two knobs:
#
#   CCPROXY_BREVITY_HINT=1
#       Prepend a tiny "be concise" instruction (~80 chars) to system blocks
#       so the model produces shorter responses. Cuts output tokens, which
#       directly reduces latency.
#
#   CCPROXY_MAX_OUTPUT_FLOOR=N
#       Cap ``max_output_tokens`` to ``min(current, N)`` (treats unset as
#       "no current cap" so the floor takes effect). N must be a positive
#       integer; non-numeric / non-positive values disable the knob.
#
# Both default OFF — they trade response richness for latency, so leave them
# off unless the user opts in.
ENV_BREVITY_HINT = "CCPROXY_BREVITY_HINT"
ENV_MAX_OUTPUT_FLOOR = "CCPROXY_MAX_OUTPUT_FLOOR"

# Marker text prepended to system blocks when ENV_BREVITY_HINT is enabled.
# Kept short (~80 chars) so the payload overhead is negligible. The text
# itself is also stable across requests, so it does not break OpenAI's
# automatic prefix cache (it sits at the very start of `instructions`).
BREVITY_HINT_TEXT = (
    "Be concise. Match the user's request length; do not over-explain."
)

# Heuristic: prefixes shorter than this many bytes don't need a routing hint
# (OpenAI's automatic cache is already optimal for tiny prefixes).
MIN_PREFIX_BYTES_FOR_HINT = 1024

# Anthropic-side billing header marker. Claude Code prepends this as the
# first ``system`` block to correlate requests with its billing pipeline.
# It carries a per-request hash (``cch=...``) so it breaks naive hashing.
_BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Return True when context compaction is opt-in enabled."""
    return _truthy(os.getenv(ENV_FLAG))


def is_skill_lazy_load_enabled() -> bool:
    """Return True when the aggressive skill-lazy-filter is opt-in enabled."""
    return _truthy(os.getenv(ENV_SKILL_LAZY_LOAD))


def is_brevity_hint_enabled() -> bool:
    """Return True when CCPROXY_BREVITY_HINT is opt-in enabled."""
    return _truthy(os.getenv(ENV_BREVITY_HINT))


def get_max_output_floor() -> int | None:
    """Return the configured max_output_tokens cap, or ``None`` when unset.

    Non-numeric or non-positive values disable the knob (return ``None``).
    """
    raw = (os.getenv(ENV_MAX_OUTPUT_FLOOR) or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def get_namespace() -> str:
    """Routing-hint namespace. Override via ``CCPROXY_CONTEXT_COMPACTION_NS``."""
    raw = (os.getenv(ENV_NS) or "").strip()
    return raw or DEFAULT_NS


# ---------------------------------------------------------------------------
# Anthropic-side helpers (operate on raw client payloads, before validation)
# ---------------------------------------------------------------------------


def looks_like_billing_header_block(block: Any) -> bool:
    """Return True when ``block`` is the Claude-Code billing-correlation block.

    The block has shape ``{"type": "text", "text": "x-anthropic-billing-header: ..."}``.
    We match conservatively on the literal prefix only.
    """

    if not isinstance(block, dict):
        return False
    if block.get("type") != "text":
        return False
    text = block.get("text")
    if not isinstance(text, str):
        return False
    return text.startswith(_BILLING_HEADER_PREFIX)


def strip_volatile_system_blocks(system: Any) -> Any:
    """Drop the ``x-anthropic-billing-header`` block from a ``system`` array.

    Returns the input unchanged if it isn't a list, or if no volatile
    blocks are present. This is intentionally conservative: only a single
    well-known prefix is stripped, never the actual instructions.
    """

    if not isinstance(system, list):
        return system
    cleaned = [b for b in system if not looks_like_billing_header_block(b)]
    if len(cleaned) == len(system):
        return system
    return cleaned


# ---------------------------------------------------------------------------
# OpenAI-side helpers (operate on the prepared Responses payload)
# ---------------------------------------------------------------------------


def _stable_json(value: Any) -> str:
    """Stable JSON dump for hashing (sort keys, no ASCII escapes)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compute_prefix_hash(instructions: str | None, tools: Any) -> str:
    """SHA256 of (instructions, tools). Empty inputs are normalized to ``""``/``[]``."""

    h = hashlib.sha256()
    h.update((instructions or "").encode("utf-8"))
    h.update(b"\x00")
    if tools is None:
        h.update(b"[]")
    else:
        h.update(_stable_json(tools).encode("utf-8"))
    return h.hexdigest()


def derive_prompt_cache_key(
    instructions: str | None,
    tools: Any,
    *,
    namespace: str | None = None,
) -> str | None:
    """Derive a stable ``prompt_cache_key`` for OpenAI Responses routing.

    Returns ``None`` if the prefix is too short to bother with (small
    prefixes are already optimal under OpenAI's automatic cache).

    The key looks like ``"{namespace}:{16-hex}"`` and is identical for
    any two requests with identical instructions+tools, so OpenAI's load
    balancer will route them to the same backend that already has the
    KV cache warm.
    """

    instr_bytes = len((instructions or "").encode("utf-8"))
    try:
        tools_bytes = len(_stable_json(tools).encode("utf-8")) if tools else 0
    except (TypeError, ValueError):
        tools_bytes = 0

    if (instr_bytes + tools_bytes) < MIN_PREFIX_BYTES_FOR_HINT:
        return None

    ns = (namespace or get_namespace()).strip() or DEFAULT_NS
    digest = compute_prefix_hash(instructions, tools)[:16]
    return f"{ns}:{digest}"


def maybe_attach_prompt_cache_key(
    payload: dict[str, Any],
    *,
    namespace: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Attach a stable ``prompt_cache_key`` to ``payload`` in-place when useful.

    Behaviour:
        * Honours :func:`is_enabled` — no-op when the env flag is unset.
        * Skips if the payload already has ``prompt_cache_key`` and
          ``overwrite=False`` (preserves caller-supplied values).
        * Skips if the prefix is too small to benefit (see
          :data:`MIN_PREFIX_BYTES_FOR_HINT`).
        * Returns the same dict for ergonomic chaining.
    """

    if not isinstance(payload, dict):
        return payload
    if not is_enabled():
        return payload
    if not overwrite and payload.get("prompt_cache_key"):
        return payload

    instructions = payload.get("instructions")
    tools = payload.get("tools")
    key = derive_prompt_cache_key(instructions, tools, namespace=namespace)
    if key:
        payload["prompt_cache_key"] = key
    return payload


# ---------------------------------------------------------------------------
# Skill lazy-load (aggressive opt-in)
# ---------------------------------------------------------------------------

import re as _re

# Compiled once: matches a single Anthropic-style ``<skill>...</skill>`` block
# possibly with surrounding whitespace. Non-greedy on the inner body so multiple
# blocks in one text are stripped independently.
_SKILL_BLOCK_RE = _re.compile(r"\s*<skill\b[^>]*>.*?</skill>\s*", _re.DOTALL)

# Markers in the latest user message that suggest a skill might actually be
# wanted. Conservative — any of these forces us to keep the skill catalogue.
_SKILL_HINT_MARKERS = (
    "/",       # slash command (also catches /codex, /briefing, etc.)
    "skill",
    "/junbi",
    "subagent",
    "agent",
    "@",       # @-mentions of agents/files
    "edu-",    # edu-quality, edu-pre-flight, edu-mock-exam-creator, ...
)


def _last_user_text(messages: Any) -> str:
    """Return concatenated text of the last user message, or "" if none."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    txt = blk.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
            return "\n".join(parts)
        return ""
    return ""


def looks_skill_irrelevant(user_text: str, *, max_bytes: int = SKILL_LAZY_LOAD_MAX_USER_BYTES) -> bool:
    """Return True when the user's request is short and contains no skill hints.

    Conservative: any of the markers in ``_SKILL_HINT_MARKERS`` (slash,
    'skill', '@', 'edu-' …) flips the answer to False so the catalogue stays
    intact. Empty input is also treated as "keep" because it's likely a
    multi-turn continuation we shouldn't second-guess.
    """
    if not isinstance(user_text, str):
        return False
    body = user_text.strip()
    if not body:
        return False
    if len(body.encode("utf-8")) > max_bytes:
        return False
    lowered = body.lower()
    for marker in _SKILL_HINT_MARKERS:
        if marker in lowered:
            return False
    return True


def strip_skill_blocks_from_text(text: str) -> tuple[str, int]:
    """Return ``(cleaned, removed_count)`` after deleting ``<skill>...</skill>``.

    Returns the original string unchanged (and ``count == 0``) when no
    skill blocks are present. Pure string op — no I/O.
    """
    if not isinstance(text, str) or "<skill" not in text:
        return text, 0
    new_text, n = _SKILL_BLOCK_RE.subn("", text)
    return new_text, n


def maybe_strip_skill_blocks_from_system(system: Any, messages: Any) -> Any:
    """Drop ``<skill>...</skill>`` from system blocks when the request is small.

    Returns the input unchanged when:
        * Skill lazy-load is disabled (env flag not set), or
        * The latest user message looks skill-relevant, or
        * No skill blocks are present.

    Always honours :func:`is_enabled` AS WELL — context compaction must
    be on for any aggressive optimization to apply.
    """
    if not is_enabled() or not is_skill_lazy_load_enabled():
        return system
    user_text = _last_user_text(messages)
    if not looks_skill_irrelevant(user_text):
        return system
    if isinstance(system, str):
        cleaned, n = strip_skill_blocks_from_text(system)
        return cleaned if n else system
    if isinstance(system, list):
        any_change = False
        out: list[Any] = []
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") == "text":
                txt = blk.get("text")
                if isinstance(txt, str):
                    new_txt, n = strip_skill_blocks_from_text(txt)
                    if n:
                        any_change = True
                        new_blk = dict(blk)
                        new_blk["text"] = new_txt
                        out.append(new_blk)
                        continue
            out.append(blk)
        return out if any_change else system
    return system


# ---------------------------------------------------------------------------
# Brevity hint + max-output floor (latency knobs, opt-in)
# ---------------------------------------------------------------------------


def prepend_brevity_hint_to_system(system: Any) -> Any:
    """Prepend the brevity instruction to the Anthropic ``system`` array/string.

    Behaviour:
        * Returns the input unchanged when it already starts with the hint
          (idempotent — repeated calls do not stack copies).
        * For a list-of-blocks system, prepends a new ``{"type": "text",
          "text": BREVITY_HINT_TEXT}`` block at index 0.
        * For a plain-string system, prepends ``BREVITY_HINT_TEXT + "\\n\\n"``.
        * For ``None``, returns a 1-element list containing the hint block.
        * Other types (e.g. unknown shapes) pass through unchanged so we do
          not corrupt non-standard payloads.
    """

    if system is None:
        return [{"type": "text", "text": BREVITY_HINT_TEXT}]

    if isinstance(system, str):
        if system.startswith(BREVITY_HINT_TEXT):
            return system
        return BREVITY_HINT_TEXT + "\n\n" + system

    if isinstance(system, list):
        # Idempotency: if the head block is already the brevity hint, no-op.
        if system:
            head = system[0]
            if isinstance(head, dict) and head.get("type") == "text":
                head_text = head.get("text")
                if isinstance(head_text, str) and head_text.startswith(BREVITY_HINT_TEXT):
                    return system
        return [{"type": "text", "text": BREVITY_HINT_TEXT}, *system]

    return system


def maybe_prepend_brevity_hint(system: Any) -> Any:
    """Honour ENV_BREVITY_HINT and prepend the hint when enabled.

    No-op (returns the input unchanged) when the env flag is unset.
    """
    if not is_brevity_hint_enabled():
        return system
    return prepend_brevity_hint_to_system(system)


def apply_max_output_floor(
    current: int | None, floor: int | None = None
) -> int | None:
    """Return the clamped ``max_output_tokens`` honouring the configured floor.

    Behaviour:
        * If ``floor`` is ``None``, look up :func:`get_max_output_floor`. When
          that is also ``None`` (knob disabled), return ``current`` unchanged.
        * If ``current`` is ``None``, return the floor (treat unset client
          cap as "no cap" — the floor takes effect).
        * Otherwise return ``min(current, floor)`` so explicit small caps
          from the client are always respected.
    """
    effective_floor = floor if floor is not None else get_max_output_floor()
    if effective_floor is None:
        return current
    if current is None:
        return effective_floor
    try:
        return min(int(current), int(effective_floor))
    except (TypeError, ValueError):
        return current


def maybe_apply_max_output_floor_to_payload(
    payload: dict[str, Any],
    *,
    field: str = "max_output_tokens",
) -> dict[str, Any]:
    """Clamp ``payload[field]`` in-place when ENV_MAX_OUTPUT_FLOOR is set.

    Returns the same dict for ergonomic chaining. No-op when:
        * ``payload`` is not a dict, or
        * The floor knob is disabled (``get_max_output_floor()`` returns None).
    """
    if not isinstance(payload, dict):
        return payload
    floor = get_max_output_floor()
    if floor is None:
        return payload
    current = payload.get(field)
    new_val = apply_max_output_floor(current, floor)
    if new_val is not None:
        payload[field] = new_val
    return payload


__all__ = [
    "ENV_FLAG",
    "ENV_NS",
    "ENV_SKILL_LAZY_LOAD",
    "ENV_BREVITY_HINT",
    "ENV_MAX_OUTPUT_FLOOR",
    "DEFAULT_NS",
    "MIN_PREFIX_BYTES_FOR_HINT",
    "SKILL_LAZY_LOAD_MAX_USER_BYTES",
    "BREVITY_HINT_TEXT",
    "is_enabled",
    "is_skill_lazy_load_enabled",
    "is_brevity_hint_enabled",
    "get_namespace",
    "get_max_output_floor",
    "looks_like_billing_header_block",
    "strip_volatile_system_blocks",
    "compute_prefix_hash",
    "derive_prompt_cache_key",
    "maybe_attach_prompt_cache_key",
    "looks_skill_irrelevant",
    "strip_skill_blocks_from_text",
    "maybe_strip_skill_blocks_from_system",
    "prepend_brevity_hint_to_system",
    "maybe_prepend_brevity_hint",
    "apply_max_output_floor",
    "maybe_apply_max_output_floor_to_payload",
]
