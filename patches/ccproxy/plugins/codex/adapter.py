import contextlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
import hashlib
import re

from .bridge_identity import canonical_session, ID_HEADERS, AGENT_HEADERS
from .bridge_metadata import COMPAT_MAX_LOG_BYTES, append_bounded, digest, metadata_record, offload_metadata
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ccproxy.auth.exceptions import OAuthTokenRefreshError
from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.plugins.interfaces import (
    DetectionServiceProtocol,
    ProfiledTokenManagerProtocol,
)
from ccproxy.services.adapters.chain_composer import compose_from_chain
from ccproxy.services.adapters.http_adapter import BaseHTTPAdapter
from ccproxy.services.adapters.mock_adapter import MockAdapter
from ccproxy.services.handler_config import HandlerConfig
from ccproxy.streaming import DeferredStreaming, StreamingBufferService
from ccproxy.utils.headers import (
    extract_request_headers,
    extract_response_headers,
    filter_request_headers,
    filter_response_headers,
)
from ccproxy.utils.model_mapper import restore_model_aliases


logger = get_plugin_logger()


# This is the GPT backend's effective context limit from runtime/models.json.
# Keep the fallback local because the installed ccproxy patch cannot import the
# repository's launcher configuration at runtime.
_DEFAULT_CODEX_CONTEXT_LIMIT_TOKENS = 200_000
_ESTIMATED_CHARACTERS_PER_TOKEN = 4
_ID_EXCLUSIONS = frozenset((*ID_HEADERS, *AGENT_HEADERS, "x-ccproxy-trace-id"))


class _ContextOverflowPreflight:
    """Share the provider-event decision with the ASGI response gate."""

    def __init__(self) -> None:
        self.error_event: dict[str, Any] | None = None

    @property
    def rejected(self) -> bool:
        return self.error_event is not None

    def reject(self, error_event: dict[str, Any]) -> None:
        self.error_event = error_event


class _ContextOverflowResponseGate(Response):
    """Hold response.start until the bounded provider preflight has decided."""

    def __init__(self, inner: Response, preflight: _ContextOverflowPreflight) -> None:
        super().__init__(content=b"", status_code=getattr(inner, "status_code", 200))
        self._inner = inner
        self._preflight = preflight

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        held_start: dict[str, Any] | None = None
        response_started = False
        response_complete = False
        response_replaced = False

        def normalize_deferred_transport_error(body: bytes) -> bytes:
            """Convert ccproxy's data-only transport SSE into Anthropic SSE."""
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return body
            if text.startswith("event: error\n") or not text.startswith("data: "):
                return body
            payload_text = text[len("data: ") :].strip()
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError):
                return body
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, dict) or error.get("type") not in {
                "timeout_error",
                "connection_error",
                "http_error",
            }:
                return body
            is_timeout = error.get("type") == "timeout_error"
            error_event = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        "The upstream model timed out before a complete response was available. "
                        "Retry the request."
                        if is_timeout
                        else "The upstream model connection failed before a complete response was available. "
                        "Retry the request."
                    ),
                },
            }
            return (
                "event: error\n"
                f"data: {json.dumps(error_event, separators=(',', ':'))}\n\n"
            ).encode("utf-8")

        async def send_http_400() -> None:
            nonlocal response_replaced, response_started, response_complete

            error_event = self._preflight.error_event
            if error_event is None:
                return
            # Mark the original response as replaced before sending. If the ASGI
            # server raises on disconnect, no later inner frame may retry start.
            response_replaced = True
            body = json.dumps(error_event, separators=(",", ":")).encode("utf-8")
            original_headers = (held_start or {}).get("headers", [])
            excluded = {
                b"content-length",
                b"content-type",
                b"content-encoding",
                b"transfer-encoding",
            }
            headers = [
                (name, value)
                for name, value in original_headers
                if name.lower() not in excluded
            ]
            headers.extend(
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ]
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": headers,
                }
            )
            response_started = True
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                }
            )
            response_complete = True

        async def send_transport_error(exc: httpx.HTTPError) -> None:
            """Return one retryable error without replaying an ambiguous POST."""
            nonlocal response_replaced, response_started, response_complete

            is_timeout = isinstance(exc, httpx.TimeoutException)
            status = 504 if is_timeout else 502
            message = (
                "The upstream model timed out before a complete response was available. "
                "Retry the request."
                if is_timeout
                else "The upstream model connection failed before a complete response was available. "
                "Retry the request."
            )
            error_event = {
                "type": "error",
                "error": {"type": "api_error", "message": message},
            }
            logger.warning(
                "codex_stream_transport_error",
                error_type=type(exc).__name__,
                status_code=status,
                response_started=response_started,
                category="streaming",
            )

            if response_started:
                if response_complete or response_replaced:
                    return
                body = (
                    "event: error\n"
                    f"data: {json.dumps(error_event, separators=(',', ':'))}\n\n"
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    }
                )
                response_complete = True
                return

            response_replaced = True
            body = json.dumps(error_event, separators=(",", ":")).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            response_started = True
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                }
            )
            response_complete = True

        async def gated_send(message: dict[str, Any]) -> None:
            nonlocal held_start, response_started, response_complete
            message_type = message.get("type")
            if response_replaced:
                return

            if message_type == "http.response.start":
                if held_start is not None or response_started:
                    raise RuntimeError("ASGI response started more than once")
                # Provider HTTP errors do not pass through the SSE adapter, so
                # they must retain their original status and body immediately.
                if message.get("status") != 200:
                    await send(message)
                    response_started = True
                else:
                    held_start = message
                return

            if message_type != "http.response.body":
                if held_start is not None and not response_started:
                    await send(held_start)
                    response_started = True
                elif not response_started:
                    raise RuntimeError("ASGI message sent before response start")
                await send(message)
                return

            if held_start is None and not response_started:
                raise RuntimeError("ASGI response body sent before response start")
            if response_complete:
                raise RuntimeError("ASGI response body sent after completion")

            if held_start is not None and self._preflight.rejected:
                await send_http_400()
                return

            if held_start is not None and not response_started:
                await send(held_start)
                response_started = True
            outbound = message
            body = message.get("body", b"")
            if isinstance(body, bytes):
                normalized = normalize_deferred_transport_error(body)
                if normalized is not body:
                    outbound = dict(message)
                    outbound["body"] = normalized
            await send(outbound)
            if not message.get("more_body", False):
                response_complete = True

        for attempt in range(2):
            try:
                await self._inner(scope, receive, gated_send)
                break
            except httpx.HTTPError as exc:
                can_retry_connect = (
                    attempt == 0
                    and held_start is None
                    and not response_started
                    and not response_complete
                    and not response_replaced
                    and isinstance(
                        exc,
                        (httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ConnectError),
                    )
                )
                if can_retry_connect:
                    logger.warning(
                        "codex_stream_connect_retry",
                        error_type=type(exc).__name__,
                        attempt=attempt + 1,
                        category="streaming",
                    )
                    continue
                await send_transport_error(exc)
                break

        # FastAPI attaches endpoint BackgroundTasks to the Response object
        # returned by the adapter, which is this gate rather than the inner
        # DeferredStreaming instance.
        background = getattr(self, "background", None)
        if background is not None:
            await background()


