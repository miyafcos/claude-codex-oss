"""Dispatcher routes: model-name based backend routing at the proxy root.

POST /v1/messages and GET /v1/models are mounted at the root (prefix="").
The launcher points ANTHROPIC_BASE_URL at http://127.0.0.1:4026/router, and
this router is mounted under that prefix by the plugin manifest, so the proxy
sees /router/v1/messages.

dispatch_messages reads the request body's "model", picks a backend plugin,
sets the per-backend format_chain on the request context (mirroring the
@with_format_chain decorators on codex/claude_api routes), then delegates to
that backend's adapter.handle_request — bypassing the backend route's own body
validation (which is exactly why we can pass Claude Code's ?beta=true extended
messages through to claude_api without the 422 the raw route would raise).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ccproxy.auth.dependencies import ConditionalAuthDep
from ccproxy.core.constants import (
    FORMAT_ANTHROPIC_MESSAGES,
    FORMAT_OPENAI_RESPONSES,
    UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES,
)
from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.request_context import RequestContext


logger = get_plugin_logger()

router = APIRouter()

# Per-backend format chain — must match the @with_format_chain declarations on
# each backend's /v1/messages route (codex: anthropic->openai_responses,
# claude_api & gemini_cli & fugu: native anthropic, no conversion; fugu does
# Anthropic↔OpenAI chat-completions translation inside its own adapter).
_FORMAT_CHAINS: dict[str, list[str]] = {
    "codex": [FORMAT_ANTHROPIC_MESSAGES, FORMAT_OPENAI_RESPONSES],
    "claude_api": [FORMAT_ANTHROPIC_MESSAGES],
    "gemini_cli": [FORMAT_ANTHROPIC_MESSAGES],
    "fugu": [FORMAT_ANTHROPIC_MESSAGES],
    "fcc": [FORMAT_ANTHROPIC_MESSAGES],
    "nanogpt": [FORMAT_ANTHROPIC_MESSAGES],
}

# Model-name prefix -> backend plugin name.
_DEFAULT_BACKEND = "codex"
_FUGU_BACKEND = "fugu"
_FUGU_QUOTA_ERROR = "usage_limit_reached"
_CLAUDE_BACKEND = "claude_api"
_FCC_BACKEND = "fcc"
_FCC_UPSTREAM_PREFIXES = ("open_router/", "ollama/")
# 配備済みのプロファイル境界を維持する。モデルの能力保証ではない。
_FCC_SAME_CONTEXT_PREFIXES = ("open_router/",)
_PICKER_PREFIX = "anthropic/gateway/"
_PICKER_BACKENDS = {
    "codex": "codex",
    "claude": "claude_api",
    "fugu": "fugu",
    "fcc": "fcc",
    "nanogpt": "nanogpt",
}
_PICKER_LABELS = {
    "codex": "Codex",
    "claude_api": "Claude",
    "fugu": "Fugu",
    "fcc": "Open",
    "nanogpt": "NanoGPT",
}


def _models_json_paths() -> tuple[Path, ...]:
    repo_runtime = Path(__file__).resolve().parents[2] / "models.json"
    installed = Path.home() / ".claude-codex" / "config" / "models.json"
    return (repo_runtime, installed)


def _claude_fallback_model() -> str:
    for path in _models_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        try:
            model = data["backends"]["claude"]["defaultModel"]
        except (KeyError, TypeError):
            continue
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""


def _is_web_search_request(data: dict[str, Any] | None) -> bool:
    tools = data.get("tools") if data is not None else None
    messages = data.get("messages") if data is not None else None
    choice = data.get("tool_choice") if data is not None else None
    if isinstance(choice, dict) and choice.get("type") == "none":
        return False
    return (
        isinstance(tools, list) and len(tools) == 1
        and isinstance(tools[0], dict) and isinstance(tools[0].get("type"), str)
        and (tools[0]["type"] == "web_search" or tools[0]["type"].startswith("web_search_"))
        and isinstance(messages, list) and len(messages) == 1
        and isinstance(messages[0], dict) and messages[0].get("role") == "user"
    )


def _resolve_model(model: str) -> tuple[str | None, str, bool]:
    """Return backend, upstream model id, and explicit-picker flag."""
    raw = model or ""
    if raw.lower().startswith(_PICKER_PREFIX):
        remainder = raw[len(_PICKER_PREFIX) :]
        family, separator, upstream = remainder.partition("/")
        backend = _PICKER_BACKENDS.get(family.lower())
        if backend is None or not separator or not upstream:
            return None, raw, True
        if backend == _FCC_BACKEND and upstream == "default":
            upstream = "fcc-default"
        return backend, upstream, True
    backend = _select_legacy_backend(raw)
    # A provider-specific legacy id is still an explicit routing request.
    # Failing closed prevents a selected Claude/Fugu/FCC model from being sent
    # to Codex when that provider is unavailable.
    return backend, raw, backend != _DEFAULT_BACKEND


def _is_fcc_upstream_model(model: str) -> bool:
    m = (model or "").lower()
    return m == "fcc-default" or m.startswith(_FCC_UPSTREAM_PREFIXES)


def _is_fcc_same_context_model(model: str) -> bool:
    return (model or "").lower().startswith(_FCC_SAME_CONTEXT_PREFIXES)


def _select_legacy_backend(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude"):
        return "claude_api"
    if m.startswith("gemini"):
        return "gemini_cli"
    if m.startswith("fugu"):
        return "fugu"
    if m.startswith("nanogpt/"):
        return "nanogpt"
    if _is_fcc_upstream_model(m):
        return _FCC_BACKEND
    # gpt-*, o1-*, o3-*, and anything unknown go to codex (which has its own
    # model_mappings to normalize them to a real GPT id).
    return _DEFAULT_BACKEND


def _select_backend(model: str) -> str | None:
    """Compatibility helper used by tests and diagnostics."""
    return _resolve_model(model)[0]


def _get_runtime(request: Request, backend: str) -> Any:
    registry = getattr(request.app.state, "plugin_registry", None)
    if registry is None:
        return None
    try:
        return registry.get_runtime(backend)
    except Exception:
        return None


def _error(status: int, message: str) -> Response:
    payload = {
        "type": "error",
        "error": {"type": "api_error", "message": message},
    }
    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type="application/json",
    )


def _response_json(response: Any) -> dict[str, Any] | None:
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray, memoryview)):
        return None
    try:
        parsed = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _fugu_quota_exhausted(response: Any) -> tuple[bool, Any]:
    """Recognize only Fugu's subscription-window exhaustion response.

    FuguAdapter currently maps upstream HTTP errors to a 502 whose error
    message embeds ``fugu upstream <status>: <json>``.  The direct 429 shape is
    also accepted so the dispatcher keeps working if the adapter later starts
    preserving upstream status codes.
    """
    status = getattr(response, "status_code", None)
    payload = _response_json(response)
    if payload is None:
        return False, None

    quota_payload: dict[str, Any] | None = None
    if status == 429:
        quota_payload = payload
    elif status == 502:
        outer_error = payload.get("error")
        if not isinstance(outer_error, dict) or outer_error.get("type") != "api_error":
            return False, None
        message = outer_error.get("message")
        prefix = "fugu upstream 429:"
        if not isinstance(message, str) or not message.startswith(prefix):
            return False, None
        try:
            embedded = json.loads(message[len(prefix) :].strip())
        except json.JSONDecodeError:
            return False, None
        if isinstance(embedded, dict):
            quota_payload = embedded

    if quota_payload is None:
        return False, None
    error = quota_payload.get("error")
    if not isinstance(error, dict) or error.get("type") != _FUGU_QUOTA_ERROR:
        return False, None

    headers = getattr(response, "headers", None)
    resets_at = None
    if headers is not None:
        try:
            resets_at = headers.get("x-codex-primary-reset-at")
        except Exception:
            resets_at = None
    if resets_at is None:
        resets_at = error.get("resets_at")
    return True, resets_at


def _strip_codex_replay_references(data: dict[str, Any]) -> bool:
    """Remove only our reserved opaque references on a provider switch."""
    changed = False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    kept_messages = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            filtered = [block for block in content if not (isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("signature"), str) and block["signature"].startswith(("ccproxy-openai-replay-v1:", "ccproxy-openai-replay-v2:")))]
            if len(filtered) != len(content):
                changed = True
                if not filtered:
                    continue
                message = {**message, "content": filtered}
        kept_messages.append(message)
    if changed:
        data["messages"] = kept_messages
    return changed


def _replace_request_body(request: Request, data: dict[str, Any]) -> None:
    request._body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = request.scope.get("headers")
    if isinstance(headers, list):
        request.scope["headers"] = [
            (name, value) for name, value in headers
            if name.lower() not in (b"content-length", b"transfer-encoding")
        ] + [(b"content-length", str(len(request._body)).encode("ascii"))]
        if hasattr(request, "_headers"):
            delattr(request, "_headers")
    if hasattr(request, "_json"):
        delattr(request, "_json")


def _fcc_profile_request(request: Request) -> bool:
    path = str(request.scope.get("path") or "")
    return "/fcc/" in path


def _stamp_route_headers(response: Any, requested: str, routed: str, backend: str, request_id: str) -> None:
    if hasattr(response, "headers"):
        for name, value in (("requested-model", requested), ("routed-model", routed), ("backend", backend), ("request-id", request_id)):
            response.headers["x-claude-codex-" + name] = str(value).encode("ascii", "backslashreplace").decode("ascii").replace("\r", "").replace("\n", "")


def _search_error(request: Request, status: int, message: str, requested: str, routed: str = "") -> Response:
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        ctx = RequestContext(request_id=str(uuid.uuid4()), start_time=time.perf_counter(), logger=logger, metadata={})
        request.state.context = ctx
    if not getattr(ctx, "request_id", None):
        ctx.request_id = str(uuid.uuid4())
    if getattr(ctx, "metadata", None) is None:
        ctx.metadata = {}
    ctx.metadata["server_tool"] = "web_search"
    logger.warning("dispatch_search_error", requested_model=requested, upstream_model=routed, backend=_DEFAULT_BACKEND, request_id=ctx.request_id, reason=message)
    response = _error(status, message)
    _stamp_route_headers(response, requested, routed, _DEFAULT_BACKEND, ctx.request_id)
    response.headers["x-claude-codex-server-tool"] = "web_search"
    return response


@router.post("/fcc/v1/messages", response_model=None)
@router.post("/v1/messages", response_model=None)
async def dispatch_messages(request: Request, auth: ConditionalAuthDep) -> Any:
    body = await request.body()
    model = ""
    parsed: dict[str, Any] | None = None
    if body:
        try:
            candidate = json.loads(body)
            if isinstance(candidate, dict):
                parsed = candidate
                model = str(candidate.get("model") or "")
        except Exception:
            model = ""

    if any(ord(character) < 32 or ord(character) > 126 for character in model):
        return _error(400, "dispatcher: model id must contain printable ASCII only")
    backend, upstream_model, _explicit_picker_model = _resolve_model(model)
    if backend is None:
        return _error(400, "dispatcher: unknown gateway model id")
    if _fcc_profile_request(request):
        if backend != _FCC_BACKEND or not _is_fcc_upstream_model(upstream_model):
            return _error(
                400,
                "dispatcher: the FCC profile only accepts fcc-default or an "
                "open_router/ - ollama/ model id",
            )
    elif backend == _FCC_BACKEND and not _is_fcc_same_context_model(upstream_model):
        return _error(
            400,
            "dispatcher: local FCC routes (ollama, fcc-default) need "
            "--backend fcc for the 52k context guard",
        )
    if backend != _DEFAULT_BACKEND and parsed is not None and _strip_codex_replay_references(parsed):
        _replace_request_body(request, parsed)
    search_route = backend == _DEFAULT_BACKEND and upstream_model.lower().startswith("gpt-") and _is_web_search_request(parsed)
    if parsed is not None and upstream_model != model:
        parsed["model"] = upstream_model
        _replace_request_body(request, parsed)
    runtime = _get_runtime(request, backend)
    if runtime is None or getattr(runtime, "adapter", None) is None:
        if search_route:
            return _search_error(request, 503, f"dispatcher: backend unavailable ({backend}); no search was performed", model, upstream_model)
        return _error(503, f"dispatcher: backend unavailable ({backend})")

    chain = _FORMAT_CHAINS.get(backend, [FORMAT_ANTHROPIC_MESSAGES])
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        request.state.context = RequestContext(
            request_id=str(uuid.uuid4()),
            start_time=time.perf_counter(),
            logger=logger,
            metadata={},
            format_chain=list(chain),
        )
    else:
        ctx.format_chain = list(chain)
        if getattr(ctx, "metadata", None) is None:
            ctx.metadata = {}
    request.state.context.metadata["endpoint"] = UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES
    if not getattr(request.state.context, "request_id", None):
        request.state.context.request_id = str(uuid.uuid4())

    if search_route:
        request.state.context.metadata["server_tool"] = "web_search"
    logger.warning("dispatch_route", requested_model=model, upstream_model=upstream_model, backend=backend, request_id=request.state.context.request_id, server_tool="web_search" if search_route else None)
    response = await runtime.adapter.handle_request(request)
    _stamp_route_headers(response, model, upstream_model, backend, request.state.context.request_id)
    if search_route and hasattr(response, "headers"):
        response.headers["x-claude-codex-server-tool"] = "web_search"
    if backend != _FUGU_BACKEND:
        return response

    quota_exhausted, resets_at = _fugu_quota_exhausted(response)
    if not quota_exhausted:
        return response

    fallback_runtime = _get_runtime(request, _CLAUDE_BACKEND)
    if fallback_runtime is None or getattr(fallback_runtime, "adapter", None) is None:
        return response
    fallback_model = _claude_fallback_model()
    if not fallback_model or parsed is None:
        return response

    parsed["model"] = fallback_model
    _replace_request_body(request, parsed)
    request.state.context.format_chain = list(_FORMAT_CHAINS[_CLAUDE_BACKEND])
    logger.warning(
        "dispatch_fugu_quota_fallback",
        model=model,
        fallback=_CLAUDE_BACKEND,
        resets_at=resets_at,
    )
    response = await fallback_runtime.adapter.handle_request(request)
    _stamp_route_headers(response, model, fallback_model, _CLAUDE_BACKEND, request.state.context.request_id)
    return response


@router.get("/fcc/v1/models", response_model=None)
@router.get("/v1/models", response_model=None)
async def list_models(request: Request, auth: ConditionalAuthDep) -> dict[str, Any]:
    """Aggregate safe same-context models for the Claude Code picker."""
    data: list[dict[str, Any]] = []
    seen: set[str] = set()
    registry = getattr(request.app.state, "plugin_registry", None)
    if registry is not None:
        fcc_profile = _fcc_profile_request(request)
        backends = (
            (_FCC_BACKEND,)
            if fcc_profile
            else ("codex", "claude_api", "fugu", _FCC_BACKEND, "nanogpt")
        )
        for backend in backends:
            try:
                runtime = registry.get_runtime(backend)
            except Exception:
                runtime = None
            cfg = getattr(runtime, "config", None) if runtime else None
            cards: list[Any] = []
            adapter = getattr(runtime, "adapter", None) if runtime else None
            dynamic_list = getattr(adapter, "list_models", None)
            # FCC can expose hundreds of upstream provider models. Claude Code
            # rejects that volatile catalog during startup, while each lookup
            # can also block model discovery for five seconds. FCC therefore
            # publishes only its stable configured alias below; fcc-model owns
            # the actual upstream selection.
            if callable(dynamic_list) and backend != _FCC_BACKEND:
                try:
                    cards.extend(await dynamic_list())
                except Exception:
                    logger.warning("dispatch_model_catalog_unavailable", backend=backend)
            if cfg is not None:
                cards.extend(getattr(cfg, "models_endpoint", []) or [])
            for card in cards:
                md = card if isinstance(card, dict) else card.model_dump(mode="json")
                md = dict(md)
                model_id = md.get("id") or md.get("root")
                if not isinstance(model_id, str):
                    continue
                if backend == _FCC_BACKEND:
                    if not _is_fcc_upstream_model(model_id):
                        continue
                    if not fcc_profile and not _is_fcc_same_context_model(model_id):
                        continue
                picker_id = _picker_model_id(backend, model_id)
                if picker_id in seen:
                    continue
                seen.add(picker_id)
                original_display = str(md.get("display_name") or model_id)
                label = _PICKER_LABELS.get(backend, backend)
                md["id"] = picker_id
                md["root"] = picker_id
                md.setdefault("object", "model")
                md["slug"] = picker_id
                md["display_name"] = f"{label} - {original_display}"
                data.append(md)
    return {"object": "list", "data": data}


def _picker_model_id(backend: str, model_id: str) -> str:
    """Map an upstream id to the Claude Code discovery namespace."""
    if backend == _CLAUDE_BACKEND and model_id.lower().startswith(
        ("claude", "anthropic")
    ):
        return model_id
    family = {
        "codex": "codex",
        "claude_api": "claude",
        "fugu": "fugu",
        "fcc": "fcc",
        "nanogpt": "nanogpt",
    }[backend]
    # Keep the final segment identical to the launcher's bare alias. Claude
    # Code accepts gateway aliases when the selected model matches the card's
    # trailing segment; collapsing fcc-default to "default" breaks validation.
    return f"{_PICKER_PREFIX}{family}/{model_id}"
