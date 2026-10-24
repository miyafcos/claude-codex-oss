"""OpenAI token accounting. Input and output totals already include details."""
from __future__ import annotations

from typing import Any


def usage_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True, exclude_unset=True)
    if not isinstance(value, dict):
        raise ValueError("Invalid OpenAI usage object")
    return value


def normalize_openai_usage(value: Any) -> dict[str, Any]:
    """Preserve missing measurements as None; reject contradictory accounting.

    Cache writes are supported only via the explicit cache_write_tokens detail
    (or cache_write_input_tokens). Unknown details are never guessed as writes.
    No error includes the supplied values or payload.
    """
    raw = usage_dict(value)

    def count(obj: dict[str, Any], *names: str) -> int | None:
        present = [obj[n] for n in names if obj.get(n) is not None]
        if not present:
            return None
        if any(type(n) is not int or n < 0 for n in present):
            raise ValueError("Invalid OpenAI token count")
        if len(set(present)) != 1:
            raise ValueError("Conflicting OpenAI token aliases")
        return present[0]

    input_total = count(raw, "input_tokens", "prompt_tokens")
    output = count(raw, "output_tokens", "completion_tokens")
    def details(*names: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for name in names:
            # Validate each alias, including falsey values. None and {} mean
            # absent details; neither may hide a conflicting nonempty alias.
            for key, value in usage_dict(raw.get(name)).items():
                if value is None:
                    continue
                if key in {"cached_tokens", "cache_write_tokens", "reasoning_tokens"}:
                    count({key: value}, key)
                if key in merged and merged[key] != value:
                    raise ValueError("Conflicting OpenAI usage details")
                merged[key] = value
        return merged

    inp = details("input_tokens_details", "prompt_tokens_details")
    out = details("output_tokens_details", "completion_tokens_details")
    cached = count(inp, "cached_tokens")
    write = count(inp, "cache_write_tokens")
    top_write = count(raw, "cache_write_input_tokens")
    if write is not None and top_write is not None and write != top_write:
        raise ValueError("Conflicting OpenAI cache write counts")
    write = write if write is not None else top_write
    reasoning = count(out, "reasoning_tokens")
    total = count(raw, "total_tokens")
    if input_total is not None and (cached or 0) + (write or 0) > input_total:
        raise ValueError("OpenAI cache details exceed input total")
    if output is not None and (reasoning or 0) > output:
        raise ValueError("OpenAI reasoning detail exceeds output total")
    if total is not None and input_total is not None and output is not None and total != input_total + output:
        raise ValueError("OpenAI total differs from input plus output")
    return {
        "status": "missing" if not raw else "complete" if input_total is not None and output is not None else "partial",
        "input_tokens": input_total,
        "cached_tokens": cached,
        "cache_write_tokens": write,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
    }


def anthropic_usage_fields(value: Any) -> dict[str, Any]:
    usage = normalize_openai_usage(value)
    total = usage["input_tokens"]
    cached = usage["cached_tokens"] or 0
    write = usage["cache_write_tokens"] or 0
    fields: dict[str, Any] = {
        "input_tokens": total - cached - write if total is not None else 0,
        "output_tokens": usage["output_tokens"] or 0,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": write,
        "bridge_usage_status": usage["status"],
    }
    if usage["reasoning_tokens"] is not None:
        fields["output_tokens_details"] = {"thinking_tokens": usage["reasoning_tokens"]}
    return fields


def normalize_anthropic_usage(value: Any) -> dict[str, Any]:
    """Convert disjoint Anthropic input buckets to the inclusive metadata form."""
    raw = usage_dict(value)
    counts = {}
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        number = raw.get(key)
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError("Invalid Anthropic token count")
        counts[key] = number
    total = counts["input_tokens"]
    if total is not None:
        total += (counts["cache_read_input_tokens"] or 0) + (counts["cache_creation_input_tokens"] or 0)
    detail = usage_dict(raw.get("output_tokens_details"))
    result = normalize_openai_usage({
        "input_tokens": total, "output_tokens": counts["output_tokens"],
        "input_tokens_details": {"cached_tokens": counts["cache_read_input_tokens"],
                                 "cache_write_tokens": counts["cache_creation_input_tokens"]},
        "output_tokens_details": {"reasoning_tokens": detail.get("thinking_tokens")},
    } if raw else None)
    status = raw.get("bridge_usage_status")
    if status in {"missing", "partial"}:
        result["status"] = status
        if status == "missing":
            result.update({key: None for key in result if key != "status"})
    return result


def responses_terminal_reason(event: dict[str, Any]) -> str:
    """Classify terminal Responses events without echoing provider error text."""
    kind = event.get("type")
    response = event.get("response")
    response = response if isinstance(response, dict) else {}
    status = response.get("status")
    if kind == "response.completed" and status in {None, "completed"}:
        return "completed"
    if kind == "response.incomplete" and status in {None, "incomplete"}:
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "max_output_tokens":
            return "max_tokens"
        if reason == "content_filter":
            raise ValueError("Upstream response incomplete: content_filter")
        raise ValueError("Upstream response incomplete: unknown_reason")
    error = response.get("error") or event.get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    safe_code = code if code in {"server_error", "rate_limit_exceeded", "invalid_prompt", "context_length_exceeded", "content_filter", "insufficient_quota"} else "unknown_error"
    raise ValueError("Upstream response failed: " + safe_code)
