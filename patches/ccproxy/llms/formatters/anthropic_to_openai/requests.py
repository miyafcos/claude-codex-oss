"""Request conversion entry points for Anthropic→OpenAI adapters."""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

from ccproxy.llms.formatters.context import register_request
from ccproxy.llms.models import anthropic as anthropic_models
from ccproxy.llms.models import openai as openai_models

# Opt-in context-compaction helpers. When CCPROXY_CONTEXT_COMPACTION=1 these
# attach a stable prompt_cache_key derived from instructions+tools so OpenAI's
# automatic prefix-cache routing stays sticky. Default OFF — no behaviour change.
from . import compaction as _compaction
from .reasoning_models import MAX_EFFORT_MODELS, REASONING_MODELS


# --- Pre-validation normalization for non-standard Anthropic block types ---

_KNOWN_CONTENT_BLOCK_TYPES = frozenset({"text", "image", "tool_use", "tool_result"})
_KNOWN_TOOL_RESULT_CONTENT_TYPES = frozenset({"text", "image"})


def _preserve_result_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_preserve_result_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "document":
            return {"type": "text", "text": _summarize_beta_block_to_text(value)}
        if value.get("type") in {"image", "container_upload", "base64", "file"}:
            raise ValueError("Unsupported binary nested in a server result; supply text or a tool_result image")
        return {key: _preserve_result_value(item) for key, item in value.items()}
    return value


def _summarize_beta_block_to_text(block: dict[str, Any]) -> str | None:
    """Return a human-readable label for known Anthropic beta block types.

    Returns ``None`` for unknown shapes so the caller falls back to a
    generic JSON dump.  Each label is short, prefix-tagged, and preserves
    any obviously useful identifier (tool name, search query, etc.) so the
    downstream model still gets enough context to reason about what the
    original block represented.
    """
    block_type = block.get("type")
    if not isinstance(block_type, str):
        return None

    # Tool search beta (Claude Code ToolSearch responses).
    if block_type == "tool_reference":
        tool_name = block.get("tool_name") or block.get("name") or "unknown"
        return f"[tool_reference: {tool_name}]"

    # Extended-thinking blocks. Claude Code occasionally replays these
    # in subsequent requests; downstream OpenAI does not accept them so
    # we surface only the visible thought text.
    if block_type == "thinking":
        thought = block.get("thinking") or block.get("text") or ""
        if isinstance(thought, str) and thought:
            return f"[thinking: {thought}]"
        return "[thinking: (omitted)]"
    if block_type == "redacted_thinking":
        return "[redacted_thinking]"

    # Server-side tool invocations (web search, MCP, code exec, bash).
    if block_type == "server_tool_use":
        name = block.get("name", "unknown")
        tool_input = block.get("input")
        if tool_input is None:
            return f"[server_tool_use: {name}]"
        try:
            args = json.dumps(tool_input, ensure_ascii=False)
        except Exception:
            args = str(tool_input)
        return f"[server_tool_use: {name} args={args}]"
    if block_type == "mcp_tool_use":
        server = block.get("server_name") or block.get("server") or "unknown"
        name = block.get("name", "unknown")
        return f"[mcp_tool_use: {server}.{name}]"
    if block_type == "mcp_tool_result":
        tool_use_id = block.get("tool_use_id", "")
        # The inner content array uses the same shape as tool_result.
        inner = block.get("content")
        inner = _preserve_result_value(inner)
        if isinstance(inner, list):
            parts: list[str] = []
            for ib in inner:
                if isinstance(ib, dict) and ib.get("type") == "text":
                    text_val = ib.get("text")
                    if isinstance(text_val, str):
                        parts.append(text_val)
            if parts:
                if len(parts) == len(inner):
                    return f"[mcp_tool_result {tool_use_id}: {' '.join(parts)}]"
                return f"[mcp_tool_result {tool_use_id}] " + json.dumps(inner, ensure_ascii=False)
        if inner:
            return f"[mcp_tool_result {tool_use_id}] " + json.dumps(inner, ensure_ascii=False)
        return f"[mcp_tool_result {tool_use_id}]"
    if block_type == "web_search_tool_result":
        tool_use_id = block.get("tool_use_id", "")
        return f"[web_search_tool_result {tool_use_id}]" + (" " if "content" in block else "") + (json.dumps(_preserve_result_value(block["content"]), ensure_ascii=False) if "content" in block else "")
    if block_type in ("code_execution_tool_result", "bash_code_execution_tool_result"):
        tool_use_id = block.get("tool_use_id", "")
        return f"[{block_type} {tool_use_id}]" + (" " if "content" in block else "") + (json.dumps(_preserve_result_value(block["content"]), ensure_ascii=False) if "content" in block else "")
    if block_type == "web_fetch_tool_result":
        return "[web_fetch_tool_result] " + json.dumps(_preserve_result_value(block.get("content")), ensure_ascii=False)

    # Files API beta — large binary upload reference.
    if block_type == "container_upload":
        raise ValueError("Unsupported container_upload: binary files cannot be read by this bridge")

    # Document blocks (PDFs, etc.). The downstream OpenAI path does not
    # support arbitrary document binaries on this code path, so collapse
    # to a short reference rather than dumping the base64 payload.
    if block_type == "document":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "text" and isinstance(source.get("data"), str):
            return source["data"]
        if isinstance(source, dict) and source.get("type") == "content":
            return _coerce_block_text(source.get("content"))
        raise ValueError("Unsupported document source: this backend does not support PDF input or other binary/URL/file documents. Read PDFs with pages to supply page images, or provide extracted text; document content has not been read.")

    return None


