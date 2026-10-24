"""Direct dict-based conversion functions for use with DictFormatAdapter.

This module provides simple wrapper functions around the existing formatter functions
that operate directly on dictionaries instead of typed Pydantic models.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .protocols import StreamAccumulatorProtocol

from pydantic import TypeAdapter, ValidationError

from ccproxy.core import logging
from ccproxy.core.constants import (
    FORMAT_ANTHROPIC_MESSAGES as ANTHROPIC_MESSAGES,
)
from ccproxy.core.constants import (
    FORMAT_OPENAI_CHAT as OPENAI_CHAT,
)
from ccproxy.core.constants import (
    FORMAT_OPENAI_RESPONSES as OPENAI_RESPONSES,
)
from ccproxy.llms.formatters import (
    anthropic_to_openai,
    openai_to_anthropic,
    openai_to_openai,
)
from ccproxy.llms.formatters import anthropic_to_openai as a2o
from ccproxy.llms.formatters.anthropic_to_openai.requests import normalize_request_for_validation
from ccproxy.llms.formatters.context import get_last_request
from ccproxy.llms.models import anthropic as anthropic_models
from ccproxy.llms.models import openai as openai_models
from ccproxy.llms.models.anthropic import MessageStreamEvent

from .format_adapter import DictFormatAdapter
from .format_registry import FormatRegistry


FormatDict = dict[str, Any]

logger = logging.get_logger(__name__)


_type_adapter_cache: dict[Any, TypeAdapter[Any]] = {}


def _validate_stream_event(model: Any, data: dict[str, Any]) -> Any:
    """Validate a streaming event against the provided model.

    Raises ValidationError when the payload does not conform so callers can fail fast.
    """

    adapter = _type_adapter_cache.get(model)
    if adapter is None:
        adapter = TypeAdapter(model)
        _type_adapter_cache[model] = adapter
    return adapter.validate_python(data)


def _extract_response_text(payload: FormatDict) -> str | None:
    """Extract assistant text from a loosely shaped Responses payload."""
    for key in ("output_text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    text_value = payload.get("text")
    if isinstance(text_value, str) and text_value.strip():
        return text_value

    if isinstance(text_value, dict):
        nested = text_value.get("text")
        if isinstance(nested, str) and nested.strip():
            return nested
        parts = text_value.get("content")
        if isinstance(parts, list):
            fragments = [
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            combined = "".join(fragments).strip()
            if combined:
                return combined

    content_value = payload.get("content")
    if isinstance(content_value, list):
        fragments: list[str] = []
        for part in content_value:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"output_text", "text", "input_text"}:
                text = part.get("text")
                if isinstance(text, str):
                    fragments.append(text)
        combined = "".join(fragments).strip()
        if combined:
            return combined

    return None


def _normalize_openai_responses_payload(data: FormatDict) -> FormatDict:
    """Coerce provider payloads into something ResponseObject can validate."""
    payload = dict(data)
    nested_response = payload.get("response")
    if isinstance(nested_response, dict):
        merged_payload = dict(nested_response)
        for key, value in payload.items():
            if key == "response":
                continue
            merged_payload.setdefault(key, value)
        payload = merged_payload

    payload.setdefault("object", "response")
    payload.setdefault("created_at", int(payload.get("created_at") or 0))
    payload.setdefault("status", payload.get("status") or "completed")
    payload.setdefault("model", payload.get("model") or "")
    payload.setdefault(
        "parallel_tool_calls", bool(payload.get("parallel_tool_calls") or False)
    )

    output_value = payload.get("output")
    if isinstance(output_value, dict):
        payload["output"] = [output_value]
    elif not isinstance(output_value, list):
        payload["output"] = []

    synthesized_output: list[dict[str, Any]] = []

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        summary = reasoning.get("summary")
        if isinstance(summary, list) and summary:
            synthesized_output.append(
                {
                    "type": "reasoning",
                    "id": str(payload.get("id") or "reasoning_0"),
                    "summary": summary,
                }
            )

    tool_calls = payload.get("tool_calls")
    if isinstance(tool_calls, list):
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function_payload = tool_call.get("function")
            function_name: str | None = None
            function_arguments: str | dict[str, Any] | None = None
            if isinstance(function_payload, dict):
                raw_name = function_payload.get("name")
                if isinstance(raw_name, str) and raw_name:
                    function_name = raw_name
                function_arguments = function_payload.get("arguments")
            else:
                raw_name = tool_call.get("name")
                if isinstance(raw_name, str) and raw_name:
                    function_name = raw_name
                function_arguments = tool_call.get("arguments")

            synthesized_output.append(
                {
                    "type": "function_call",
                    "id": str(tool_call.get("id") or f"tool_{index}"),
                    "status": str(tool_call.get("status") or "completed"),
                    "call_id": str(
                        tool_call.get("call_id")
                        or tool_call.get("id")
                        or f"call_{index}"
                    ),
                    "name": function_name or "function",
                    "arguments": function_arguments or "",
                }
            )

    text_content = _extract_response_text(payload)
    if text_content:
        synthesized_output.append(
            {
                "type": "message",
                "id": str(payload.get("id") or "message_0"),
                "status": str(payload.get("status") or "completed"),
                "role": "assistant",
                "content": [{"type": "output_text", "text": text_content}],
            }
        )

    if not payload["output"] and synthesized_output:
        payload["output"] = synthesized_output

    return payload


def _normalize_openai_error_payload(data: FormatDict) -> FormatDict:
    """Coerce OpenAI-style and plain HTTP error payloads into ErrorResponse shape."""
    payload = dict(data)
    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
        return payload

    detail = payload.get("detail")
    if isinstance(detail, dict):
        payload["error"] = detail
        return payload

    if isinstance(detail, str) and detail:
        payload["error"] = {
            "message": detail,
            "type": str(payload.get("type") or "invalid_request_error"),
        }
        return payload

    message = payload.get("message")
    if isinstance(message, str) and message:
        payload["error"] = {
            "message": message,
            "type": str(payload.get("type") or "invalid_request_error"),
        }

    return payload


# Generic stream mapper to DRY conversion loops
async def map_stream(
    stream: AsyncIterator[FormatDict],
    *,
    validator_model: Any,
    converter: Any,
    accumulator: StreamAccumulatorProtocol | None = None,
) -> AsyncIterator[FormatDict]:
    """Map stream with optional accumulation before validation.

    Args:
        stream: Input stream of format dictionaries
        validator_model: Pydantic model for validation
        converter: Converter function to apply to validated stream
        accumulator: Optional accumulator for handling partial chunks

    Returns:
        Converted stream
    """

    async def _typed_stream() -> AsyncIterator[Any]:
        async for chunk_data in stream:
            if accumulator:
                # Accumulate chunk and get complete object if ready
                complete_object = accumulator.accumulate_chunk(chunk_data)

                if complete_object is None:
                    # Still accumulating, skip this chunk
                    continue

                # Have complete object, validate it
                try:
                    yield _validate_stream_event(validator_model, complete_object)
                except ValidationError as exc:
                    logger.debug(
                        "stream_chunk_validation_failed",
                        model=str(validator_model),
                        error=str(exc),
                        action="raise",
                    )
                    raise
            else:
                # No accumulator, validate directly
                try:
                    yield _validate_stream_event(validator_model, chunk_data)
                except ValidationError as exc:
                    logger.debug(
                        "stream_chunk_validation_failed",
                        model=str(validator_model),
                        error=str(exc),
                        action="raise",
                    )
                    raise

    converted_chunks = converter(_typed_stream())
    async for converted_chunk in converted_chunks:
        if hasattr(converted_chunk, "model_dump"):
            yield converted_chunk.model_dump(exclude_unset=True)
        else:
            yield converted_chunk


# OpenAI to Anthropic converters (for plugins that target Anthropic APIs)
async def convert_openai_to_anthropic_request(data: FormatDict) -> FormatDict:
    """Convert OpenAI ChatCompletion request to Anthropic CreateMessage request."""
    # Convert dict to typed model
    request = openai_models.ChatCompletionRequest.model_validate(data)

    # Use existing formatter function
    result = (
        await openai_to_anthropic.convert__openai_chat_to_anthropic_message__request(
            request
        )
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_anthropic_to_openai_response(data: FormatDict) -> FormatDict:
    """Convert Anthropic MessageResponse to OpenAI ChatCompletion response."""
    # Convert dict to typed model
    response = anthropic_models.MessageResponse.model_validate(data)

    # Use existing formatter function
    result = anthropic_to_openai.convert__anthropic_message_to_openai_chat__response(
        response
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_anthropic_to_openai_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert Anthropic MessageStream to OpenAI ChatCompletion stream."""

    async for out_chunk in map_stream(
        stream,
        validator_model=anthropic_models.MessageStreamEvent,
        converter=anthropic_to_openai.convert__anthropic_message_to_openai_chat__stream,
    ):
        yield out_chunk


