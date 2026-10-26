"""Offline NanoGPT routing tests with local synthetic dispatcher fixtures."""
from __future__ import annotations

import asyncio

import importlib.util

import json

import sys

import types

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_ROUTES_PATH = _REPO_ROOT / "runtime" / "ccproxy-plugins" / "dispatcher" / "routes.py"

_MISSING = object()

class _FakeAPIRouter:
    """Records route registrations; its decorators return the function as-is
    (real fastapi behaves the same), so the decorated handlers stay callable."""

    def __init__(self, *args, **kwargs) -> None:
        self.routes: list = []

    def post(self, *args, **kwargs):
        def deco(fn):
            self.routes.append(("POST", args, kwargs, fn))
            return fn

        return deco

    def get(self, *args, **kwargs):
        def deco(fn):
            self.routes.append(("GET", args, kwargs, fn))
            return fn

        return deco

class _FakeFastapiRequest:  # only referenced in (stringified) annotations
    pass

class _FakeResponse:
    def __init__(self, content=None, status_code=200, media_type=None, headers=None) -> None:
        self.status_code = status_code
        self.media_type = media_type
        self.headers = headers or {}
        if isinstance(content, str):
            self.body = content.encode("utf-8")
        elif isinstance(content, bytes):
            self.body = content
        else:
            self.body = b""

class _StubLogger:
    def __getattr__(self, _name):
        def _noop(*args, **kwargs):
            return None

        return _noop

class _StubRequestContext:
    def __init__(
        self,
        request_id=None,
        start_time=None,
        logger=None,
        metadata=None,
        format_chain=None,
    ) -> None:
        self.request_id = request_id
        self.start_time = start_time
        self.logger = logger
        self.metadata = metadata if metadata is not None else {}
        self.format_chain = format_chain

def _build_stub_modules() -> dict[str, types.ModuleType]:
    mods: dict[str, types.ModuleType] = {}

    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.APIRouter = _FakeAPIRouter
    fastapi_mod.Request = _FakeFastapiRequest

    responses_mod = types.ModuleType("fastapi.responses")
    responses_mod.Response = _FakeResponse
    fastapi_mod.responses = responses_mod

    ccproxy_mod = types.ModuleType("ccproxy")
    auth_mod = types.ModuleType("ccproxy.auth")
    deps_mod = types.ModuleType("ccproxy.auth.dependencies")
    deps_mod.ConditionalAuthDep = object  # used only in annotations

    core_mod = types.ModuleType("ccproxy.core")
    constants_mod = types.ModuleType("ccproxy.core.constants")
    constants_mod.FORMAT_ANTHROPIC_MESSAGES = "anthropic_messages"
    constants_mod.FORMAT_OPENAI_RESPONSES = "openai_responses"
    constants_mod.UPSTREAM_ENDPOINT_ANTHROPIC_MESSAGES = "endpoint_anthropic_messages"

    logging_mod = types.ModuleType("ccproxy.core.logging")
    logging_mod.get_plugin_logger = lambda *a, **k: _StubLogger()

    reqctx_mod = types.ModuleType("ccproxy.core.request_context")
    reqctx_mod.RequestContext = _StubRequestContext

    mods["fastapi"] = fastapi_mod
    mods["fastapi.responses"] = responses_mod
    mods["ccproxy"] = ccproxy_mod
    mods["ccproxy.auth"] = auth_mod
    mods["ccproxy.auth.dependencies"] = deps_mod
    mods["ccproxy.core"] = core_mod
    mods["ccproxy.core.constants"] = constants_mod
    mods["ccproxy.core.logging"] = logging_mod
    mods["ccproxy.core.request_context"] = reqctx_mod
    return mods

