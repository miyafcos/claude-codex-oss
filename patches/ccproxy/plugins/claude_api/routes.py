"""API routes for Claude API plugin.

claude-codex-hybrid patch
=========================
Upstream の claude_api/routes.py は POST /v1/messages と /v1/chat/completions の
ボディを FastAPI の依存性 (`_: CreateMessageRequest` / `_: ChatCompletionRequest`)
で厳格に pydantic 検証し、GET /v1/models も `response_model` で検証する。

Claude Code (claude.exe) は `?beta=true` で in-band system / 拡張ブロックを含む
messages を送るため、この厳格検証は 422 を返してしまう。codex プラグインの routes は
同じ /v1/messages を `response_model=None` かつ **生 Request のみ**で受け取り
(adapter 側で normalize する) ため 422 にならない。本パッチは claude_api routes を
codex routes と同じ「生 Request 受け + response_model=None」方針に揃える:

  - POST /v1/messages         : `_: CreateMessageRequest` を除去、response_model=None
  - POST /v1/chat/completions : `_: ChatCompletionRequest` を除去、response_model=None
  - POST /v1/responses        : response_model は元から None (変更なし)
  - GET  /v1/models           : response_model=None (ModelList 検証による 500 を回避)

adapter.handle_request(request) が body を自前で読むので、依存性での事前検証は不要。
"""

import uuid
import json
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from ccproxy.api.decorators import with_format_chain
from ccproxy.api.dependencies import (
    get_plugin_adapter,
    get_provider_config_dependency,
)
from ccproxy.auth.dependencies import ConditionalAuthDep
from ccproxy.core.constants import (
    FORMAT_ANTHROPIC_MESSAGES,
    FORMAT_OPENAI_CHAT,
    FORMAT_OPENAI_RESPONSES,
    UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES,
)
from ccproxy.core.logging import get_plugin_logger

from .config import ClaudeAPISettings


if TYPE_CHECKING:
    pass

logger = get_plugin_logger()

ClaudeAPIAdapterDep = Annotated[Any, Depends(get_plugin_adapter("claude_api"))]
ClaudeAPIConfigDep = Annotated[
    ClaudeAPISettings,
    Depends(get_provider_config_dependency("claude_api", ClaudeAPISettings)),
]

# Main API Router - Core Claude API endpoints
router = APIRouter()


def _cast_result(result: object) -> Any:
    return cast(Any, result)


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


async def _handle_adapter_request(
    request: Request,
    adapter: Any,
) -> Any:
    # Direct Claude endpoints also reject Codex-only saved references. Native
    # Anthropic signatures and the rest of the request remain unchanged.
    try:
        payload = json.loads(await request.body())
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and _strip_codex_replay_references(payload):
        request._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request.scope["headers"] = [(key, value) for key, value in request.scope.get("headers", []) if key.lower() not in {b"content-length", b"transfer-encoding"}] + [(b"content-length", str(len(request._body)).encode("ascii"))]
        for attribute in ("_headers", "_json"):
            if hasattr(request, attribute):
                delattr(request, attribute)
    result = await adapter.handle_request(request)
    return _cast_result(result)


@router.post("/v1/messages", response_model=None)
@with_format_chain(
    [FORMAT_ANTHROPIC_MESSAGES], endpoint=UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES
)
async def create_anthropic_message(
    request: Request,
    auth: ConditionalAuthDep,
    adapter: ClaudeAPIAdapterDep,
) -> Any:
    """Create a message using Claude AI with native Anthropic format.

    生 Request のみを受け取り (codex routes と同じ)、adapter 側で
    normalize / validation を行う。FastAPI の事前ボディ検証はしない
    (claude.exe の ?beta=true 拡張 messages が 422 になるため)。
    """
    return await _handle_adapter_request(request, adapter)


@router.post("/v1/chat/completions", response_model=None)
@with_format_chain(
    [FORMAT_OPENAI_CHAT, FORMAT_ANTHROPIC_MESSAGES],
    endpoint=UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES,
)
async def create_openai_chat_completion(
    request: Request,
    auth: ConditionalAuthDep,
    adapter: ClaudeAPIAdapterDep,
) -> Any:
    """Create a chat completion using Claude AI with OpenAI-compatible format."""
    return await _handle_adapter_request(request, adapter)


@router.post("/v1/responses", response_model=None)
@with_format_chain(
    [FORMAT_OPENAI_RESPONSES, FORMAT_ANTHROPIC_MESSAGES],
    endpoint=UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES,
)
async def claude_v1_responses(
    request: Request,
    auth: ConditionalAuthDep,
    adapter: ClaudeAPIAdapterDep,
) -> Any:
    """Response API compatible endpoint using Claude backend."""
    session_id = request.headers.get("session_id") or str(uuid.uuid4())
    return await _handle_adapter_request(request, adapter)


@router.get("/v1/models", response_model=None)
async def list_models(
    request: Request,
    auth: ConditionalAuthDep,
    config: ClaudeAPIConfigDep,
) -> dict[str, Any]:
    """List available Claude models from configuration.

    response_model 検証を外す (upstream の openai ModelList 検証が、
    config の models_endpoint カード形状と合わず 500 になるため)。
    codex routes と同様に slug / display_name を補完して返す。
    """
    models: list[dict[str, Any]] = []
    for card in config.models_endpoint:
        model_data = card.model_dump(mode="json")
        slug = model_data.get("slug") or model_data.get("id") or model_data.get("root")
        if isinstance(slug, str) and slug:
            model_data.setdefault("slug", slug)
            model_data.setdefault("display_name", slug)
        model_data.setdefault("object", "model")
        models.append(model_data)
    return {"object": "list", "data": models}