def _normalize_tool_result_content_blocks(blocks: list[Any]) -> list[Any]:
    """Convert non-standard items in tool_result.content to text blocks.

    Standard ``text``/``image`` items pass through.  Recognized beta
    types get a short labeled string; everything else is JSON-dumped.
    """
    result: list[Any] = []
    for item in blocks:
        if not isinstance(item, dict):
            result.append(item)
            continue
        item_type = item.get("type")
        if item_type == "mcp_tool_result" and isinstance(item.get("content"), list):
            result.extend(_normalize_tool_result_content_blocks(item["content"]))
            continue
        if item_type in _KNOWN_TOOL_RESULT_CONTENT_TYPES:
            result.append(item)
            continue
        label = _summarize_beta_block_to_text(item)
        if label is not None:
            result.append({"type": "text", "text": label})
        else:
            result.append(
                {"type": "text", "text": json.dumps(item, ensure_ascii=False)}
            )
    return result


def _normalize_unknown_block_to_text(block: dict[str, Any]) -> dict[str, Any]:
    """Convert an unrecognized content block to a text block.

    Recognized beta block types are summarized with a short label that
    preserves useful identifiers (tool name, server name, etc.); unknown
    shapes fall back to a JSON dump of the original block, or to the
    block's own ``text`` field when one is present.
    """
    label = _summarize_beta_block_to_text(block)
    if label is not None:
        return {"type": "text", "text": label}
    text_value = block.get("text")
    if isinstance(text_value, str):
        return {"type": "text", "text": text_value}
    return {"type": "text", "text": json.dumps(block, ensure_ascii=False)}


def _has_cache_control(obj: Any) -> bool:
    """Quick scan for any nested cache_control key."""
    if isinstance(obj, dict):
        if "cache_control" in obj:
            return True
        return any(_has_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_cache_control(x) for x in obj)
    return False