class CodexAdapter(BaseHTTPAdapter):
    """Simplified Codex adapter."""

    def __init__(
        self,
        detection_service: DetectionServiceProtocol,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config=config, **kwargs)
        self.detection_service: DetectionServiceProtocol = detection_service
        self.token_manager: ProfiledTokenManagerProtocol = cast(
            ProfiledTokenManagerProtocol, self.auth_manager
        )
        self.base_url = self.config.base_url.rstrip("/")
        self._compat_trace_path: Path | None = None

    def _compat_trace_enabled(self, request_context: Any | None = None) -> bool:
        if not self._is_claude_native_request(request_context):
            return False
        value = (os.getenv("CCPROXY_CLAUDE_COMPAT_TRACE", "") or "").strip().lower()
        return value not in {"", "0", "false", "off", "no"}

    def _get_compat_trace_path(self) -> Path:
        if self._compat_trace_path is not None:
            return self._compat_trace_path

        configured = os.getenv("CCPROXY_CLAUDE_COMPAT_TRACE_PATH")
        if isinstance(configured, str) and configured.strip():
            self._compat_trace_path = Path(configured.strip())
        else:
            home_dir = Path.home()
            self._compat_trace_path = (
                home_dir / ".claude-codex-proxy" / "logs" / "claude-compat-trace.jsonl"
            )
        return self._compat_trace_path

    def _truncate_trace_value(self, value: Any, depth: int = 0) -> Any:
        if depth >= 6:
            return "<max-depth>"
        if hasattr(value, "model_dump"):
            with contextlib.suppress(Exception):
                return self._truncate_trace_value(value.model_dump(mode="json"), depth + 1)
        if hasattr(value, "dict"):
            with contextlib.suppress(Exception):
                return self._truncate_trace_value(value.dict(), depth + 1)
        if isinstance(value, dict):
            truncated: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 80:
                    truncated["<truncated_keys>"] = len(value) - index
                    break
                truncated[str(key)] = self._truncate_trace_value(item, depth + 1)
            return truncated
        if isinstance(value, list):
            if len(value) > 50:
                return [
                    *[self._truncate_trace_value(item, depth + 1) for item in value[:50]],
                    {"<truncated_items>": len(value) - 50},
                ]
            return [self._truncate_trace_value(item, depth + 1) for item in value]
        if isinstance(value, str) and len(value) > 4000:
            return value[:4000] + "...<truncated>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (bytes, bytearray)):
            return f"<{type(value).__name__}:{len(value)} bytes>"
        return value

    def _write_compat_trace(
        self,
        *,
        phase: str,
        request_context: Any | None = None,
        payload: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self._compat_trace_enabled(request_context):
            return

        metadata = getattr(request_context, "metadata", None)
        identity = metadata.get("bridge_identity") if isinstance(metadata, dict) else None
        protocol = "anthropic.messages" if ("raw_client" in phase or "client_event" in phase or "response_after_format_chain" in phase) else "openai.responses"
        cached = metadata.pop("_bridge_prepared_metadata", None) if isinstance(metadata, dict) and phase in {"request_prepared_provider", "stream_request_prepared_provider"} else None
        # prepare_provider_request serializes the same immutable prepared body
        # immediately after computing this record. Consume its phase cache once.
        record = dict(cached, phase=phase) if isinstance(cached, dict) else metadata_record(phase, getattr(request_context, "request_id", None), payload, identity, protocol=protocol, diagnostics=extra)
        append_bounded(self._get_compat_trace_path(), record, limit=COMPAT_MAX_LOG_BYTES)

    def _write_usage_metadata(self, phase: str, request_context: Any, payload: Any) -> None:
        configured = os.getenv("CCPROXY_USAGE_METADATA_PATH")
        if not configured:
            return
        metadata = getattr(request_context, "metadata", None)
        identity = metadata.get("bridge_identity") if isinstance(metadata, dict) else None
        if isinstance(payload, dict) and phase == "response":
            event_type = payload.get("type")
            if event_type and event_type not in {"response.completed", "response.failed", "response.incomplete", "error"}:
                return
            if isinstance(metadata, dict):
                if metadata.get("bridge_usage_recorded"):
                    return
                metadata["bridge_usage_recorded"] = True
        record = metadata_record(phase, getattr(request_context, "request_id", None), payload, identity)
        if phase == "request" and isinstance(metadata, dict) and self._compat_trace_enabled(request_context):
            metadata["_bridge_prepared_metadata"] = record
        append_bounded(Path(configured), record)

    # --- Error-log opt-in (CCPROXY_ERROR_LOG=1) -------------------------
    # Writes a single-line JSONL summary of failed requests to a separate
    # file so the user can audit 400/5xx responses without grepping through
    # the giant claude-compat-trace.jsonl. We deliberately avoid recording
    # secrets (full system prompt, tool args) — only structural info.

    def _error_log_enabled(self) -> bool:
        value = (os.getenv("CCPROXY_ERROR_LOG", "") or "").strip().lower()
        return value not in {"", "0", "false", "off", "no"}

    def _get_error_log_path(self) -> Path:
        configured = os.getenv("CCPROXY_ERROR_LOG_PATH")
        if isinstance(configured, str) and configured.strip():
            return Path(configured.strip())
        return Path.home() / ".claude-codex-proxy" / "logs" / "ccproxy-errors.jsonl"

    def _summarize_request_payload(self, payload: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "model": None,
            "stream": None,
            "has_thinking": False,
            "has_tools": False,
            "tool_count": 0,
            "message_count": 0,
            "has_system": False,
            "system_block_count": 0,
        }
        if not isinstance(payload, dict):
            return summary
        try:
            model_value = payload.get("model")
            if isinstance(model_value, str):
                summary["model"] = model_value
            stream_value = payload.get("stream")
            if isinstance(stream_value, bool):
                summary["stream"] = stream_value
            summary["has_thinking"] = "thinking" in payload
            tools = payload.get("tools")
            if isinstance(tools, list):
                summary["has_tools"] = len(tools) > 0
                summary["tool_count"] = len(tools)
            messages = payload.get("messages")
            if isinstance(messages, list):
                summary["message_count"] = len(messages)
            system = payload.get("system")
            if system:
                summary["has_system"] = True
                if isinstance(system, list):
                    summary["system_block_count"] = len(system)
                elif isinstance(system, str):
                    summary["system_block_count"] = 1
        except Exception:
            pass
        return summary

    def _write_error_log(
        self,
        *,
        phase: str,
        status_code: int,
        error: BaseException | str,
        request_context: Any | None = None,
        payload: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self._error_log_enabled():
            return
        metadata = getattr(request_context, "metadata", None)
        identity = metadata.get("bridge_identity") if isinstance(metadata, dict) else None
        record = metadata_record(phase, getattr(request_context, "request_id", None), payload, identity, diagnostics=extra)
        record["status"] = status_code
        record["error_type"] = type(error).__name__ if isinstance(error, BaseException) else "upstream_error"
        # Exceptions, tool arguments, and provider error bodies may echo opaque
        # values. Preserve error class/status only, using the same bounded sink.
        append_bounded(self._get_error_log_path(), record)

    async def _emit_metadata(self, method: str, *args, **kwargs):
        return await offload_metadata(getattr(self, method), *args, **kwargs)

    def _observe_provider_response(self, request_context: Any, payload: Any) -> None:
        metadata = getattr(request_context, "metadata", None)
        if not isinstance(metadata, dict) or not isinstance(payload, dict):
            return
        kind = payload.get("type")
        terminal = kind in {"response.completed", "response.incomplete", "response.failed", "error"}
        compat = self._compat_trace_enabled(request_context)
        usage_path = os.getenv("CCPROXY_USAGE_METADATA_PATH")
        if not compat and not (terminal and usage_path):
            return
        record = metadata_record("stream_response_provider_event", getattr(request_context, "request_id", None),
                                 payload, metadata.get("bridge_identity"))
        record["boundary"] = "provider"
        record["model_role"] = "served"
        record["model_status"] = "measured" if "model" in record else "missing"
        if terminal and "usage" not in record:
            from ccproxy.llms.formatters.usage_contract import normalize_openai_usage
            record["usage"] = normalize_openai_usage(None)
        if compat:
            append_bounded(self._get_compat_trace_path(), record, limit=COMPAT_MAX_LOG_BYTES)
        if terminal and usage_path and not metadata.get("bridge_usage_recorded"):
            # Keep success separate from attempted telemetry while this terminal
            # is in hand. No claim of later-terminal reachability is needed.
            if append_bounded(Path(usage_path), dict(record, phase="response")):
                metadata["bridge_usage_recorded"] = True

    def _get_context_limit_tokens(self) -> tuple[int, str]:
        configured = (os.getenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "") or "").strip()
        if configured:
            with contextlib.suppress(ValueError):
                value = int(configured)
                if value > 0:
                    return value, "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
        return _DEFAULT_CODEX_CONTEXT_LIMIT_TOKENS, "gpt_backend_default"

    def _estimate_context_actual_tokens(
        self,
        request_body: bytes | None,
        limit_tokens: int,
    ) -> int:
        body_text = (request_body or b"").decode("utf-8", errors="replace")
        character_estimate = (
            len(body_text) + _ESTIMATED_CHARACTERS_PER_TOKEN - 1
        ) // _ESTIMATED_CHARACTERS_PER_TOKEN

        # The upstream event does not report actual input tokens. Four UTF-8
        # characters per token is only a rough request-size estimate. When that
        # estimate is below the configured limit, limit + 1 is deliberately
        # synthetic: Claude Code's recovery regex requires actual > limit.
        return max(limit_tokens + 1, character_estimate)

    def _translate_context_length_error_event(
        self,
        event: Any,
        *,
        request_body: bytes | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        if not isinstance(event, dict) or event.get("type") != "error":
            return event, None

        error = event.get("error")
        if not isinstance(error, dict):
            return event, None

        code = str(error.get("code") or "")
        error_type = str(error.get("type") or "")
        original_message = str(error.get("message") or "")
        message_lower = original_message.lower()
        is_context_overflow = code == "context_length_exceeded" or (
            error_type == "invalid_request_error"
            and (
                "context window" in message_lower
                or "maximum context" in message_lower
            )
        )
        if not is_context_overflow:
            return event, None

        limit_tokens, limit_source = self._get_context_limit_tokens()
        actual_tokens = self._estimate_context_actual_tokens(
            request_body,
            limit_tokens,
        )
        upstream_text = original_message or code or "context length exceeded"

        translated_error = dict(error)
        translated_error["type"] = "invalid_request_error"
        translated_error["message"] = (
            f"prompt is too long: {actual_tokens} tokens > "
            f"{limit_tokens} maximum (upstream: {code or error_type}: "
            f"{upstream_text})"
        )
        translated_event = dict(event)
        translated_event["error"] = translated_error
        return translated_event, {
            "upstream_code": code or None,
            "upstream_type": error_type or None,
            "upstream_message": original_message,
            "actual_tokens": actual_tokens,
            "limit_tokens": limit_tokens,
            "limit_source": limit_source,
            "actual_source": "request_utf8_chars_divided_by_4_with_limit_plus_1_floor",
        }

    def _wrap_streaming_response_adapter(
        self,
        adapter: Any | None,
        request_context: Any | None = None,
        request_body: bytes | None = None,
        preflight: _ContextOverflowPreflight | None = None,
    ) -> Any | None:
        if adapter is None:
            class _Passthrough:
                async def convert_stream(self, stream):
                    async for item in stream:
                        yield item
            return _Passthrough()
        if getattr(adapter, "_ccproxy_compat_trace_wrapped", False):
            return adapter
        if not hasattr(adapter, "convert_stream"):
            return adapter

        trace_enabled = self._compat_trace_enabled(request_context)
        if preflight is None and not trace_enabled and not os.getenv("CCPROXY_USAGE_METADATA_PATH"):
            return adapter

        owner = self

        class _CompatTraceStreamingAdapter:
            _ccproxy_compat_trace_wrapped = True

            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def convert_stream(self, stream: Any) -> Any:
                async def traced_input() -> Any:
                    iterator = stream.__aiter__()

                    if preflight is not None:
                        prefetched: list[Any] = []
                        preflight_error: Exception | None = None
                        try:
                            for expected_type in (
                                "response.created",
                                "response.in_progress",
                            ):
                                item = await anext(iterator)
                                prefetched.append(item)
                                if (
                                    not isinstance(item, dict)
                                    or item.get("type") != expected_type
                                ):
                                    break
                            else:
                                # The shared httpx stream already has a finite
                                # read timeout. Do not add a shorter local timeout:
                                # a delayed sequence-2 overflow must still become
                                # HTTP 400 rather than an HTTP 200 SSE error.
                                item = await anext(iterator)
                                prefetched.append(item)
                                translated_item, translation = (
                                    owner._translate_context_length_error_event(
                                        item,
                                        request_body=request_body,
                                    )
                                )
                                if translation is not None:
                                    upstream_error = translation.get(
                                        "upstream_message"
                                    ) or (
                                        translation.get("upstream_code")
                                        or "context length exceeded"
                                    )
                                    logger.warning(
                                        "codex_context_length_error_http_400",
                                        actual_tokens=translation["actual_tokens"],
                                        limit_tokens=translation["limit_tokens"],
                                        limit_source=translation["limit_source"],
                                        category="transform",
                                    )
                                    await owner._emit_metadata("_write_error_log",
                                        phase="stream_context_length_error_http_400",
                                        status_code=400,
                                        error=type(upstream_error).__name__,
                                        request_context=request_context,
                                        extra=translation,
                                    )
                                    await owner._emit_metadata("_write_compat_trace",
                                        phase="stream_context_length_error_http_400",
                                        request_context=request_context,
                                        payload=translated_item,
                                        extra=translation,
                                    )
                                    translated_error = cast(
                                        dict[str, Any], translated_item
                                    )["error"]
                                    preflight.reject(
                                        {
                                            "type": "error",
                                            "error": {
                                                "type": "invalid_request_error",
                                                "message": translated_error["message"],
                                            },
                                        }
                                    )
                                    return
                        except StopAsyncIteration:
                            pass
                        except Exception as exc:
                            # Replay already-read bootstrap events before the
                            # existing streaming error path handles the failure.
                            preflight_error = exc

                        for item in prefetched:
                            yield item
                        if preflight_error is not None:
                            raise preflight_error

                    async for item in iterator:
                        yield item

                async def traced_output() -> Any:
                    async for item in self._inner.convert_stream(traced_input()):
                        if trace_enabled:
                            await owner._emit_metadata("_write_compat_trace",
                                phase="stream_response_client_event",
                                request_context=request_context,
                                payload=item,
                                extra={"adapter_type": type(self._inner).__name__},
                            )
                        yield item

                return traced_output()

        return _CompatTraceStreamingAdapter(adapter)

    async def handle_request(
        self, request: Request
    ) -> Response | StreamingResponse | DeferredStreaming:
        """Handle request with Codex-specific streaming behavior.

        Codex upstream only supports streaming. If the client requests a non-streaming
        response, we internally stream and buffer it, then return a standard Response.
        """
        # Context + request info
        ctx = request.state.context
        self._ensure_tool_accumulator(ctx)
        if self.mock_handler:
            return await MockAdapter(self.mock_handler).handle_request(request)

        endpoint = ctx.metadata.get("endpoint", "")
        body = await request.body()
        body = await self._map_request_model(ctx, body)
        headers = extract_request_headers(request)

        # Determine client streaming intent from body flag (fallback to False)
        wants_stream = self._detect_streaming_intent(body, headers)
        logger.trace(
            "codex_adapter_request_intent",
            wants_stream=wants_stream,
            endpoint=endpoint,
            format_chain=getattr(ctx, "format_chain", []),
            category="streaming",
        )

        # Explicitly set service_type for downstream helpers
        with contextlib.suppress(Exception):
            ctx.metadata.setdefault("service_type", "codex")

        # If client wants streaming, delegate to streaming handler directly
        if wants_stream and self.streaming_handler:
            logger.trace(
                "codex_adapter_delegating_streaming",
                endpoint=endpoint,
                category="streaming",
            )
            return await self.handle_streaming(request, endpoint)

        # Otherwise, buffer the upstream streaming response into a standard one
        if getattr(self.config, "buffer_non_streaming", True):
            # 1) Prepare provider request (adds auth, sets stream=true, etc.)
            # Apply request format conversion if specified
            if ctx.format_chain and len(ctx.format_chain) > 1:
                try:
                    request_payload = self._decode_json_body(
                        body, context="codex_request"
                    )
                    await self._emit_metadata("_write_compat_trace",
                        phase="request_raw_client",
                        request_context=ctx,
                        payload=request_payload,
                    )
                    request_payload = await self._apply_format_chain(
                        data=request_payload,
                        format_chain=ctx.format_chain,
                        stage="request",
                    )
                    await self._emit_metadata("_write_compat_trace",
                        phase="request_after_format_chain",
                        request_context=ctx,
                        payload=request_payload,
                    )
                    body = self._encode_json_body(request_payload)
                except Exception as e:
                    logger.error(
                        "codex_format_chain_request_failed",
                        error=type(e).__name__,
                        exc_info=False,
                        category="transform",
                    )
                    await self._emit_metadata("_write_error_log",
                        phase="non_streaming_format_chain_request",
                        status_code=400,
                        error=e,
                        request_context=ctx,
                        payload=locals().get("request_payload"),
                    )
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Failed to convert request using format chain",
                                "details": str(e),
                            }
                        },
                    )

            prepared_body, prepared_headers = await self.prepare_provider_request(
                body, headers, endpoint, request_context=ctx
            )
            with contextlib.suppress(Exception):
                await self._emit_metadata("_write_compat_trace",
                    phase="request_prepared_provider",
                    request_context=ctx,
                    payload=self._decode_json_body(
                        prepared_body, context="codex_prepared_provider_request"
                    ),
                    extra={"header_keys": sorted(prepared_headers.keys())},
                )
            logger.trace(
                "codex_adapter_prepared_provider_request",
                header_keys=list(prepared_headers.keys()),
                body_size=len(prepared_body or b""),
                category="http",
            )

            # 2) Build handler config using composed adapter from format_chain (unified path)

            composed_adapter = (
                compose_from_chain(
                    registry=self.format_registry, chain=ctx.format_chain
                )
                if self.format_registry and ctx.format_chain
                else None
            )

            handler_config = HandlerConfig(
                supports_streaming=True,
                request_transformer=None,
                response_adapter=composed_adapter,
                format_context=None,
            )

            # 3) Use StreamingBufferService to convert upstream stream -> regular response
            target_url = await self.get_target_url(endpoint)
            # Try to use a client with base_url for better hook integration
            http_client = await self.http_pool_manager.get_client()
            hook_manager = (
                getattr(self.streaming_handler, "hook_manager", None)
                if self.streaming_handler
                else None
            )
            buffer_service = StreamingBufferService(
                http_client=http_client,
                request_tracer=None,
                hook_manager=hook_manager,
                http_pool_manager=self.http_pool_manager,
            )

            buffered_response = await buffer_service.handle_buffered_streaming_request(
                method=request.method,
                url=target_url,
                headers=prepared_headers,
                body=prepared_body,
                handler_config=handler_config,
                request_context=ctx,
                provider_name="codex",
            )
            logger.trace(
                "codex_adapter_buffered_response_ready",
                status_code=buffered_response.status_code,
                buffer_response_bytes=len(buffered_response.body),
                category="streaming",
                format_chain=getattr(ctx, "format_chain", []),
            )

            # 4) Apply reverse format chain on buffered body if needed
            if ctx.format_chain and len(ctx.format_chain) > 1:
                from typing import Literal

                mode: Literal["error", "response"] = (
                    "error" if buffered_response.status_code >= 400 else "response"
                )
                try:
                    body_bytes = (
                        buffered_response.body
                        if isinstance(buffered_response.body, bytes)
                        else bytes(buffered_response.body)
                    )
                    response_payload = self._decode_json_body(
                        body_bytes, context=f"codex_{mode}"
                    )
                    await self._emit_metadata("_write_compat_trace",
                        phase=f"{mode}_raw_provider",
                        request_context=ctx,
                        payload=response_payload,
                        extra={"status_code": buffered_response.status_code},
                    )
                    response_payload = self._normalize_provider_response_payload(
                        response_payload
                    )
                    await self._emit_metadata("_write_compat_trace",
                        phase=f"{mode}_normalized_provider",
                        request_context=ctx,
                        payload=response_payload,
                        extra={"status_code": buffered_response.status_code},
                    )
                    response_payload = await self._apply_format_chain(
                        data=response_payload,
                        format_chain=ctx.format_chain,
                        stage=mode,
                    )
                    await self._emit_metadata("_write_compat_trace",
                        phase=f"{mode}_after_format_chain",
                        request_context=ctx,
                        payload=response_payload,
                        extra={"status_code": buffered_response.status_code},
                    )
                    metadata = getattr(ctx, "metadata", None)
                    alias_map = getattr(ctx, "_model_alias_map", None)
                    if isinstance(metadata, dict):
                        if (
                            isinstance(alias_map, dict)
                            and isinstance(response_payload, dict)
                            and isinstance(response_payload.get("model"), str)
                        ):
                            response_payload["model"] = alias_map.get(
                                response_payload["model"], response_payload["model"]
                            )
                        restore_model_aliases(response_payload, metadata)
                    converted_body = self._encode_json_body(response_payload)
                except Exception as e:
                    logger.error(
                        "codex_format_chain_response_failed",
                        error=type(e).__name__,
                        mode=mode,
                        exc_info=False,
                        category="transform",
                    )
                    await self._emit_metadata("_write_error_log",
                        phase="non_streaming_format_chain_response",
                        status_code=502,
                        error=e,
                        request_context=ctx,
                        extra={"mode": mode},
                    )
                    return JSONResponse(
                        status_code=502,
                        content={
                            "error": {
                                "type": "server_error",
                                "message": "Failed to convert provider response using format chain",
                                "details": type(e).__name__,
                            }
                        },
                    )

                headers_out = filter_response_headers(dict(buffered_response.headers))
                return Response(
                    content=converted_body,
                    status_code=buffered_response.status_code,
                    headers=headers_out,
                    media_type="application/json",
                )

            # No conversion needed; return buffered response as-is
            return buffered_response

        # Fallback: no buffering requested, use base non-streaming flow
        return await super().handle_request(request)

    async def get_target_url(self, endpoint: str) -> str:
        return f"{self.base_url}/responses"

    async def prepare_provider_request(
        self,
        body: bytes,
        headers: dict[str, str],
        endpoint: str,
        request_context: Any | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        filtered_headers = await self.prepare_provider_headers(headers, request_context=request_context)

        if self._request_body_is_encoded(headers):
            return body, filtered_headers

        # Body will be re-serialized as plain JSON; drop stale encoding header
        filtered_headers.pop("content-encoding", None)

        # Parse body (format conversion is now handled by format chain)
        try:
            body_data = json.loads(body.decode()) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "codex_request_body_parse_failed",
                error=type(exc).__name__,
                body_length=len(body) if body else 0,
                category="validation",
            )
            body_data = {}
        apply_detection_payload = self._should_apply_detection_payload(
            request_context=request_context
        )
        if apply_detection_payload:
            body_data = self._apply_request_template(body_data)
        else:
            body_data = self._normalize_input_messages(body_data)

        # Claude-native requests should skip Codex tool/template injection, but the
        # backend still requires a non-empty instructions field.
        detected_instructions = self._get_instructions()

        existing_instructions = body_data.get("instructions")
        if isinstance(existing_instructions, str) and existing_instructions:
            instructions = (
                detected_instructions + "\n" + existing_instructions
                if detected_instructions
                else existing_instructions
            )
        else:
            instructions = detected_instructions

        if instructions:
            body_data["instructions"] = instructions
        else:
            body_data.pop("instructions", None)

        body_data = self._sanitize_provider_body(body_data)
        from .replay_state import prepare_replay
        body_data = prepare_replay(body_data, filtered_headers, request_context, enabled=self._is_claude_native_request(request_context))
        metadata = getattr(request_context, "metadata", None)
        if isinstance(metadata, dict):
            metadata["bridge_identity"] = {
                "session_hash": digest(filtered_headers["session-id"]),
                "session_source": metadata.get("bridge_session_source"),
                "principal_hash": digest(filtered_headers.get("chatgpt-account-id") or filtered_headers.get("authorization")),
            }
            trace_ids = [value for key, value in headers.items() if key.lower() == "x-ccproxy-trace-id"]
            if len(trace_ids) == 1 and isinstance(trace_ids[0], str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", trace_ids[0]):
                metadata["bridge_identity"]["trace_id_hash"] = digest(trace_ids[0])
        if isinstance(metadata, dict) and (self._compat_trace_enabled(request_context) or os.getenv("CCPROXY_USAGE_METADATA_PATH")):
            async def observer(event):
                await self._emit_metadata("_observe_provider_response", request_context, event)
            metadata["_bridge_provider_observer"] = observer
        await self._emit_metadata("_write_usage_metadata", "request", request_context, body_data)
        return json.dumps(body_data).encode(), filtered_headers

    def _sanitize_provider_body(self, body_data: dict[str, Any]) -> dict[str, Any]:
        """Apply Codex-specific payload sanitization shared by all request paths."""

        # Codex backend requires stream=true, always override
        body_data["stream"] = True
        body_data["store"] = False

        # Remove unsupported keys for Codex
        _unsupported_keys = (
            "max_output_tokens",
            "max_completion_tokens",
            "max_tokens",
            "metadata",
            "service_tier",
            "temperature",
        )
        removed_keys = [k for k in _unsupported_keys if k in body_data]
        for key in _unsupported_keys:
            body_data.pop(key, None)
        if removed_keys:
            logger.debug(
                "codex_sanitize_removed_fields",
                removed=removed_keys,
                category="request",
            )

        list_input = body_data.get("input", [])
        # Remove any input types that Codex does not support
        body_data["input"] = [
            input for input in list_input if input.get("type") != "item_reference"
        ]

        # Remove any prefixed metadata fields that shouldn't be sent to the API
        body_data = self._remove_metadata_fields(body_data)

        return body_data

    def _normalize_provider_response_payload(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Normalize Codex Responses payloads before reverse format conversion."""

        if not isinstance(payload, dict):
            return payload

        normalized = dict(payload)
        nested_response = normalized.get("response")
        if "output" not in normalized and isinstance(nested_response, dict):
            normalized = dict(nested_response)

        if isinstance(normalized.get("output"), dict):
            normalized["output"] = [normalized["output"]]

        if "output" not in normalized:
            normalized["output"] = self._synthesize_response_output(normalized)

        normalized.setdefault("object", "response")
        normalized["created_at"] = int(normalized.get("created_at") or 0)
        normalized["status"] = str(normalized.get("status") or "completed")
        normalized["model"] = str(normalized.get("model") or "")
        normalized["parallel_tool_calls"] = bool(
            normalized.get("parallel_tool_calls", False)
        )

        return normalized

    def _synthesize_response_output(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Build minimal Responses output items from legacy/plain Codex payloads."""

        output_items: list[dict[str, Any]] = []

        tool_calls = payload.get("tool_calls")
        if isinstance(tool_calls, list):
            for index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue

                function_payload = tool_call.get("function")
                if not isinstance(function_payload, dict):
                    function_payload = {}

                call_id = tool_call.get("call_id") or tool_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call_{index}"

                item_id = tool_call.get("id")
                if not isinstance(item_id, str) or not item_id:
                    item_id = call_id

                arguments = (
                    function_payload.get("arguments")
                    or tool_call.get("arguments")
                    or "{}"
                )

                output_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "status": "completed",
                        "name": function_payload.get("name")
                        or tool_call.get("name")
                        or "function",
                        "call_id": call_id,
                        "arguments": arguments,
                    }
                )

        text_value = self._extract_response_text(payload)
        if isinstance(text_value, str) and text_value:
            output_items.append(
                {
                    "type": "message",
                    "id": str(payload.get("id") or "msg_0"),
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text_value}],
                }
            )

        if output_items:
            return output_items

        return [
            {
                "type": "message",
                "id": str(payload.get("id") or "msg_0"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ""}],
            }
        ]

    def _extract_response_text(self, payload: dict[str, Any]) -> str | None:
        """Extract assistant text from Codex response payload variants."""

        text_value = payload.get("text")
        if isinstance(text_value, str):
            return text_value

        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text

        content_value = payload.get("content")
        if isinstance(content_value, str):
            return content_value

        if isinstance(content_value, list):
            parts: list[str] = []
            for part in content_value:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            if parts:
                return "".join(parts)

        return None

    @staticmethod
    def _token_account_id(token_value: str) -> str | None:
        """chatgpt_account_id claim of the access token itself, if present."""
        try:
            import jwt

            claims = jwt.decode(token_value, options={"verify_signature": False})
        except Exception:
            return None
        auth = claims.get("https://api.openai.com/auth")
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        return account if isinstance(account, str) and account else None

    async def _account_for_token(self, token_value: str) -> str | None:
        """chatgpt-account-id that belongs to the token being sent.

        The token manager keeps its profile until a token refresh, but the
        token itself is re-read from ~/.codex/auth.json. When another tool
        (mycmux) swaps that file to a different account, the cached profile
        would send the old chatgpt-account-id with the new token, and upstream
        answers 401 token_expired. The account named inside the token wins, so
        a credentials re-read between token and profile cannot split them.
        """
        manager = self.token_manager
        profile = await manager.get_profile_quick()
        cached = getattr(profile, "chatgpt_account_id", None) if profile else None
        token_account = self._token_account_id(token_value)
        if token_account is None:
            return cached
        if cached is not None and cached != token_account and hasattr(manager, "_profile_cache"):
            logger.warning(
                "codex_profile_cache_account_changed",
                cached=hashlib.sha256(str(cached).encode()).hexdigest()[:8] if cached else None,
                token=hashlib.sha256(token_account.encode()).hexdigest()[:8],
                category="auth",
            )
            manager._profile_cache = None
            await manager.get_profile_quick()
        return token_account

    async def prepare_provider_headers(self, headers: dict[str, str], request_context: Any = None) -> dict[str, str]:
        try:
            canonical_session(headers, "validate-only")
        except ValueError:
            raise HTTPException(status_code=400, detail={"type": "invalid_request_error", "message": "Invalid session identity metadata"}) from None
        token_value = await self._resolve_access_token()

        chatgpt_account_id = await self._account_for_token(token_value)

        filtered_headers = filter_request_headers(headers, preserve_auth=False)
        content_encoding = headers.get("content-encoding")
        if content_encoding:
            filtered_headers["content-encoding"] = content_encoding

        principal = str(chatgpt_account_id) if chatgpt_account_id else "token:" + hashlib.sha256(token_value.encode()).hexdigest()
        session_id, session_source = canonical_session(headers, principal)
        metadata = getattr(request_context, "metadata", None)
        if isinstance(metadata, dict):
            metadata["bridge_session_source"] = session_source
        # Underscore aliases and captured CLI IDs must not override this request.
        for key in list(filtered_headers):
            if key.lower() in _ID_EXCLUSIONS:
                filtered_headers.pop(key)

        base_headers = {
            "authorization": f"Bearer {token_value}",
            "content-type": "application/json",
            "session-id": session_id,
        }

        if chatgpt_account_id is not None:
            base_headers["chatgpt-account-id"] = chatgpt_account_id

        filtered_headers.update(base_headers)

        cli_headers = self._collect_cli_headers()
        for key, value in cli_headers.items():
            if key.lower() not in _ID_EXCLUSIONS:
                filtered_headers.setdefault(key, value)

        return filtered_headers

    async def process_provider_response(
        self, response: httpx.Response, endpoint: str
    ) -> Response | StreamingResponse:
        """Return a plain Response; streaming handled upstream by BaseHTTPAdapter.

        The BaseHTTPAdapter is responsible for detecting streaming and delegating
        to the shared StreamingHandler. For non-streaming responses, adapters
        should return a simple Starlette Response.
        """
        response_headers = extract_response_headers(response)
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type"),
        )

    async def _resolve_access_token(self) -> str:
        """Resolve an access token suitable for Codex requests.

        If the auth manager/credential balancer is not configured, raise a
        unified AuthenticationError so middleware can return a clean 401
        without leaking stack traces.
        """

        # Guard: token manager must be configured via plugin auth_manager
        if not getattr(self, "token_manager", None):
            from ccproxy.core.errors import AuthenticationError

            logger.warning(
                "auth_manager_override_not_resolved",
                plugin="codex",
                auth_manager_name="codex_credential_balancer",
                category="auth",
            )
            raise AuthenticationError(
                "Authentication manager not configured for Codex provider"
            )

        token_manager = self.token_manager

        async def _snapshot_token() -> str | None:
            snapshot = await token_manager.get_token_snapshot()
            if snapshot and snapshot.access_token:
                return snapshot.access_token
            return None

        # Strategy 1: refresh credentials if stale
        credentials = await token_manager.load_credentials()
        if credentials and token_manager.should_refresh(credentials):
            try:
                refreshed = await token_manager.get_access_token_with_refresh()
                if refreshed:
                    return refreshed
            except OAuthTokenRefreshError as exc:
                logger.warning(
                    "codex_token_strategy_failed",
                    strategy="refresh_stale_credentials",
                    error=type(exc).__name__,
                    category="auth",
                )
                fallback = await _snapshot_token()
                if fallback:
                    return fallback

        # Strategy 2: get cached/fresh access token
        token = None
        try:
            token = await token_manager.get_access_token()
        except OAuthTokenRefreshError as exc:
            logger.warning(
                "codex_token_strategy_failed",
                strategy="get_access_token",
                error=type(exc).__name__,
                category="auth",
            )
            fallback = await _snapshot_token()
            if fallback:
                return fallback

        if token:
            return token

        # Strategy 3: force refresh
        try:
            refreshed = await token_manager.get_access_token_with_refresh()
            if refreshed:
                return refreshed
        except OAuthTokenRefreshError as exc:
            logger.warning(
                "codex_token_strategy_failed",
                strategy="force_refresh",
                error=type(exc).__name__,
                category="auth",
            )
            fallback = await _snapshot_token()
            if fallback:
                return fallback

        # Strategy 4: final snapshot fallback
        fallback = await _snapshot_token()
        if fallback:
            return fallback

        from ccproxy.core.errors import AuthenticationError

        logger.warning(
            "codex_all_token_strategies_exhausted",
            plugin="codex",
            category="auth",
        )
        raise AuthenticationError(
            "No authentication credentials available — all token strategies exhausted"
        )

    def _collect_cli_headers(self) -> dict[str, str]:
        """Collect safe CLI headers from detection cache for forwarding."""

        if not self.detection_service:
            return {}

        headers_data = self.detection_service.get_detected_headers()
        if not headers_data:
            return {}

        ignores = {
            header.lower() for header in self.detection_service.get_ignored_headers()
        }
        redacted = {
            header.lower() for header in self.detection_service.get_redacted_headers()
        }

        return headers_data.filtered(ignores=ignores, redacted=redacted)

    async def handle_streaming(
        self, request: Request, endpoint: str, **kwargs: Any
    ) -> Response | StreamingResponse | DeferredStreaming:
        """Handle streaming with request conversion for Codex.

        Applies request format conversion (e.g., anthropic.messages -> openai.responses) before
        preparing the provider request, then delegates to StreamingHandler with
        a streaming response adapter for reverse conversion as needed.
        """
        if not self.streaming_handler:
            # Fallback to base behavior
            return await super().handle_streaming(request, endpoint, **kwargs)
        if self.mock_handler:
            return await MockAdapter(self.mock_handler).handle_streaming(
                request, endpoint, **kwargs
            )

        # Get context
        ctx = request.state.context
        self._ensure_tool_accumulator(ctx)

        # Extract body and headers
        body = await request.body()
        body = await self._map_request_model(ctx, body)
        headers = extract_request_headers(request)

        # Ensure format adapters are available when required
        self._ensure_format_registry(ctx.format_chain, endpoint)

        # Apply request format conversion if a chain is defined
        if ctx.format_chain and len(ctx.format_chain) > 1:
            try:
                request_payload = self._decode_json_body(
                    body, context="codex_stream_request"
                )
                await self._emit_metadata("_write_compat_trace",
                    phase="stream_request_raw_client",
                    request_context=ctx,
                    payload=request_payload,
                )
                request_payload = await self._apply_format_chain(
                    data=request_payload,
                    format_chain=ctx.format_chain,
                    stage="request",
                )
                await self._emit_metadata("_write_compat_trace",
                    phase="stream_request_after_format_chain",
                    request_context=ctx,
                    payload=request_payload,
                )
                self._record_tool_definitions(ctx, request_payload)
                body = self._encode_json_body(request_payload)
            except Exception as e:
                logger.error(
                    "codex_format_chain_request_failed",
                    error=type(e).__name__,
                    exc_info=False,
                    category="transform",
                )
                await self._emit_metadata("_write_error_log",
                    phase="streaming_format_chain_request",
                    status_code=400,
                    error=e,
                    request_context=ctx,
                    payload=locals().get("request_payload"),
                )
                # Convert error to streaming response

                error_content = {
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Failed to convert request using format chain",
                        "details": str(e),
                    }
                }
                error_bytes = json.dumps(error_content).encode("utf-8")

                async def error_generator() -> (
                    Any
                ):  # AsyncGenerator[bytes, None] would be more specific
                    yield error_bytes

                return StreamingResponse(
                    content=error_generator(),
                    status_code=400,
                    media_type="application/json",
                )

        # Provider-specific preparation (adds auth, sets stream=true)
        prepared_body, prepared_headers = await self.prepare_provider_request(
            body, headers, endpoint, request_context=ctx
        )
        with contextlib.suppress(Exception):
            await self._emit_metadata("_write_compat_trace",
                phase="stream_request_prepared_provider",
                request_context=ctx,
                payload=self._decode_json_body(
                    prepared_body, context="codex_stream_prepared_provider_request"
                ),
                extra={"header_keys": sorted(prepared_headers.keys())},
            )

        # Get format adapter for streaming reverse conversion
        streaming_format_adapter = None
        if ctx.format_chain and len(ctx.format_chain) > 1 and self.format_registry:
            from_format = ctx.format_chain[-1]
            to_format = ctx.format_chain[0]
            try:
                streaming_format_adapter = self.format_registry.get_if_exists(
                    from_format, to_format
                )
            except Exception:
                streaming_format_adapter = None
        preflight = (
            _ContextOverflowPreflight()
            if streaming_format_adapter is not None
            and self._is_claude_native_request(ctx)
            else None
        )
        streaming_format_adapter = self._wrap_streaming_response_adapter(
            streaming_format_adapter,
            request_context=ctx,
            request_body=prepared_body,
            preflight=preflight,
        )

        handler_config = HandlerConfig(
            supports_streaming=True,
            request_transformer=None,
            response_adapter=streaming_format_adapter,
            format_context=None,
        )

        target_url = await self.get_target_url(endpoint)

        parsed_url = urlparse(target_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

        response = await self.streaming_handler.handle_streaming_request(
            method=request.method,
            url=target_url,
            headers=prepared_headers,
            body=prepared_body,
            handler_config=handler_config,
            request_context=ctx,
            client=await self.http_pool_manager.get_client(base_url=base_url),
        )
        if preflight is not None:
            return cast(
                Response | StreamingResponse | DeferredStreaming,
                _ContextOverflowResponseGate(response, preflight),
            )
        return response

    # Helper methods
    def _remove_metadata_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        """Remove fields that start with '_' as they are internal metadata.

        Args:
            data: Dictionary that may contain metadata fields

        Returns:
            Cleaned dictionary without metadata fields
        """
        if not isinstance(data, dict):
            return data

        # Create a new dict without keys starting with '_'
        cleaned_data: dict[str, Any] = {}
        for key, value in data.items():
            if not key.startswith("_"):
                # Recursively clean nested dictionaries
                if isinstance(value, dict):
                    cleaned_data[key] = self._remove_metadata_fields(value)
                elif isinstance(value, list):
                    # Clean list items if they are dictionaries
                    cleaned_items: list[Any] = []
                    for item in value:
                        if isinstance(item, dict):
                            cleaned_items.append(self._remove_metadata_fields(item))
                        else:
                            cleaned_items.append(item)
                    cleaned_data[key] = cleaned_items
                else:
                    cleaned_data[key] = value

        return cleaned_data

    def _apply_request_template(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return data

        template = self._get_request_template()
        if not template:
            return self._normalize_input_messages(data)

        merged = copy.deepcopy(data)

        for key in ("include", "parallel_tool_calls", "reasoning", "tool_choice"):
            if key not in merged and key in template:
                merged[key] = copy.deepcopy(template[key])

        if not merged.get("tools") and isinstance(template.get("tools"), list):
            merged["tools"] = copy.deepcopy(template["tools"])

        if "prompt_cache_key" not in merged:
            prompt_cache_key = template.get("prompt_cache_key")
            if isinstance(prompt_cache_key, str) and prompt_cache_key:
                merged["prompt_cache_key"] = str(uuid.uuid4())

        return self._normalize_input_messages(merged)

    def _normalize_input_messages(self, data: dict[str, Any]) -> dict[str, Any]:
        input_items = data.get("input")
        if not isinstance(input_items, list):
            return data

        normalized_items: list[Any] = []
        for item in input_items:
            if (
                isinstance(item, dict)
                and "type" not in item
                and "role" in item
                and "content" in item
            ):
                normalized_item = dict(item)
                normalized_item["type"] = "message"
                normalized_items.append(normalized_item)
                continue

            normalized_items.append(item)

        result = dict(data)
        result["input"] = normalized_items
        return result

    def _request_body_is_encoded(self, headers: dict[str, str]) -> bool:
        encoding = headers.get("content-encoding", "").strip().lower()
        return bool(encoding and encoding != "identity")

    def _detect_streaming_intent(self, body: bytes, headers: dict[str, str]) -> bool:
        if self._request_body_is_encoded(headers):
            accept = headers.get("accept", "").lower()
            return "text/event-stream" in accept

        try:
            data = json.loads(body.decode()) if body else {}
            return bool(data.get("stream", False))
        except Exception:
            accept = headers.get("accept", "").lower()
            return "text/event-stream" in accept

    def _should_apply_detection_payload(
        self, request_context: Any | None = None
    ) -> bool:
        if self._is_claude_native_request(request_context):
            return False
        return bool(getattr(self.config, "inject_detection_payload", True))

    def _is_claude_native_request(self, request_context: Any | None = None) -> bool:
        if request_context is None:
            return False

        format_chain = getattr(request_context, "format_chain", None)
        if isinstance(format_chain, list) and format_chain:
            first_format = format_chain[0]
            if isinstance(first_format, str) and first_format == "anthropic.messages":
                return True

        metadata = getattr(request_context, "metadata", None)
        if isinstance(metadata, dict):
            endpoint = metadata.get("endpoint")
            if isinstance(endpoint, str) and endpoint.rstrip("/").endswith("/v1/messages"):
                return True

        return False

    def _get_instructions(self) -> str:
        if not self.detection_service:
            return ""

        prompts = self.detection_service.get_detected_prompts()
        if prompts.has_instructions():
            return prompts.instructions or ""

        injection = self.detection_service.get_system_prompt()
        if isinstance(injection, dict):
            instructions = injection.get("instructions")
            if isinstance(instructions, str):
                return instructions

        fallback = getattr(self.detection_service, "instructions_value", None)
        if isinstance(fallback, str):
            return fallback

        return ""

    def _get_request_template(self) -> dict[str, Any]:
        if not self.detection_service:
            return {}

        prompts = self.detection_service.get_detected_prompts()
        if isinstance(prompts.raw, dict) and prompts.raw:
            return prompts.raw

        return {}

    def adapt_error(self, error_body: dict[str, Any]) -> dict[str, Any]:
        """Convert Codex error format to appropriate API error format.

        Args:
            error_body: Codex error response

        Returns:
            API-formatted error response
        """
        # Handle errors with a "detail" field (Codex provider format)
        if isinstance(error_body, dict) and "detail" in error_body:
            detail = error_body["detail"]
            return {
                "error": {
                    "type": "invalid_request_error",
                    "message": str(detail) if isinstance(detail, str) else "Request rejected by provider",
                }
            }

        # Handle other error formats that might have "error" key
        if isinstance(error_body, dict) and "error" in error_body:
            return error_body

        # Default: wrap non-standard errors
        return {
            "error": {
                "type": "internal_server_error",
                "message": "An error occurred processing the request",
            }
        }
