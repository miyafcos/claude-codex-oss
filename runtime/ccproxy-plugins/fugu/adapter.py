"""Sakana Fugu HTTP adapter.

BaseAdapter that fulfils an Anthropic /v1/messages request by converting it
to an OpenAI chat-completions request against the Fugu API and converting the
response back to Anthropic shape. Auth is Bearer ${FUGU_API_KEY}.

Fugu is internally multi-agent — a single request can drive several models
behind the scenes — so per-call token usage runs hotter than a single-model
provider. The adapter does not stream from upstream; for ``stream=true``
requests it fetches the full response then re-emits it as a minimal synthetic
Anthropic SSE stream (same pattern as gemini_cli). Tool use is supported via
OpenAI ``tools`` / ``tool_calls`` round-tripping, but not streamed
incrementally.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ccproxy.core.logging import get_plugin_logger
from ccproxy.services.adapters.base import BaseAdapter


logger = get_plugin_logger()

_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+")
_ALLOWED_MODEL_PREFIX = "fugu"
_LOG_BODY_EXCERPT_LIMIT = 300
# The dispatcher parses this embedded JSON to detect usage_limit_reached.
# The observed quota body is about 250 characters, so keep ample headroom.
_ERROR_BODY_LIMIT = 2000


class FuguAdapter(BaseAdapter):
    def __init__(self, config: Any, **kwargs: Any) -> None:
        super().__init__(config=config, **kwargs)

    # ---- BaseAdapter required methods ----

    async def handle_request(
        self, request: Request
    ) -> Response | StreamingResponse:
        data = await self._read_body(request)
        model = self._resolve_model(str(data.get("model") or ""))
        wants_stream = bool(data.get("stream"))

        api_key = self._get_api_key()
        if not api_key:
            logger.error("fugu_api_key_missing")
            return self._error_response(
                500,
                "fugu: FUGU_API_KEY env var not set on proxy host",
            )

        payload = self._anthropic_to_openai(data, model)
        try:
            openai_resp = await self._post_fugu(payload, api_key)
        except httpx.TimeoutException:
            return self._error_response(504, "fugu: upstream request timed out")
        except httpx.HTTPStatusError as e:  # noqa: BLE001
            upstream_body = e.response.text or ""
            body_excerpt = upstream_body[:_LOG_BODY_EXCERPT_LIMIT]
            embedded_body = upstream_body[:_ERROR_BODY_LIMIT]
            logger.error(
                "fugu_upstream_error",
                status=e.response.status_code,
                body_excerpt=body_excerpt,
            )
            return self._error_response(
                502,
                f"fugu upstream {e.response.status_code}: "
                f"{embedded_body or '(no body)'}",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("fugu_request_failed", error=str(e))
            return self._error_response(502, f"fugu adapter error: {e}")

        msg = self._openai_to_anthropic(openai_resp, model)
        if wants_stream:
            return StreamingResponse(
                self._sse(msg), media_type="text/event-stream"
            )
        return Response(content=json.dumps(msg), media_type="application/json")

    async def handle_streaming(
        self, request: Request, endpoint: str, **kwargs: Any
    ) -> Response | StreamingResponse:
        result = await self.handle_request(request)
        if isinstance(result, StreamingResponse):
            return result
        if result.status_code >= 400:
            return result
        try:
            msg = json.loads(bytes(result.body).decode("utf-8"))
        except Exception:  # noqa: BLE001
            msg = self._to_anthropic_skeleton("", self._default_model())
        return StreamingResponse(self._sse(msg), media_type="text/event-stream")

    async def cleanup(self) -> None:
        return None

    # ---- helpers ----

    async def _read_body(self, request: Request) -> dict[str, Any]:
        body = await request.body()
        if not body:
            return {}
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _default_model(self) -> str:
        return getattr(self.config, "default_model", "fugu-ultra")

    def _get_api_key(self) -> str:
        return str(getattr(self.config, "api_key", "") or "")

    def _base_url(self) -> str:
        return str(
            getattr(self.config, "base_url", "https://api.sakana.ai/v1")
        ).rstrip("/")

    def _timeout(self) -> float:
        return float(getattr(self.config, "timeout_seconds", 300) or 300)

    def _resolve_model(self, model: str) -> str:
        m = (model or "").strip()
        if not m or not m.lower().startswith(_ALLOWED_MODEL_PREFIX):
            return self._default_model()
        if not _MODEL_RE.fullmatch(m):
            return self._default_model()
        return m

    # ---- anthropic -> openai chat completions ----

    def _anthropic_to_openai(
        self, data: dict[str, Any], model: str
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []

        system = data.get("system")
        sys_text = self._extract_system_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

        for msg in data.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            converted = self._convert_message(role, content)
            messages.extend(converted)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }

        # Optional params — pass through what OpenAI chat completions accepts.
        if isinstance(data.get("max_tokens"), int):
            payload["max_tokens"] = max(
                data["max_tokens"],
                int(getattr(self.config, "minimum_max_tokens", 512) or 512),
            )
        if isinstance(data.get("temperature"), (int, float)):
            payload["temperature"] = float(data["temperature"])
        if isinstance(data.get("top_p"), (int, float)):
            payload["top_p"] = float(data["top_p"])
        stop = data.get("stop_sequences")
        if isinstance(stop, list) and stop:
            payload["stop"] = stop

        tools = self._convert_tools(data.get("tools"))
        if tools:
            payload["tools"] = tools
        tool_choice = self._convert_tool_choice(data.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        return payload

    def _extract_system_text(self, system: Any) -> str:
        if isinstance(system, str):
            return system.strip()
        if isinstance(system, list):
            parts = [
                b.get("text", "")
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p).strip()
        return ""

    def _convert_message(
        self, role: Any, content: Any
    ) -> list[dict[str, Any]]:
        """Convert one Anthropic message into one or more OpenAI messages.

        tool_use blocks on an assistant turn become tool_calls on the same
        message. tool_result blocks on a user turn become separate role=tool
        messages (OpenAI's wire), one per tool_use_id.
        """
        if role == "assistant":
            return [self._convert_assistant(content)]
        # role == "user" (or anything else — treat as user)
        return self._convert_user(content)

    def _convert_assistant(self, content: Any) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(b.get("id") or f"call_{uuid.uuid4().hex}"),
                            "type": "function",
                            "function": {
                                "name": str(b.get("name") or ""),
                                "arguments": json.dumps(
                                    b.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
        msg: dict[str, Any] = {"role": "assistant"}
        joined = "\n".join(p for p in text_parts if p)
        # OpenAI requires content to be non-null when no tool_calls; with
        # tool_calls, content can be null but we keep "" for compatibility.
        msg["content"] = joined if joined else ("" if tool_calls else "")
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return msg

    def _convert_user(self, content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"role": "user", "content": content}]
        if not isinstance(content, list):
            return [{"role": "user", "content": ""}]

        result: list[dict[str, Any]] = []
        # Text + image blocks aggregate into a single user message;
        # tool_result blocks split off as separate role=tool messages.
        user_blocks: list[Any] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                user_blocks.append({"type": "text", "text": b.get("text", "")})
            elif t == "image":
                user_blocks.append(self._image_block_to_openai(b))
            elif t == "tool_result":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(b.get("tool_use_id") or ""),
                        "content": self._tool_result_to_text(b.get("content")),
                    }
                )
        if user_blocks:
            # If only text blocks, collapse to plain string content.
            if all(blk.get("type") == "text" for blk in user_blocks):
                joined = "\n".join(blk.get("text", "") for blk in user_blocks)
                result.append({"role": "user", "content": joined})
            else:
                result.append({"role": "user", "content": user_blocks})
        if not result:
            result.append({"role": "user", "content": ""})
        return result

    def _image_block_to_openai(self, block: dict[str, Any]) -> dict[str, Any]:
        source = block.get("source") or {}
        stype = source.get("type")
        if stype == "base64":
            mt = source.get("media_type") or "image/png"
            data = source.get("data") or ""
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mt};base64,{data}"},
            }
        if stype == "url":
            return {
                "type": "image_url",
                "image_url": {"url": source.get("url") or ""},
            }
        return {"type": "text", "text": "[image omitted]"}

    def _tool_result_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
            return "\n".join(parts)
        return ""

    def _convert_tools(self, tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        out: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            if not isinstance(name, str) or not name:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description") or "",
                        "parameters": t.get("input_schema") or {"type": "object"},
                    },
                }
            )
        return out

    def _convert_tool_choice(self, choice: Any) -> Any:
        if not isinstance(choice, dict):
            return None
        ct = choice.get("type")
        if ct == "auto":
            return "auto"
        if ct == "any":
            return "required"
        if ct == "tool":
            name = choice.get("name")
            if isinstance(name, str) and name:
                return {"type": "function", "function": {"name": name}}
        return None

    # ---- upstream call ----

    async def _post_fugu(
        self, payload: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        url = f"{self._base_url()}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    # ---- openai chat completion -> anthropic message ----

    def _openai_to_anthropic(
        self, resp: dict[str, Any], requested_model: str
    ) -> dict[str, Any]:
        choices = resp.get("choices") or []
        choice0 = choices[0] if choices else {}
        msg = choice0.get("message") or {}

        content_blocks: list[dict[str, Any]] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            content_blocks.append({"type": "text", "text": text})
        elif isinstance(text, list):
            for b in text:
                if isinstance(b, dict) and b.get("type") == "text":
                    content_blocks.append(
                        {"type": "text", "text": b.get("text", "")}
                    )

        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:  # noqa: BLE001
                args = {"_raw": fn.get("arguments") or ""}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex}"),
                    "name": str(fn.get("name") or ""),
                    "input": args,
                }
            )

        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        finish = choice0.get("finish_reason") or "stop"
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "stop_sequence",
        }.get(finish, "end_turn")

        usage = resp.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)

        return {
            "id": str(resp.get("id") or f"msg_{uuid.uuid4().hex}"),
            "type": "message",
            "role": "assistant",
            "model": str(resp.get("model") or requested_model),
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }

    def _to_anthropic_skeleton(self, text: str, model: str) -> dict[str, Any]:
        return {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    # ---- synthetic Anthropic SSE (non-streaming upstream) ----

    def _sse(self, msg: dict[str, Any]) -> Iterator[bytes]:
        text_blocks: list[str] = []
        tool_blocks: list[dict[str, Any]] = []
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text_blocks.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                tool_blocks.append(b)
        model = msg.get("model", "")
        mid = msg.get("id", f"msg_{uuid.uuid4().hex}")
        stop_reason = msg.get("stop_reason") or "end_turn"
        usage = msg.get("usage") or {}

        def ev(event: str, data: dict[str, Any]) -> bytes:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")

        yield ev(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": mid,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": 0,
                    },
                },
            },
        )

        idx = 0
        joined_text = "".join(text_blocks)
        if joined_text or not tool_blocks:
            yield ev(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            if joined_text:
                yield ev(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": joined_text},
                    },
                )
            yield ev(
                "content_block_stop",
                {"type": "content_block_stop", "index": idx},
            )
            idx += 1

        for tu in tool_blocks:
            yield ev(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tu.get("id", ""),
                        "name": tu.get("name", ""),
                        "input": {},
                    },
                },
            )
            yield ev(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            tu.get("input") or {}, ensure_ascii=False
                        ),
                    },
                },
            )
            yield ev(
                "content_block_stop",
                {"type": "content_block_stop", "index": idx},
            )
            idx += 1

        yield ev(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                },
                "usage": {
                    "output_tokens": int(usage.get("output_tokens") or 0),
                },
            },
        )
        yield ev("message_stop", {"type": "message_stop"})

    def _error_response(self, status: int, message: str) -> Response:
        payload = {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        }
        return Response(
            content=json.dumps(payload),
            status_code=status,
            media_type="application/json",
        )
