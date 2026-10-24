"""Anthropic-wire pass-through adapter for a local FCC gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ccproxy.core.logging import get_plugin_logger
from ccproxy.services.adapters.base import BaseAdapter


logger = get_plugin_logger()

_FCC_MODEL_PREFIXES = (
    "anthropic/",
    "claude-3-freecc-no-thinking/",
    "open_router/",
    "ollama/",
)
_REQUEST_HEADERS = ("anthropic-version", "anthropic-beta", "x-request-id")
_RESPONSE_HEADERS = (
    "content-type",
    "request-id",
    "retry-after",
    "x-request-id",
    "x-should-retry",
)


class FccAdapter(BaseAdapter):
    def __init__(self, config: Any, **kwargs: Any) -> None:
        super().__init__(config=config, **kwargs)
        self._token_cache_key: tuple[int, int] | None = None
        self._token_cache = ""

    async def handle_request(self, request: Request) -> Response | StreamingResponse:
        try:
            payload = json.loads(await request.body() or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._error(400, "fcc: invalid JSON request")
        if not isinstance(payload, dict):
            return self._error(400, "fcc: request body must be an object")

        model = str(payload.get("model") or "")
        if model == "fcc-default":
            payload["model"] = str(getattr(self.config, "default_alias", "") or "")
        elif not model.startswith(_FCC_MODEL_PREFIXES):
            return self._error(400, "fcc: unsupported model id")

        token = self._auth_token()
        if not token:
            return self._error(503, "fcc: local gateway authentication is unavailable")

        try:
            base_url = self._base_url()
        except ValueError:
            return self._error(503, "fcc: base URL must use loopback HTTP")

        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        for name in _REQUEST_HEADERS:
            value = request.headers.get(name)
            if value:
                headers[name] = value

        timeout = httpx.Timeout(
            float(getattr(self.config, "timeout_seconds", 600.0) or 600.0),
            connect=float(getattr(self.config, "connect_timeout_seconds", 5.0) or 5.0),
        )
        client = httpx.AsyncClient(timeout=timeout)
        upstream: httpx.Response | None = None
        try:
            upstream_request = client.build_request(
                "POST",
                f"{base_url}/v1/messages",
                headers=headers,
                json=payload,
            )
            upstream = await client.send(
                upstream_request, stream=bool(payload.get("stream"))
            )
        except httpx.TimeoutException:
            await client.aclose()
            return self._error(504, "fcc: local gateway timed out")
        except httpx.HTTPError:
            await client.aclose()
            return self._error(503, "fcc: local gateway is unavailable")
        except BaseException:
            # Cancellation and unexpected transport failures must not leave the
            # client pool open. Preserve the original exception for the host.
            await client.aclose()
            raise

        response_headers = self._safe_response_headers(upstream)
        if upstream.status_code >= 400:
            status = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            return self._error(status, f"fcc: local gateway returned HTTP {status}")

        if payload.get("stream"):
            async def iter_body() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                iter_body(),
                status_code=upstream.status_code,
                media_type="text/event-stream",
                headers=response_headers,
            )

        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        return Response(
            content=content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=response_headers,
        )

    async def handle_streaming(
        self, request: Request, endpoint: str, **kwargs: Any
    ) -> Response | StreamingResponse:
        return await self.handle_request(request)

    async def list_models(self) -> list[dict[str, Any]]:
        """Fetch FCC's dynamic gateway catalog without exposing credentials."""
        token = self._auth_token()
        if not token:
            return []
        try:
            base_url = self._base_url()
            timeout = httpx.Timeout(
                float(getattr(self.config, "connect_timeout_seconds", 5.0) or 5.0)
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{base_url}/v1/models",
                    headers={"authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            logger.warning("fcc_model_catalog_unavailable")
            return []
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [item for item in data if isinstance(item, dict)]

    async def cleanup(self) -> None:
        return None

    def _base_url(self) -> str:
        raw = str(getattr(self.config, "base_url", "") or "").rstrip("/")
        parsed = urlparse(raw)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("FCC base URL is not loopback HTTP")
        return raw

    def _auth_token(self) -> str:
        explicit = str(getattr(self.config, "auth_token", "") or "")
        if explicit:
            return explicit
        path = Path(getattr(self.config, "env_file", Path.home() / ".fcc" / ".env")).expanduser()
        try:
            stat = path.stat()
            cache_key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._token_cache_key = None
            self._token_cache = ""
            return ""
        if cache_key != self._token_cache_key:
            values = dotenv_values(path)
            self._token_cache = str(values.get("ANTHROPIC_AUTH_TOKEN") or "")
            self._token_cache_key = cache_key
        return self._token_cache

    @staticmethod
    def _safe_response_headers(response: httpx.Response) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in response.headers.items():
            lower = name.lower()
            if lower in _RESPONSE_HEADERS or lower.startswith("x-ratelimit-"):
                headers[lower] = value
        return headers

    @staticmethod
    def _error(status: int, message: str) -> Response:
        return Response(
            content=json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": message}}
            ),
            status_code=status,
            media_type="application/json",
        )