def _strip_cache_control_recursive(obj: Any) -> Any:
    """Drop Anthropic-only ``cache_control`` keys from any nested dict.

    OpenAI Responses API ignores ``cache_control`` and Anthropic prompt-cache
    semantics do not transfer to the OpenAI backend. Stripping shrinks the
    payload, avoids unnecessary work in the format-chain validator, and keeps
    the outbound request free of dead metadata.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_cache_control_recursive(v)
            for k, v in obj.items()
            if k != "cache_control"
        }
    if isinstance(obj, list):
        return [_strip_cache_control_recursive(x) for x in obj]
    return obj


def _coerce_block_text(content: Any) -> str:
    """Extract concatenated text from a message ``content`` (str or block list).

    Used to lift the human-readable payload out of an in-band system message.
    Recognized beta blocks are summarized with a short label; unknown shapes
    fall back to a JSON dump so no context is silently dropped.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
            continue
        label = _summarize_beta_block_to_text(block)
        parts.append(label if label is not None else json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _absorb_system_role_messages(data: dict[str, Any]) -> dict[str, Any]:
    """Preserve in-band system positions and effort outside the legacy role validator."""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return data
    if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return data

    data = copy.deepcopy(data)
    system_messages: list[dict[str, Any]] = []
    remaining: list[Any] = []
    for index, msg in enumerate(data["messages"]):
        if isinstance(msg, dict) and msg.get("role") == "system":
            unknown = set(msg) - {"role", "content", "output_config", "cache_control", "clear_at"}
            if unknown:
                raise ValueError(f"Unsupported in-band system controls: {sorted(unknown)}")
            clear_at = msg.get("clear_at")
            if clear_at not in (None, "next_user_message"):
                raise ValueError(f"Unsupported in-band system clear_at: {clear_at}")
            if clear_at == "next_user_message" and any(
                isinstance(later, dict) and later.get("role") == "user"
                for later in data["messages"][index + 1:]
            ):
                continue
            config = msg.get("output_config", {})
            if not isinstance(config, dict) or set(config) - {"effort"}:
                raise ValueError("Unsupported in-band system output_config")
            text = _coerce_block_text(msg.get("content"))
            system_messages.append({"position": len(remaining), "text": text, "output_config": config})
        else:
            remaining.append(msg)
    data["messages"] = remaining

    data["bridge_system_messages"] = system_messages
    return data


def normalize_request_for_validation(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize non-standard Anthropic block types before Pydantic validation.

    Claude Code may send beta-only block types (tool_reference, etc.) that the
    standard ccproxy Anthropic models do not recognize.  This function converts
    them to plain text blocks so that ``CreateMessageRequest.model_validate()``
    succeeds.

    It also lifts in-band ``role: "system"`` messages (injected by recent
    Claude Code as SessionStart hook output) into the top-level system field,
    and strips Anthropic-only ``cache_control`` keys, which are no-ops on the
    OpenAI backend.
    """
    data = _absorb_system_role_messages(data)
    thinking = data.get("thinking")
    if isinstance(thinking, dict) and thinking.get("display") == "updates":
        data = copy.deepcopy(data)
        data["bridge_thinking_display"] = "updates"
        data["thinking"]["display"] = "omitted"

    if _has_cache_control(data):
        data = _strip_cache_control_recursive(data)

    # WP-AT: opt-in latency knob. When CCPROXY_BREVITY_HINT=1, prepend a tiny
    # "be concise" instruction to the system blocks so the model produces
    # shorter responses (output tokens dominate wall-clock latency once the
    # input prefix is cached). Default OFF — independent of the compaction
    # flag because this is a UX-affecting behaviour, not a cache hint.
    if _compaction.is_brevity_hint_enabled():
        sys_in = data.get("system")
        sys_out = _compaction.prepend_brevity_hint_to_system(sys_in)
        if sys_out is not sys_in:
            data = dict(data)
            data["system"] = sys_out

    # Opt-in: drop the volatile ``x-anthropic-billing-header`` system block
    # that Claude Code prepends to every request. It carries a per-request
    # correlation hash that is meaningless to OpenAI and otherwise pollutes
    # trace logs / hash-based dedup. No-op unless CCPROXY_CONTEXT_COMPACTION=1.
    if _compaction.is_enabled():
        sys_in = data.get("system")
        sys_out = _compaction.strip_volatile_system_blocks(sys_in)
        if sys_out is not sys_in:
            data = dict(data)
            data["system"] = sys_out

        # Aggressive opt-in (CCPROXY_SKILL_LAZY_LOAD=1, also requires
        # CCPROXY_CONTEXT_COMPACTION=1): when the latest user request is short
        # and contains no skill-routing markers, strip ``<skill>...</skill>``
        # blocks from the system catalogue. Tool definitions are preserved —
        # only the long human-readable catalogue text is removed.
        if _compaction.is_skill_lazy_load_enabled():
            sys_curr = data.get("system")
            sys_pruned = _compaction.maybe_strip_skill_blocks_from_system(
                sys_curr, data.get("messages")
            )
            if sys_pruned is not sys_curr:
                if data is not None:
                    data = dict(data) if data is not data else dict(data)
                data["system"] = sys_pruned

    messages = data.get("messages")
    if not isinstance(messages, list):
        return data

    needs_copy = False
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_result" and isinstance(block.get("content"), list):
                for inner in block["content"]:
                    if isinstance(inner, dict) and inner.get("type") not in _KNOWN_TOOL_RESULT_CONTENT_TYPES:
                        needs_copy = True
                        break
            elif bt not in _KNOWN_CONTENT_BLOCK_TYPES:
                needs_copy = True
            if needs_copy:
                break
        if needs_copy:
            break

    if not needs_copy:
        return data

    data = copy.deepcopy(data)
    for msg in data["messages"]:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        normalized: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                normalized.append(block)
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    block = dict(block)
                    block["content"] = _normalize_tool_result_content_blocks(inner)
                normalized.append(block)
            elif block_type == "thinking" and msg.get("role") == "assistant" and isinstance(block.get("signature"), str) and block["signature"].startswith(("ccproxy-openai-replay-v1:", "ccproxy-openai-replay-v2:")):
                # An opaque bridge reference is never converted into model text.
                # The upstream typed union lacks ThinkingBlock. Carry only our
                # private reference through a supported empty text block; the
                # Responses builder consumes it before any text serialization.
                if re.fullmatch(r"ccproxy-openai-replay-v2:[0-9a-f]{64}\.[0-9a-f]{64}", block["signature"]):
                    normalized.append({"type": "text", "text": "", "_bridge_replay_reference": block["signature"]})
            elif block_type not in _KNOWN_CONTENT_BLOCK_TYPES:
                normalized.append(_normalize_unknown_block_to_text(block))
            else:
                normalized.append(block)
        msg["content"] = normalized

    return data


# --- Reasoning effort mapping ---


_MAX_EFFORT_MODEL_PREFIXES: tuple[str, ...] = MAX_EFFORT_MODELS


def _normalize_reasoning_effort(value: Any, model_name: str = "") -> str | None:
    """Return a normalized OpenAI reasoning effort label when possible."""

    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None

    supports_max = model_name.strip().lower() in MAX_EFFORT_MODELS
    max_target = "max" if supports_max else "xhigh"
    effort_aliases = {
        "min": "minimal",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "max": max_target,
        "maximum": max_target,
        "xhigh": "xhigh",
    }
    return effort_aliases.get(normalized, normalized)


def _derive_openai_reasoning_from_anthropic_request(
    request: anthropic_models.CreateMessageRequest,
) -> dict[str, Any] | None:
    """Map Anthropic thinking semantics onto OpenAI Responses reasoning fields."""

    model_name = (request.model or "").strip().lower()
    is_gpt5x = model_name in REASONING_MODELS or model_name.startswith(("gpt-5.4", "gpt-5.5"))

    thinking_cfg = request.thinking
    request_extra = getattr(request, "model_extra", None) or {}
    output_config = getattr(request, "output_config", None)
    if output_config is None and isinstance(request_extra, dict):
        output_config = request_extra.get("output_config")
    output_effort = (
        output_config.get("effort")
        if isinstance(output_config, dict)
        else getattr(output_config, "effort", None)
    )
    for system_message in request_extra.get("bridge_system_messages", []):
        config = system_message.get("output_config", {})
        if "effort" in config:
            output_effort = config["effort"]
    effort = _normalize_reasoning_effort(output_effort, model_name=model_name)

    for extra_source in (
        request_extra,
        getattr(thinking_cfg, "model_extra", None) or {},
    ):
        if effort:
            break
        if not isinstance(extra_source, dict):
            continue
        for key in ("reasoning_effort", "effort", "thinking_effort", "max_effort"):
            effort = _normalize_reasoning_effort(
                extra_source.get(key), model_name=model_name
            )
            if effort:
                break
        if effort:
            break

    if effort is None:
        thinking_type = getattr(thinking_cfg, "type", None)
        if thinking_cfg is None and is_gpt5x:
            effort = _normalize_reasoning_effort(
                os.getenv("CCPROXY_CODEX_DEFAULT_REASONING_EFFORT"),
                model_name=model_name,
            )
        elif thinking_type == "enabled":
            effort = "medium"
        elif thinking_type == "adaptive" and is_gpt5x:
            effort = _normalize_reasoning_effort(
                os.getenv("CCPROXY_CODEX_ADAPTIVE_REASONING_EFFORT", "xhigh"),
                model_name=model_name,
            )

    if not effort:
        return None

    summary = (os.getenv("CCPROXY_CODEX_REASONING_SUMMARY", "auto") or "auto").strip()
    if not summary:
        summary = "auto"

    return {
        "effort": effort,
        "summary": summary,
    }


def _anthropic_block_get(block: Any, key: str, default: Any = None) -> Any:
    """Read block fields from either Anthropic models or raw dict payloads."""

    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _json_stringify(value: Any) -> str:
    """Serialize values for Responses API function call fields."""

    if isinstance(value, str):
        return value

    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _normalize_function_call_item_id(tool_use_id: Any, fallback_index: int) -> tuple[str, str]:
    """Return a Responses-safe (id, call_id) pair for a replayed tool call."""

    if isinstance(tool_use_id, str) and tool_use_id:
        call_id = tool_use_id
    else:
        call_id = f"call_{fallback_index}"

    if call_id.startswith("fc_"):
        item_id = call_id
    else:
        item_id = f"fc_{call_id}"

    return item_id, call_id


def _stringify_tool_result_content(content: Any) -> str:
    """Normalize Anthropic tool_result payloads into a single output string."""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        normalized_parts: list[Any] = []
        text_parts: list[str] = []

        for part in content:
            part_type = _anthropic_block_get(part, "type")
            if part_type == "image":
                text_parts.append("[image supplied in the following user input]")
                normalized_parts.append({"type": "text", "text": text_parts[-1]})
                continue
            if part_type == "text":
                text_value = _anthropic_block_get(part, "text")
                if isinstance(text_value, str):
                    text_parts.append(text_value)
                    normalized_parts.append({"type": "text", "text": text_value})
                    continue

            if isinstance(part, dict):
                normalized_parts.append(part)
            elif hasattr(part, "model_dump"):
                normalized_parts.append(part.model_dump(mode="json"))
            else:
                normalized_parts.append(str(part))

        if text_parts and len(text_parts) == len(normalized_parts):
            return " ".join(text_parts)

        return _json_stringify(normalized_parts)

    if hasattr(content, "model_dump"):
        return _json_stringify(content.model_dump(mode="json"))

    return _json_stringify(content)


def _convert_anthropic_content_block_to_responses_content(
    block: Any, role: str
) -> dict[str, Any] | None:
    """Map Anthropic text/image blocks to Responses message content blocks."""

    block_type = _anthropic_block_get(block, "type")

    if block_type == "text":
        text_value = _anthropic_block_get(block, "text")
        if not isinstance(text_value, str) or not text_value:
            return None
        return {
            "type": "input_text" if role == "user" else "output_text",
            "text": text_value,
        }

    if role == "user" and block_type == "image":
        source = _anthropic_block_get(block, "source")
        if not isinstance(source, dict):
            if hasattr(source, "model_dump"):
                source = source.model_dump(mode="json")
            else:
                source = None
        if (
            isinstance(source, dict)
            and source.get("type") == "base64"
            and isinstance(source.get("media_type"), str)
            and isinstance(source.get("data"), str)
        ):
            return {
                "type": "input_image",
                "image_url": f"data:{source['media_type']};base64,{source['data']}",
            }
        if isinstance(source, dict) and source.get("type") == "url" and isinstance(source.get("url"), str):
            return {"type": "input_image", "image_url": source["url"]}
        raise ValueError("Unsupported image source: expected base64 with media_type/data or URL")

    return None


def _append_responses_message_input_item(
    input_items: list[dict[str, Any]], role: str, content: list[dict[str, Any]]
) -> None:
    """Append a Responses message item when there is remaining message content."""

    if not content:
        return
    input_items.append({"type": "message", "role": role, "content": list(content)})


def _build_responses_input_items_from_anthropic_messages(
    messages: list[anthropic_models.Message],
) -> list[dict[str, Any]]:
    """Convert the full Anthropic conversation into Responses input items."""

    input_items: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.role
        content = msg.content

        if isinstance(content, str):
            text_value = content.strip()
            if text_value:
                input_items.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": [
                            {
                                "type": (
                                    "input_text" if role == "user" else "output_text"
                                ),
                                "text": text_value,
                            }
                        ],
                    }
                )
            continue

        if not isinstance(content, list):
            continue

        pending_content: list[dict[str, Any]] = []

        for block in content:
            block_type = _anthropic_block_get(block, "type")

            if role == "assistant" and block_type == "text":
                signature = _anthropic_block_get(block, "_bridge_replay_reference")
                if isinstance(signature, str) and signature.startswith(("ccproxy-openai-replay-v1:", "ccproxy-openai-replay-v2:")):
                    _append_responses_message_input_item(input_items, role, pending_content)
                    pending_content = []
                    input_items.append({"type": "reasoning", "id": "bridge_ref", "summary": [], "encrypted_content": signature})
                    continue

            if role == "assistant" and block_type == "tool_use":
                _append_responses_message_input_item(
                    input_items, role, pending_content
                )
                pending_content = []

                tool_name = _anthropic_block_get(block, "name")
                tool_id = _anthropic_block_get(block, "id")
                tool_input = _anthropic_block_get(block, "input", {}) or {}
                item_id, call_id = _normalize_function_call_item_id(
                    tool_id, len(input_items)
                )
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": tool_name or "",
                        "arguments": _json_stringify(tool_input),
                    }
                )
                continue

            if role == "user" and block_type == "tool_result":
                _append_responses_message_input_item(
                    input_items, role, pending_content
                )
                pending_content = []

                tool_use_id = _anthropic_block_get(block, "tool_use_id")
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_use_id or "",
                        "output": _stringify_tool_result_content(
                            _anthropic_block_get(block, "content", "")
                        ),
                    }
                )
                result_content = _anthropic_block_get(block, "content", "")
                if isinstance(result_content, list):
                    images = [
                        _convert_anthropic_content_block_to_responses_content(part, "user")
                        for part in result_content
                        if _anthropic_block_get(part, "type") == "image"
                    ]
                    if images:
                        _append_responses_message_input_item(input_items, "user", [
                            {"type": "input_text", "text": f"Images returned by tool call {tool_use_id or ''}:"},
                            *images,
                        ])
                continue

            converted_block = _convert_anthropic_content_block_to_responses_content(
                block, role
            )
            if converted_block is not None:
                pending_content.append(converted_block)

        _append_responses_message_input_item(input_items, role, pending_content)

    return input_items


def _convert_native_web_search_tool(tool: Any) -> dict[str, Any]:
    converted: dict[str, Any] = {"type": "web_search", "external_web_access": True}
    allowed = getattr(tool, "allowed_domains", None)
    blocked = getattr(tool, "blocked_domains", None)
    if allowed and blocked:
        raise ValueError("web_search accepts allowed_domains or blocked_domains, not both")
    filters: dict[str, Any] = {}
    for key, domains in (("allowed_domains", allowed), ("blocked_domains", blocked)):
        if domains is not None and (not isinstance(domains, list) or not all(isinstance(domain, str) and domain for domain in domains)):
            raise ValueError(f"web_search {key} must be a list of nonempty domains")
        if domains:
            filters[key] = domains
    if filters:
        converted["filters"] = filters
    location = getattr(tool, "user_location", None)
    if location is not None:
        converted["user_location"] = location.model_dump(exclude_none=True) if hasattr(location, "model_dump") else location
    return converted


def _build_responses_payload_from_anthropic_request(
    request: anthropic_models.CreateMessageRequest,
) -> tuple[dict[str, Any], str | None]:
    """Project an Anthropic message request into Responses payload fields."""

    payload_data: dict[str, Any] = {"model": request.model}
    instructions_text: str | None = None

    if request.max_tokens is not None:
        payload_data["max_output_tokens"] = int(request.max_tokens)
    if request.stream:
        payload_data["stream"] = True

    reasoning_payload = _derive_openai_reasoning_from_anthropic_request(request)
    if reasoning_payload is not None:
        payload_data["reasoning"] = reasoning_payload

    if request.service_tier is not None:
        payload_data["service_tier"] = request.service_tier
    if request.temperature is not None:
        payload_data["temperature"] = request.temperature
    if request.top_p is not None:
        payload_data["top_p"] = request.top_p

    if request.metadata is not None and hasattr(request.metadata, "model_dump"):
        meta_dump = request.metadata.model_dump()
        payload_data["metadata"] = meta_dump

    if request.system:
        if isinstance(request.system, str):
            instructions_text = request.system
            payload_data["instructions"] = request.system
        else:
            joined = "".join(block.text for block in request.system if block.text)
            instructions_text = joined or None
            if joined:
                payload_data["instructions"] = joined

    system_messages = (getattr(request, "model_extra", None) or {}).get("bridge_system_messages", [])
    input_items: list[dict[str, Any]] = []
    for position in range(len(request.messages) + 1):
        for system_message in system_messages:
            if system_message["position"] == position and system_message["text"]:
                _append_responses_message_input_item(input_items, "developer", [
                    {"type": "input_text", "text": system_message["text"]}
                ])
        if position < len(request.messages):
            input_items.extend(_build_responses_input_items_from_anthropic_messages([request.messages[position]]))
    payload_data["input"] = input_items

    has_web_search = False
    if request.tools:
        tools: list[dict[str, Any]] = []
        for tool in request.tools:
            tool_name = getattr(tool, "name", None)
            tool_type = getattr(tool, "type", None)
            tool_schema = getattr(tool, "input_schema", None)
            if isinstance(tool_type, str) and tool_type.startswith("web_search"):
                has_web_search = True
                tools.append(_convert_native_web_search_tool(tool))
                continue
            if isinstance(tool, anthropic_models.Tool) or (
                isinstance(tool_name, str) and isinstance(tool_schema, dict)
            ):
                tools.append(
                    {
                        "type": "function",
                        "name": tool_name,
                        "description": getattr(tool, "description", None),
                        "parameters": tool_schema,
                    }
                )
        if tools:
            payload_data["tools"] = tools

    tc = request.tool_choice
    if tc is not None:
        tc_type = getattr(tc, "type", None)
        if tc_type == "none":
            payload_data["tool_choice"] = "none"
        elif tc_type == "auto":
            payload_data["tool_choice"] = "auto"
        elif tc_type == "any":
            payload_data["tool_choice"] = "required"
        elif tc_type == "tool":
            name = getattr(tc, "name", None)
            if name:
                if has_web_search and name == "web_search":
                    payload_data["tool_choice"] = {"type": "web_search"}
                else:
                    # Responses API uses flat {"type": "function", "name": ...}
                    payload_data["tool_choice"] = {
                        "type": "function",
                        "name": name,
                    }
        disable_parallel = getattr(tc, "disable_parallel_tool_use", None)
        if isinstance(disable_parallel, bool):
            payload_data["parallel_tool_calls"] = not disable_parallel

    if has_web_search:
        payload_data["include"] = ["web_search_call.action.sources"]
        limits = [getattr(tool, "max_uses", None) for tool in request.tools if isinstance(getattr(tool, "type", None), str) and tool.type.startswith("web_search")]
        for limit in limits:
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                raise ValueError("web_search max_uses must be a positive integer")
        search_limit = min((limit for limit in limits if limit is not None), default=None)
        if search_limit is not None:
            instructions_text = (instructions_text or "") + f"\nDo not exceed {search_limit} native web_search actions in this response."
            payload_data["instructions"] = instructions_text
        if payload_data.get("tool_choice") != "none":
            instructions_text = (instructions_text or "") + (
                "\n\nUse the native web_search tool when searching. "
                "Report only actual search results with their source URLs; "
                "if no search ran or it failed, say so without inventing results."
            )
            payload_data["instructions"] = instructions_text

    payload_data.setdefault("background", None)

    # WP-AT: opt-in latency knob. Cap max_output_tokens to env value when
    # CCPROXY_MAX_OUTPUT_FLOOR=N is set. Treats "client did not set max_tokens"
    # as "no client cap" so the floor takes effect; honours smaller client
    # values via min(). Default OFF.
    _compaction.maybe_apply_max_output_floor_to_payload(payload_data)

    # Opt-in: attach a stable prompt_cache_key so OpenAI keeps requests with
    # the same long instructions+tools prefix on the same warm backend. No-op
    # unless CCPROXY_CONTEXT_COMPACTION=1.
    _compaction.maybe_attach_prompt_cache_key(payload_data)

    return payload_data, instructions_text


def convert__anthropic_message_to_openai_responses__request(
    request: anthropic_models.CreateMessageRequest,
) -> openai_models.ResponseRequest:
    """Convert Anthropic CreateMessageRequest to OpenAI ResponseRequest using typed models."""
    payload_data, instructions_text = _build_responses_payload_from_anthropic_request(
        request
    )

    response_request = openai_models.ResponseRequest.model_validate(payload_data)

    register_request(request, instructions_text)

    return response_request


def convert__anthropic_message_to_openai_chat__request(
    request: anthropic_models.CreateMessageRequest,
) -> openai_models.ChatCompletionRequest:
    """Convert Anthropic CreateMessageRequest to OpenAI ChatCompletionRequest using typed models."""
    openai_messages: list[dict[str, Any]] = []
    # System prompt
    if request.system:
        if isinstance(request.system, str):
            sys_content = request.system
        else:
            sys_content = "".join(block.text for block in request.system)
        if sys_content:
            openai_messages.append({"role": "system", "content": sys_content})

    # User/assistant messages with text + data-url images
    for msg in request.messages:
        role = msg.role
        content = msg.content

        # Handle tool usage and results
        if role == "assistant" and isinstance(content, list):
            tool_calls = []
            text_parts = []
            for block in content:
                block_type = getattr(block, "type", None)
                if block_type == "tool_use":
                    # Type guard for ToolUseBlock
                    if hasattr(block, "id") and hasattr(block, "name"):
                        # Safely get input with fallback to empty dict
                        tool_input = getattr(block, "input", {}) or {}

                        # Ensure input is properly serialized as JSON
                        try:
                            args_str = json.dumps(tool_input)
                        except Exception:
                            args_str = json.dumps({"arguments": str(tool_input)})

                        tool_calls.append(
                            {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": args_str,
                                },
                            }
                        )
                elif block_type == "text":
                    # Type guard for TextBlock
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
            if tool_calls:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": tool_calls,
                }
                assistant_msg["content"] = " ".join(text_parts) if text_parts else None
                openai_messages.append(assistant_msg)
                continue
        elif role == "user" and isinstance(content, list):
            is_tool_result = any(
                getattr(b, "type", None) == "tool_result" for b in content
            )
            if is_tool_result:
                for block in content:
                    if getattr(block, "type", None) == "tool_result":
                        # Type guard for ToolResultBlock
                        if hasattr(block, "tool_use_id"):
                            # Get content with an empty string fallback
                            result_content = getattr(block, "content", "")

                            # Convert complex content to string representation
                            if not isinstance(result_content, str):
                                try:
                                    if isinstance(result_content, list):
                                        # Handle list of text blocks
                                        text_parts = []
                                        for part in result_content:
                                            if (
                                                hasattr(part, "text")
                                                and hasattr(part, "type")
                                                and part.type == "text"
                                            ):
                                                text_parts.append(part.text)
                                        if text_parts:
                                            result_content = " ".join(text_parts)
                                        else:
                                            result_content = json.dumps(result_content)
                                    else:
                                        # Convert other non-string content to JSON
                                        result_content = json.dumps(result_content)
                                except Exception:
                                    # Fallback to string representation
                                    result_content = str(result_content)

                            openai_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": block.tool_use_id,
                                    "content": result_content,
                                }
                            )
                continue

        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            text_accum: list[str] = []
            for block in content:
                # Support both raw dicts and Anthropic model instances
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text" and isinstance(block.get("text"), str):
                        text_accum.append(block.get("text") or "")
                    elif btype == "image":
                        source = block.get("source") or {}
                        if (
                            isinstance(source, dict)
                            and source.get("type") == "base64"
                            and isinstance(source.get("media_type"), str)
                            and isinstance(source.get("data"), str)
                        ):
                            url = f"data:{source['media_type']};base64,{source['data']}"
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": url},
                                }
                            )
                else:
                    # Pydantic models
                    btype = getattr(block, "type", None)
                    if (
                        btype == "text"
                        and hasattr(block, "text")
                        and isinstance(getattr(block, "text", None), str)
                    ):
                        text_accum.append(block.text or "")
                    elif btype == "image":
                        source = getattr(block, "source", None)
                        if (
                            source is not None
                            and getattr(source, "type", None) == "base64"
                            and isinstance(getattr(source, "media_type", None), str)
                            and isinstance(getattr(source, "data", None), str)
                        ):
                            url = f"data:{source.media_type};base64,{source.data}"
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": url},
                                }
                            )
            if parts or len(text_accum) > 1:
                if text_accum:
                    parts.insert(0, {"type": "text", "text": " ".join(text_accum)})
                openai_messages.append({"role": role, "content": parts})
            else:
                openai_messages.append(
                    {"role": role, "content": (text_accum[0] if text_accum else "")}
                )
        else:
            openai_messages.append({"role": role, "content": content})

    # Tools mapping (custom tools -> function tools)
    tools: list[dict[str, Any]] = []
    if request.tools:
        for tool in request.tools:
            tool_type = getattr(tool, "type", None)
            if isinstance(tool_type, str) and tool_type.startswith("web_search"):
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "description": "Search the web for information.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query"},
                                },
                                "required": ["query"],
                            },
                        },
                    }
                )
                continue
            if isinstance(tool, anthropic_models.Tool):
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                )

    params: dict[str, Any] = {
        "model": request.model,
        "messages": openai_messages,
        "max_completion_tokens": request.max_tokens,
        "stream": request.stream or None,
    }
    if tools:
        params["tools"] = tools

    # tool_choice mapping
    tc = request.tool_choice
    if tc is not None:
        tc_type = getattr(tc, "type", None)
        if tc_type == "none":
            params["tool_choice"] = "none"
        elif tc_type == "auto":
            params["tool_choice"] = "auto"
        elif tc_type == "any":
            params["tool_choice"] = "required"
        elif tc_type == "tool":
            name = getattr(tc, "name", None)
            if name:
                params["tool_choice"] = {
                    "type": "function",
                    "function": {"name": name},
                }
        # parallel_tool_calls from disable_parallel_tool_use
        disable_parallel = getattr(tc, "disable_parallel_tool_use", None)
        if isinstance(disable_parallel, bool):
            params["parallel_tool_calls"] = not disable_parallel

    # Validate against OpenAI model
    return openai_models.ChatCompletionRequest.model_validate(params)


__all__ = [
    "convert__anthropic_message_to_openai_chat__request",
    "convert__anthropic_message_to_openai_responses__request",
    "normalize_request_for_validation",
]
