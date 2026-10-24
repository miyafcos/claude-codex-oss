"""Gemini CLI wrapper adapter.

BaseAdapter (no HTTP, no auth) that fulfils an Anthropic /v1/messages request by
shelling out to the gemini CLI (which holds its own cached OAuth — no API key).
The prompt is passed on stdin (avoids cmdline escaping/length limits); only the
trusted cli_command/model reach the shell, and the model is strictly validated.

gemini is one-shot text, so tool_use loops are NOT supported: any tools in the
request are ignored and the model is told to answer with plain text. The result
is returned as an Anthropic message, or wrapped in a minimal synthetic SSE
stream when the client asked for streaming.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ccproxy.core.logging import get_plugin_logger
from ccproxy.services.adapters.base import BaseAdapter


logger = get_plugin_logger()

_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+")


class GeminiCliAdapter(BaseAdapter):
    def __init__(self, config: Any, **kwargs: Any) -> None:
        super().__init__(config=config, **kwargs)

    # ---- BaseAdapter required methods ----

    async def handle_request(
        self, request: Request
    ) -> Response | StreamingResponse:
        data = await self._read_body(request)
        model = self._resolve_model(str(data.get("model") or ""))
        wants_stream = bool(data.get("stream"))
        prompt = self._anthropic_to_prompt(data)
        try:
            text = await self._run_gemini(prompt, model)
        except subprocess.TimeoutExpired:
            return self._error_response(504, "gemini CLI timed out")
        except Exception as e:  # noqa: BLE001
            logger.error("gemini_cli_failed", error=str(e))
            return self._error_response(502, f"gemini CLI error: {e}")
        msg = self._to_anthropic_message(text, model)
        if wants_stream:
            return StreamingResponse(
                self._sse(msg), media_type="text/event-stream"
            )
        return Response(content=json.dumps(msg), media_type="application/json")

    async def handle_streaming(
        self, request: Request, endpoint: str, **kwargs: Any
    ) -> StreamingResponse:
        result = await self.handle_request(request)
        if isinstance(result, StreamingResponse):
            return result
        try:
            msg = json.loads(bytes(result.body).decode("utf-8"))
        except Exception:  # noqa: BLE001
            msg = self._to_anthropic_message("", self._default_model())
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
        return getattr(self.config, "default_model", "gemini-3.5-flash")

    def _resolve_model(self, model: str) -> str:
        m = (model or "").strip()
        # Strict allowlist: only gemini-* with safe chars reach the shell.
        if not m or not m.lower().startswith("gemini") or not _MODEL_RE.fullmatch(m):
            return self._default_model()
        return m

    def _anthropic_to_prompt(self, data: dict[str, Any]) -> str:
        parts: list[str] = []
        system = data.get("system")
        if isinstance(system, str) and system.strip():
            parts.append(f"# System\n{system.strip()}")
        elif isinstance(system, list):
            sys_txt = "\n".join(
                b.get("text", "")
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if sys_txt:
                parts.append(f"# System\n{sys_txt}")
        if data.get("tools"):
            parts.append(
                "# Note\nTools are NOT available here. Answer directly with your "
                "final text response (do not attempt tool calls)."
            )
        for msg in data.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            label = "User" if msg.get("role") == "user" else "Assistant"
            text = self._extract_text(msg.get("content"))
            if text.strip():
                parts.append(f"## {label}\n{text.strip()}")
        parts.append("## Assistant")
        return "\n\n".join(parts)

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    out.append(b.get("text", ""))
                elif t == "tool_use":
                    out.append(
                        f"[tool_use: {b.get('name', '')} "
                        f"{json.dumps(b.get('input', {}), ensure_ascii=False)}]"
                    )
                elif t == "tool_result":
                    out.append(f"[tool_result: {self._extract_text(b.get('content'))}]")
                elif t == "image":
                    out.append("[image omitted]")
            return "\n".join(out)
        return ""

    async def _run_gemini(self, prompt: str, model: str) -> str:
        cli = getattr(self.config, "cli_command", "gemini")
        timeout = int(getattr(self.config, "timeout_seconds", 180))

        def _run() -> str:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            # cmd /c で gemini の .cmd shim を実行する。引数は list 要素として渡すので
            # cli が空白入り絶対パスでも安全 (quote 不要)。model は _resolve_model で
            # 厳格 allowlist 済みなので shell injection はできない。
            argv = ["cmd", "/c", cli, "-m", model, "-o", "text"]
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            try:
                out, err = proc.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                # cmd.exe -> node/gemini の孫プロセスまでツリーごと kill して孤児を防ぐ
                # (Popen.kill は直接の子=cmd.exe しか殺さない)。
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.kill()
                    proc.communicate()
                except Exception:  # noqa: BLE001
                    pass
                raise
            # 非ゼロ終了 (auth 失効 / モデル不在 / quota / クラッシュ等) を成功扱いで
            # 握りつぶさない。raise すると handle_request が 502 にマップする。
            if proc.returncode != 0:
                snippet = (err or "").strip().replace("\n", " ")[:300]
                raise RuntimeError(
                    f"gemini CLI exited {proc.returncode}: {snippet or '(no stderr)'}"
                )
            return out or ""

        raw = await asyncio.to_thread(_run)
        return self._strip_noise(raw)

    def _strip_noise(self, raw: str) -> str:
        prefixes = getattr(self.config, "noise_prefixes", [])
        kept = [
            ln
            for ln in raw.splitlines()
            if not any(ln.strip().startswith(p) for p in prefixes)
        ]
        return "\n".join(kept).strip()

    def _to_anthropic_message(self, text: str, model: str) -> dict[str, Any]:
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

    def _sse(self, msg: dict[str, Any]) -> Iterator[bytes]:
        text = msg["content"][0]["text"] if msg.get("content") else ""
        model = msg.get("model", "")
        mid = msg.get("id", f"msg_{uuid.uuid4().hex}")

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
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield ev(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield ev(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
        yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield ev(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
        yield ev("message_stop", {"type": "message_stop"})

    def _error_response(self, status: int, message: str) -> Response:
        payload = {"type": "error", "error": {"type": "api_error", "message": message}}
        return Response(
            content=json.dumps(payload),
            status_code=status,
            media_type="application/json",
        )