def _load_routes():
    """Load the hyphenated-package routes module under functional stubs.

    Stubs are installed unconditionally (so the decorators are real, even if a
    prior test left a MagicMock ``fastapi`` in sys.modules) and restored in a
    finally block, so the loaded module keeps working references while global
    sys.modules state is left untouched for other tests."""
    stubs = _build_stub_modules()
    saved = {name: sys.modules.get(name, _MISSING) for name in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            "dispatcher_routes_under_test", _ROUTES_PATH
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev

R = _load_routes()

_UNSET = object()

class _FakeAdapter:
    def __init__(self, result="RESULT") -> None:
        self.result = result
        self.calls: list = []
        self.bodies: list[bytes] = []

    async def handle_request(self, request):
        self.calls.append(request)
        self.bodies.append(await request.body())
        return self.result

class _FakeCatalogAdapter(_FakeAdapter):
    def __init__(self, models: list[dict]) -> None:
        super().__init__()
        self.models = models

    async def list_models(self):
        self.calls.append("list_models")
        return list(self.models)

class _FakeStreamingResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.status_code = 200
        self.media_type = "text/event-stream"
        self.body_iterator = iter(chunks)

class _FakeRuntime:
    def __init__(self, adapter=None, config=None) -> None:
        self.adapter = adapter
        self.config = config

class _FakeRegistry:
    def __init__(self, runtimes: dict) -> None:
        self.runtimes = runtimes

    def get_runtime(self, backend: str):
        if backend not in self.runtimes:
            raise KeyError(backend)
        return self.runtimes[backend]

class _FakeReqState:
    pass

class _FakeAppState:
    def __init__(self, registry) -> None:
        self.plugin_registry = registry

class _FakeApp:
    def __init__(self, registry) -> None:
        self.state = _FakeAppState(registry)

class _FakeRequest:
    def __init__(
        self,
        model=_UNSET,
        registry=None,
        context=_UNSET,
        raw_body=_UNSET,
        path="/router/v1/messages",
    ) -> None:
        if raw_body is not _UNSET:
            self._body = raw_body
        elif model is _UNSET:
            self._body = b""
        else:
            self._body = json.dumps({"model": model}).encode("utf-8")
        self.app = _FakeApp(registry)
        self.state = _FakeReqState()
        self.scope = {"path": path}
        if context is not _UNSET:
            self.state.context = context

    async def body(self):
        return self._body

class _FakeCard:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self, mode="json"):
        return dict(self._data)

class _FakeConfig:
    def __init__(self, cards: list) -> None:
        self.models_endpoint = cards

def _dispatch(req):
    return asyncio.run(R.dispatch_messages(req, auth=None))

def _list_models(registry, path="/router/v1/models"):
    req = _FakeRequest(registry=registry, raw_body=b"", path=path)
    return asyncio.run(R.list_models(req, auth=None))


@pytest.mark.parametrize("model", [
    "nanogpt/z-ai/glm-5.3-flash-uncensored",
    "nanogpt/deepseek/deepseek-v4.1-flash-1017",
    "anthropic/gateway/nanogpt/z-ai/glm-5.3-flash-uncensored",
])
def test_nanogpt_selection_never_reaches_codex(model):
    codex = _FakeAdapter()
    nano = _FakeAdapter()
    registry = _FakeRegistry({
        "codex": _FakeRuntime(adapter=codex),
        "nanogpt": _FakeRuntime(adapter=nano),
    })
    request = _FakeRequest(model=model, registry=registry)
    _dispatch(request)
    assert len(nano.calls) == 1
    assert not codex.calls
    assert "glm-5.3" in json.loads(nano.bodies[0])["model"] or "deepseek" in model
    assert request.state.context.format_chain == [R.FORMAT_ANTHROPIC_MESSAGES]


def test_missing_nanogpt_does_not_fallback():
    codex = _FakeAdapter()
    request = _FakeRequest(
        model="nanogpt/z-ai/glm-5.3-flash-uncensored",
        registry=_FakeRegistry({"codex": _FakeRuntime(adapter=codex)}),
    )
    response = _dispatch(request)
    assert response.status_code == 503
    assert not codex.calls


def test_nanogpt_is_not_exposed_in_local_fcc_profile():
    nano = _FakeAdapter()
    request = _FakeRequest(
        model="nanogpt/z-ai/glm-5.3-flash-uncensored",
        path="/router/fcc/v1/messages",
        registry=_FakeRegistry({"nanogpt": _FakeRuntime(adapter=nano)}),
    )
    assert _dispatch(request).status_code == 400
    assert not nano.calls


def test_nanogpt_discovery_namespace_routes_back_to_provider():
    card = _FakeCard({"id": "z-ai/glm-5.3-flash-uncensored"})
    registry = _FakeRegistry({
        "nanogpt": _FakeRuntime(config=_FakeConfig([card])),
    })
    catalog = _list_models(registry)
    assert len(catalog["data"]) == 1
    model_id = catalog["data"][0]["id"]
    assert R._select_backend(model_id) == "nanogpt"
    assert catalog["data"][0]["display_name"].startswith("NanoGPT - ")