async def convert_openai_to_anthropic_error(data: FormatDict) -> FormatDict:
    """Convert OpenAI error to Anthropic error."""
    # Convert dict to typed model
    normalized_data = _normalize_openai_error_payload(data)
    error = openai_models.ErrorResponse.model_validate(normalized_data)

    # Use existing formatter function
    result = openai_to_anthropic.convert__openai_to_anthropic__error(error)

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


# Anthropic to OpenAI converters (reverse direction, if needed)
async def convert_anthropic_to_openai_request(data: FormatDict) -> FormatDict:
    """Convert Anthropic CreateMessage request to OpenAI ChatCompletion request."""
    # Normalize non-standard block types before validation
    data = normalize_request_for_validation(data)
    # Convert dict to typed model
    request = anthropic_models.CreateMessageRequest.model_validate(data)

    # Use existing formatter function
    result = anthropic_to_openai.convert__anthropic_message_to_openai_chat__request(
        request
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_openai_to_anthropic_response(data: FormatDict) -> FormatDict:
    """Convert OpenAI ChatCompletion response to Anthropic MessageResponse."""
    # Convert dict to typed model
    response = openai_models.ChatCompletionResponse.model_validate(data)

    # Use existing formatter function
    result = openai_to_anthropic.convert__openai_chat_to_anthropic_messages__response(
        response
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_openai_to_anthropic_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert OpenAI ChatCompletion stream to Anthropic MessageStream."""
    from .chat_accumulator import ChatCompletionAccumulator

    # Use accumulator to handle partial tool calls
    accumulator = ChatCompletionAccumulator()

    async for out_chunk in map_stream(
        stream,
        validator_model=openai_models.ChatCompletionChunk,
        converter=openai_to_anthropic.convert__openai_chat_to_anthropic_messages__stream,
        accumulator=accumulator,
    ):
        yield out_chunk


async def convert_anthropic_to_openai_error(data: FormatDict) -> FormatDict:
    """Convert Anthropic error to OpenAI error."""
    # Convert dict to typed model
    error = anthropic_models.ErrorResponse.model_validate(data)

    # Use existing formatter function
    result = anthropic_to_openai.convert__anthropic_to_openai__error(error)

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


# OpenAI Responses format converters (for Codex plugin)
async def convert_openai_responses_to_anthropic_request(data: FormatDict) -> FormatDict:
    """Convert OpenAI Responses request to Anthropic CreateMessage request."""
    # Convert dict to typed model
    request = openai_models.ResponseRequest.model_validate(data)

    # Use existing formatter function
    result = (
        openai_to_anthropic.convert__openai_responses_to_anthropic_message__request(
            request
        )
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


def _native_search_limit() -> int | None:
    request = get_last_request()
    tools = request.get("tools") if isinstance(request, dict) else getattr(request, "tools", None)
    if not isinstance(tools, list):
        return None
    limits: list[int] = []
    for tool in tools:
        tool_type = tool.get("type") if isinstance(tool, dict) else getattr(tool, "type", None)
        limit = tool.get("max_uses") if isinstance(tool, dict) else getattr(tool, "max_uses", None)
        if isinstance(tool_type, str) and tool_type.startswith("web_search") and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            limits.append(limit)
    return min(limits, default=None)


def _enforce_native_search_limit(output: Any) -> None:
    limit = _native_search_limit()
    if limit is None or not isinstance(output, list):
        return
    ids = set()
    anonymous = 0
    for item in output:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            identifier = item.get("id")
            if isinstance(identifier, str):
                ids.add(identifier)
            else:
                anonymous += 1
    if len(ids) + anonymous > limit:
        raise ValueError("Native web_search exceeded the requested max_uses; response aborted without provider fallback.")


def _native_search_sources(output: Any) -> list[str]:
    urls: list[str] = []
    if not isinstance(output, list):
        return urls
    for item in output:
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = []
        if item.get("type") == "web_search_call":
            action = item.get("action") or {}
            if isinstance(action, dict):
                candidates.extend(action.get("sources") or [])
                if isinstance(action.get("url"), str):
                    candidates.append({"url": action["url"]})
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    candidates.extend(part.get("annotations") or [])
        for source in candidates:
            url = source.get("url") if isinstance(source, dict) else None
            if isinstance(url, str) and url.startswith(("https://", "http://")) and "\n" not in url and "\r" not in url and url not in urls:
                urls.append(url)
    return urls


def _native_search_source_text(urls: list[str]) -> str:
    return "\n\nSources:\n" + "\n".join("- " + url for url in urls) if urls else ""


async def convert_openai_responses_to_anthropic_response(
    data: FormatDict,
) -> FormatDict:
    """Convert OpenAI Responses response to Anthropic MessageResponse."""
    from ccproxy.llms.formatters.utils import openai_usage_to_anthropic_usage

    normalized_data = _normalize_openai_responses_payload(data)
    _enforce_native_search_limit(normalized_data.get("output"))
    from ccproxy.llms.formatters.usage_contract import responses_terminal_reason
    status = normalized_data.get("status", "completed")
    terminal_reason = responses_terminal_reason({"type": "response." + str(status), "response": normalized_data})
    # Validate before Pydantic can coerce a bad token count or discard a detail.
    converted_usage = openai_usage_to_anthropic_usage(normalized_data.get("usage"))
    # Usage has already been validated from raw values. The upstream formatter
    # receives content/status only so Pydantic defaults cannot turn missing
    # fields into measured zeros or trigger contradictory double conversion.
    response = openai_models.ResponseObject.model_validate({**normalized_data, "usage": None})

    # Use existing formatter function
    result = (
        openai_to_anthropic.convert__openai_responses_to_anthropic_message__response(
            response
        )
    )

    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    ordered_content = []
    for item in response.output or []:
        single = response.model_copy(update={"output": [item]})
        ordered_content.extend(openai_to_anthropic.convert__openai_responses_to_anthropic_message__response(single).model_dump(exclude_unset=True).get("content", []))
    result_dict["content"] = ordered_content
    result_dict["usage"] = converted_usage.model_dump(exclude_none=True)
    if terminal_reason == "max_tokens":
        result_dict["stop_reason"] = "max_tokens"
    source_text = _native_search_source_text(_native_search_sources(normalized_data.get("output")))
    if source_text:
        result_dict.setdefault("content", []).append({"type": "text", "text": source_text})
    from ccproxy.plugins.codex.replay_state import finish_reply, abort_turn
    if normalized_data.get("status") == "completed" and not data.get("_bridge_replay_ambiguous"):
        reasoning = [item for item in normalized_data.get("output", []) if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("encrypted_content")]
        marker = finish_reply(result_dict.get("content", []), reasoning, normalized_data.get("model"), output=normalized_data.get("output"))
        if marker:
            result_dict.setdefault("content", []).append(marker)
    else:
        abort_turn()
    return result_dict


async def convert_anthropic_to_openai_responses_request(data: FormatDict) -> FormatDict:
    """Convert Anthropic CreateMessage request to OpenAI Responses request."""
    # Normalize non-standard block types before validation
    data = normalize_request_for_validation(data)
    # Convert dict to typed model
    request = anthropic_models.CreateMessageRequest.model_validate(data)

    # Use existing formatter function
    result = (
        anthropic_to_openai.convert__anthropic_message_to_openai_responses__request(
            request
        )
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_anthropic_to_openai_responses_response(
    data: FormatDict,
) -> FormatDict:
    """Convert Anthropic MessageResponse to OpenAI Responses response."""
    # Convert dict to typed model
    response = anthropic_models.MessageResponse.model_validate(data)

    # Use existing formatter function
    result = (
        anthropic_to_openai.convert__anthropic_message_to_openai_responses__response(
            response
        )
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


# OpenAI Chat ↔ OpenAI Responses converters (for Codex plugin)
async def convert_openai_chat_to_openai_responses_request(
    data: FormatDict,
) -> FormatDict:
    """Convert OpenAI ChatCompletion request to OpenAI Responses request."""
    # Convert dict to typed model
    request = openai_models.ChatCompletionRequest.model_validate(data)

    # Use existing formatter function
    result = await openai_to_openai.convert__openai_chat_to_openai_responses__request(
        request
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_openai_responses_to_openai_chat_response(
    data: FormatDict,
) -> FormatDict:
    """Convert OpenAI Responses response to OpenAI ChatCompletion response."""
    if isinstance(data, dict):
        if data.get("object") == "chat.completion" or (
            "choices" in data and "response" not in data and "model" in data
        ):
            return data

    # Convert dict to typed model
    normalized_data = _normalize_openai_responses_payload(data)
    response = openai_models.ResponseObject.model_validate(normalized_data)

    # Use existing formatter function
    result = openai_to_openai.convert__openai_responses_to_openai_chat__response(
        response
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_openai_chat_to_openai_responses_response(
    data: FormatDict,
) -> FormatDict:
    """Convert OpenAI ChatCompletion response to OpenAI Responses response."""
    # Convert dict to typed model
    response = openai_models.ChatCompletionResponse.model_validate(data)

    # Use existing formatter function
    result = await openai_to_openai.convert__openai_chat_to_openai_responses__response(
        response
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


async def convert_openai_responses_to_openai_chat_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert OpenAI Responses stream to OpenAI ChatCompletion stream."""
    from ccproxy.llms.models.openai import AnyStreamEvent

    async for out_chunk in map_stream(
        stream,
        validator_model=AnyStreamEvent,
        converter=openai_to_openai.convert__openai_responses_to_openai_chat__stream,
    ):
        yield out_chunk


async def convert_openai_chat_to_openai_responses_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert OpenAI ChatCompletion stream to OpenAI Responses stream."""
    from .chat_accumulator import ChatCompletionAccumulator

    # Use accumulator to handle partial tool calls
    accumulator = ChatCompletionAccumulator()

    async for out_chunk in map_stream(
        stream,
        validator_model=openai_models.ChatCompletionChunk,
        converter=openai_to_openai.convert__openai_chat_to_openai_responses__stream,
        accumulator=accumulator,
    ):
        yield out_chunk


async def convert_anthropic_to_openai_responses_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert Anthropic MessageStream to OpenAI Responses stream.

    Avoid dict→model→dict churn by using the shared map_stream helper.
    """

    async for out_chunk in map_stream(
        stream,
        validator_model=MessageStreamEvent,
        converter=a2o.convert__anthropic_message_to_openai_responses__stream,
    ):
        yield out_chunk


async def _normalize_openai_responses_stream_for_anthropic(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Normalize Responses SSE events so the Anthropic stream adapter can consume them."""

    search_output: list[FormatDict] = []
    search_limit = _native_search_limit()
    search_ids: set[str] = set()
    text_seen = False
    async for chunk in stream:
        event_type = chunk.get("type")
        item = chunk.get("item") or {}
        search_id = chunk.get("item_id") if event_type == "response.web_search_call.in_progress" else item.get("id") if event_type == "response.output_item.added" and isinstance(item, dict) and item.get("type") == "web_search_call" else None
        if isinstance(search_id, str) and search_id not in search_ids:
            search_ids.add(search_id)
            if search_limit is not None and len(search_ids) > search_limit:
                yield {"type": "error", "error": {"type": "api_error", "message": "Native web_search exceeded the requested max_uses; response aborted without provider fallback."}}
                return
        if event_type == "response.output_text.delta" and chunk.get("delta"):
            text_seen = True
        if event_type == "response.output_item.done":
            item = chunk.get("item")
            if isinstance(item, dict) and item.get("type") in {"web_search_call", "message"}:
                search_output.append(item)
        if event_type == "response.completed":
            response = chunk.get("response") or {}
            output = response.get("output") if isinstance(response, dict) else None
            sources = _native_search_sources(output if isinstance(output, list) and output else search_output)
            source_text = _native_search_source_text(sources)
            completed_search = any(item.get("type") == "web_search_call" and item.get("status") == "completed" for item in search_output)
            if source_text or (completed_search and not text_seen):
                yield {"type": "response.output_text.delta", "delta": source_text or "Search completed with no source URLs returned."}
                yield {"type": "response.output_text.done", "text": source_text}
        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
        }:
            continue
        if event_type not in {"response.output_item.added", "response.output_item.done"}:
            yield chunk
            continue

        item = chunk.get("item")
        if not isinstance(item, dict):
            yield chunk
            continue

        item_type = item.get("type")
        if item_type != "function_call":
            yield chunk
            continue

        normalized_chunk = dict(chunk)
        normalized_item = dict(item)
        normalized_item["type"] = "output_tool_call"
        normalized_chunk["item"] = normalized_item
        yield normalized_chunk


async def _normalize_anthropic_stream_for_claude_client(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Emit tool_use blocks using Anthropic's incremental input_json_delta shape."""

    async for chunk in stream:
        if chunk.get("type") != "content_block_start":
            yield chunk
            continue

        content_block = chunk.get("content_block")
        if not isinstance(content_block, dict) or content_block.get("type") != "tool_use":
            yield chunk
            continue

        normalized_chunk = dict(chunk)
        normalized_block = dict(content_block)
        input_payload = normalized_block.get("input")
        normalized_block["input"] = {}
        normalized_chunk["content_block"] = normalized_block
        yield normalized_chunk

        if isinstance(input_payload, dict):
            partial_json = json.dumps(input_payload, ensure_ascii=False)
            if partial_json:
                yield {
                    "type": "content_block_delta",
                    "index": chunk.get("index", 0),
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": partial_json,
                    },
                }


async def _dedupe_anthropic_stream_events(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Drop duplicate block/message terminators emitted by the Responses adapter."""

    active_blocks: set[int] = set()
    message_started = False
    message_stopped = False

    async for chunk in stream:
        chunk_type = chunk.get("type")
        if chunk_type == "message_start":
            if message_started:
                continue
            message_started = True
            yield chunk
            continue

        if chunk_type == "content_block_start":
            index = int(chunk.get("index", 0) or 0)
            active_blocks.add(index)
            yield chunk
            continue

        if chunk_type == "content_block_stop":
            index = int(chunk.get("index", 0) or 0)
            if index not in active_blocks:
                continue
            active_blocks.remove(index)
            yield chunk
            continue

        if chunk_type == "message_stop":
            if message_stopped:
                continue
            message_stopped = True
            yield chunk
            continue

        yield chunk


async def convert_openai_responses_to_anthropic_stream(
    stream: AsyncIterator[FormatDict],
) -> AsyncIterator[FormatDict]:
    """Convert usage and opaque state through the existing reply formatter."""
    from ccproxy.plugins.codex.replay_state import active_turn, clean_reasoning, finish_reply, abort_turn

    capture = active_turn() is not None
    reasoning: dict[str, FormatDict] = {}
    served_model = None
    invalid_state = False
    terminal_success = False
    output_events = []
    terminal_output = None

    async def provider_stream() -> AsyncIterator[FormatDict]:
        nonlocal served_model, invalid_state, terminal_success, terminal_output
        async for event in stream:
            if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                terminal_success = event.get("type") == "response.completed" and (event.get("response") or {}).get("status") in {None, "completed"}
            if capture:
                if event.get("type") == "response.output_item.done":
                    if len(output_events) < 64:
                        output_events.append(event)
                    else:
                        invalid_state = True
                response = event.get("response")
                if event.get("type") == "response.completed" and isinstance(response, dict):
                    terminal_output = response.get("output")
                if isinstance(response, dict) and response.get("model"):
                    served_model = response["model"]
                candidates = []
                if event.get("type") == "response.output_item.done":
                    candidates = [event.get("item")]
                if event.get("type") == "response.completed" and isinstance(response, dict):
                    candidates += response.get("output") or []
                for item in candidates:
                    if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("encrypted_content"):
                        valid = clean_reasoning([item])
                        if not valid or len(reasoning) >= 16 and item.get("id") not in reasoning:
                            invalid_state = True
                            continue
                        reasoning[item["id"]] = valid[0]
            yield event

    normalized_stream = _normalize_openai_responses_stream_for_anthropic(provider_stream())

    async def _dict_stream() -> AsyncIterator[FormatDict]:
        async for chunk in openai_to_anthropic.convert__openai_responses_to_anthropic_messages__stream(normalized_stream):
            yield chunk.model_dump(exclude_unset=True) if hasattr(chunk, "model_dump") else chunk

    normalized_client_stream = _normalize_anthropic_stream_for_claude_client(_dict_stream())
    blocks: dict[int, FormatDict] = {}
    arguments: dict[int, str] = {}
    captured_bytes = 0
    try:
        async for out_chunk in _dedupe_anthropic_stream_events(normalized_client_stream):
            kind = out_chunk.get("type")
            index = out_chunk.get("index", 0)
            if capture and captured_bytes <= 512 * 1024:
                captured_bytes += len(json.dumps(out_chunk, ensure_ascii=True))
                if kind == "content_block_start":
                    blocks[index] = dict(out_chunk["content_block"])
                elif kind == "content_block_delta" and index in blocks:
                    delta = out_chunk.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        blocks[index]["text"] = blocks[index].get("text", "") + delta.get("text", "")
                    elif delta.get("type") == "input_json_delta":
                        arguments[index] = arguments.get(index, "") + delta.get("partial_json", "")
                elif kind == "content_block_stop" and index in arguments:
                    try:
                        blocks[index]["input"] = json.loads(arguments[index])
                    except ValueError:
                        invalid_state = True
                elif kind == "message_delta" and not invalid_state and terminal_success:
                    from ccproxy.plugins.codex.replay_state import ordered_response_output
                    ordered = ordered_response_output(terminal_output, output_events)
                    ordered_reasoning = [item for item in ordered or [] if item.get("type") == "reasoning"]
                    marker = finish_reply([blocks[i] for i in sorted(blocks)], ordered_reasoning, served_model, output=ordered)
                    if marker:
                        marker_index = max(blocks, default=-1)+1
                        yield {"type": "content_block_start", "index": marker_index, "content_block": {**marker, "signature": ""}}
                        yield {"type": "content_block_delta", "index": marker_index, "delta": {"type": "signature_delta", "signature": marker["signature"]}}
                        yield {"type": "content_block_stop", "index": marker_index}
            yield out_chunk
    finally:
        abort_turn()


async def convert_openai_responses_to_openai_chat_request(
    data: FormatDict,
) -> FormatDict:
    """Convert OpenAI Responses request to OpenAI ChatCompletion request."""
    # Convert dict to typed model
    request = openai_models.ResponseRequest.model_validate(data)

    # Use existing formatter function
    result = await openai_to_openai.convert__openai_responses_to_openaichat__request(
        request
    )

    # Convert back to dict
    result_dict: FormatDict = result.model_dump(exclude_unset=True)
    return result_dict


# Passthrough and additional error conversion functions
# OpenAI↔OpenAI error formats are identical; return input unchanged.
async def convert_openai_responses_to_anthropic_error(data: FormatDict) -> FormatDict:
    """Convert OpenAI Responses error to Anthropic error."""
    # OpenAI errors are similar across formats - use existing converter
    return await convert_openai_to_anthropic_error(data)


async def convert_anthropic_to_openai_responses_error(data: FormatDict) -> FormatDict:
    """Convert Anthropic error to OpenAI Responses error."""
    # Use existing anthropic -> openai error converter (errors are same format)
    return await convert_anthropic_to_openai_error(data)


async def convert_openai_responses_to_openai_chat_error(data: FormatDict) -> FormatDict:
    """Convert OpenAI Responses error to OpenAI ChatCompletion error."""
    # Errors have the same format between OpenAI endpoints - passthrough
    return data


async def convert_openai_chat_to_openai_responses_error(data: FormatDict) -> FormatDict:
    """Convert OpenAI ChatCompletion error to OpenAI Responses error."""
    # Errors have the same format between OpenAI endpoints - passthrough
    return data


__all__ = [
    "convert_openai_to_anthropic_request",
    "convert_anthropic_to_openai_response",
    "convert_anthropic_to_openai_stream",
    "convert_openai_to_anthropic_error",
    "convert_anthropic_to_openai_request",
    "convert_openai_to_anthropic_response",
    "convert_openai_to_anthropic_stream",
    "convert_anthropic_to_openai_error",
    "convert_openai_responses_to_anthropic_request",
    "convert_openai_responses_to_anthropic_response",
    "convert_openai_responses_to_anthropic_error",
    "convert_anthropic_to_openai_responses_request",
    "convert_anthropic_to_openai_responses_response",
    "convert_anthropic_to_openai_responses_error",
    "convert_anthropic_to_openai_responses_stream",
    "convert_openai_responses_to_anthropic_stream",
    "convert_openai_chat_to_openai_responses_request",
    "convert_openai_responses_to_openai_chat_response",
    "convert_openai_responses_to_openai_chat_error",
    "convert_openai_chat_to_openai_responses_response",
    "convert_openai_chat_to_openai_responses_error",
    "convert_openai_chat_to_openai_responses_stream",
    "convert_openai_responses_to_openai_chat_stream",
    "convert_openai_responses_to_openai_chat_request",
]

# Centralized pair→stage mapping and registration helpers


def get_converter_map() -> dict[tuple[str, str], dict[str, Any]]:
    """Return a mapping of (from, to) → {request, response, error, stream} callables.

    Missing stages are allowed (e.g., error), and will default to passthrough in composition.
    """
    return {
        # OpenAI Chat → Anthropic Messages
        (OPENAI_CHAT, ANTHROPIC_MESSAGES): {
            "request": convert_openai_to_anthropic_request,
            "response": convert_anthropic_to_openai_response,
            "error": convert_anthropic_to_openai_error,
            "stream": convert_anthropic_to_openai_stream,
        },
        # Anthropic Messages → OpenAI Chat
        (ANTHROPIC_MESSAGES, OPENAI_CHAT): {
            "request": convert_anthropic_to_openai_request,
            "response": convert_openai_to_anthropic_response,
            "error": convert_openai_to_anthropic_error,
            "stream": convert_openai_to_anthropic_stream,
        },
        # OpenAI Chat ↔ OpenAI Responses
        (OPENAI_CHAT, OPENAI_RESPONSES): {
            "request": convert_openai_chat_to_openai_responses_request,
            "response": convert_openai_chat_to_openai_responses_response,
            "error": convert_openai_chat_to_openai_responses_error,
            "stream": convert_openai_chat_to_openai_responses_stream,
        },
        (OPENAI_RESPONSES, OPENAI_CHAT): {
            "request": convert_openai_responses_to_openai_chat_request,
            "response": convert_openai_responses_to_openai_chat_response,
            "error": convert_openai_responses_to_openai_chat_error,
            "stream": convert_openai_responses_to_openai_chat_stream,
        },
        # OpenAI Responses ↔ Anthropic Messages
        (OPENAI_RESPONSES, ANTHROPIC_MESSAGES): {
            "request": convert_openai_responses_to_anthropic_request,
            "response": convert_openai_responses_to_anthropic_response,
            "error": convert_openai_responses_to_anthropic_error,
            "stream": convert_openai_responses_to_anthropic_stream,
        },
        (ANTHROPIC_MESSAGES, OPENAI_RESPONSES): {
            "request": convert_anthropic_to_openai_responses_request,
            "response": convert_anthropic_to_openai_responses_response,
            "error": convert_anthropic_to_openai_responses_error,
            "stream": convert_anthropic_to_openai_responses_stream,
        },
    }


def register_converters(registry: FormatRegistry, *, plugin_name: str = "core") -> None:
    """Register DictFormatAdapter instances for all known pairs into the registry."""
    for (src, dst), stages in get_converter_map().items():
        adapter = DictFormatAdapter(
            request=stages.get("request"),
            response=stages.get("response"),
            error=stages.get("error"),
            stream=stages.get("stream"),
            name=f"{src}->{dst}",
        )
        registry.register(
            from_format=src, to_format=dst, adapter=adapter, plugin_name=plugin_name
        )
