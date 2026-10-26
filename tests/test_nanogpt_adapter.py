from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import Request


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "runtime" / "ccproxy-plugins" / "nanogpt"
MODEL = "z-ai/glm-5.3-flash-uncensored"
TOKEN = "nanogpt-test-secret-not-real"


def _load_adapter():
    names = ["ccproxy", "ccproxy.services", "ccproxy.services.adapters", "ccproxy.services.adapters.base", "nanogpt_test", "nanogpt_test.credentials"]
    saved = {name: sys.modules.get(name) for name in names}
    try:
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["nanogpt_test"].__path__ = [str(PLUGIN)]

        class BaseAdapter:
            def __init__(self, config=None, **kwargs):
                self.config = config

        sys.modules["ccproxy.services.adapters.base"].BaseAdapter = BaseAdapter
        credentials_spec = importlib.util.spec_from_file_location("nanogpt_test.credentials", PLUGIN / "credentials.py")
        credentials = importlib.util.module_from_spec(credentials_spec)
        sys.modules["nanogpt_test.credentials"] = credentials
        credentials_spec.loader.exec_module(credentials)
        spec = importlib.util.spec_from_file_location("nanogpt_test.adapter", PLUGIN / "adapter.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, credentials
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


ADAPTER, CREDENTIALS = _load_adapter()


def _request(**updates):
    payload = {"model": "nanogpt/" + MODEL, "messages": [{"role": "user", "content": "試験"}], "max_tokens": 64000}
    payload.update(updates)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer unrelated-proxy-secret")], "method": "POST", "path": "/router/v1/messages"})
    request._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return request


def _adapter(tmp_path):
    config = types.SimpleNamespace(
        models_endpoint=[types.SimpleNamespace(id=MODEL)],
        key_file=tmp_path / "absent.dpapi", timeout_seconds=1.0,
    )
    return ADAPTER.NanoGptAdapter(config=config)


def _policy(**updates):
    routing = {"recommendedMode": "subscription", "paidSpendPolicyAllowsBalance": False, "subscriptionQuotaAvailable": True}
    routing.update(updates)
    return {"routing": routing}


def _catalog(**updates):
    model = {"id": MODEL, "context_length": 1048576, "max_output_tokens": 32768, "capabilities": {"tool_calling": True}, "reasoning_efforts": ["low", "high"]}
    model.update(updates)
    return {"data": [model]}


def _transport(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def create(**kwargs):
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(ADAPTER.httpx, "AsyncClient", create)
    monkeypatch.setenv("NANOGPT_API_KEY", TOKEN)
    return clients


@pytest.mark.parametrize("routing", [None, {}, {"routing": {}}, _policy(paidSpendPolicyAllowsBalance=True), _policy(paidSpendPolicyAllowsBalance=0), _policy(subscriptionQuotaAvailable=None), _policy(subscriptionQuotaAvailable=1), _policy(recommendedMode="paygo")])
def test_policy_fails_closed(routing):
    assert ADAPTER.NanoGptAdapter.policy_error(routing)


def test_policy_accepts_exact_subscription_only():
    assert ADAPTER.NanoGptAdapter.policy_error(_policy()) == ""


def test_missing_key_does_not_make_http_request(monkeypatch, tmp_path):
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    monkeypatch.setattr(ADAPTER.httpx, "AsyncClient", lambda **kwargs: pytest.fail("キーなしで接続した"))
    response = asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert response.status_code == 503


def test_unknown_model_does_not_make_http_request(monkeypatch, tmp_path):
    monkeypatch.setattr(ADAPTER.httpx, "AsyncClient", lambda **kwargs: pytest.fail("未知モデルで接続した"))
    response = asyncio.run(_adapter(tmp_path).handle_request(_request(model="openai/gpt-test")))
    assert response.status_code == 400


@pytest.mark.parametrize("policy", [_policy(paidSpendPolicyAllowsBalance=True), _policy(subscriptionQuotaAvailable=False), {"routing": None}])
def test_rejected_policy_never_posts(monkeypatch, tmp_path, policy):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=policy)

    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert response.status_code == 403
    assert len(calls) == 1
    assert all(client.is_closed for client in clients)
    assert TOKEN.encode() not in response.body


def test_notice_in_two_text_blocks_preserves_workflow_last_block():
    stream = object.__new__(ADAPTER._ManagedStream)
    stream.notice = "Synthetic effort adjusted: max -> high\n"
    stream.notice_sent = False
    stream.notice_offset = 0
    blocks = []
    for index in (0, 1):
        frame = {"type": "content_block_start", "index": index,
                 "content_block": {"type": "text", "text": ""}}
        frames = stream._notice_frame(["event: content_block_start", "data: " + json.dumps(frame)])
        blocks.append(b''.join(frames))
    assert sum(b.count(stream.notice.strip().encode()) for b in blocks) == 2
    assert stream.notice.strip().encode() in blocks[-1]


@pytest.mark.parametrize("catalog", [{"data": []}, _catalog(context_length=524288), _catalog(capabilities={"tool_calling": False}), _catalog(context_length=True)])
def test_unverified_catalog_model_never_posts(monkeypatch, tmp_path, catalog):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=_policy() if request.url.path.endswith("usage") else catalog)

    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert response.status_code == 403
    assert all(client.is_closed for client in clients)


def test_safe_payload_preserves_tools_and_removes_billing_overrides(monkeypatch, tmp_path):
    sent = []
    tools = [{"name": "echo", "input_schema": {"type": "object", "properties": {}}}]
    messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "echo", "input": {}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "試験結果"}]}]

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert "x-provider" not in request.headers
        assert "x-billing-mode" not in request.headers
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "完了"}]})

    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request(
        tools=tools, messages=messages, output_config={"effort": "xhigh"},
        service_tier="priority", provider="other", fallbacks=["openai/other"],
        metadata={"secret": "do-not-send"},
    )))
    assert response.status_code == 200
    assert sent[0]["model"] == MODEL
    assert sent[0]["max_tokens"] == 32768
    assert sent[0]["output_config"] == {"effort": "high"}
    assert sent[0]["tools"] == tools
    assert sent[0]["messages"] == messages
    assert not {"provider", "fallbacks", "metadata", "service_tier"}.intersection(sent[0])
    assert all(client.is_closed for client in clients)
    assert response.headers["x-claude-codex-requested-effort"] == "xhigh"
    assert response.headers["x-claude-codex-sent-effort"] == "high"
    assert b"effort adjusted: xhigh -> high" in response.body


@pytest.mark.parametrize("content", ["環境情報", [{"type": "text", "text": "環境情報"}], "", [], None])
def test_inline_effort_moves_to_supported_top_level_without_losing_system_text(content):
    messages = [
        {"role": "user", "content": "合成入力"},
        {"role": "system", "content": content, "output_config": {"effort": "xhigh"}},
    ]
    original = json.loads(json.dumps(messages))
    payload = ADAPTER.NanoGptAdapter._payload(
        {"messages": messages, "output_config": {"effort": "low", "format": {"type": "json_schema"}}},
        MODEL, {"reasoning_efforts": ["low", "high"]},
    )
    assert payload["output_config"] == {"effort": "high", "format": {"type": "json_schema"}}
    expected = [{"role": "user", "content": "合成入力"}]
    if content not in (None, "", []):
        expected.append({"role": "system", "content": content})
    assert payload["messages"] == expected
    assert messages == original


def test_inline_effort_uses_latest_active_update_and_expires_temporary_system_message():
    payload = ADAPTER.NanoGptAdapter._payload({"messages": [
        {"role": "system", "content": "過去の一時情報", "output_config": {"effort": "max"}, "clear_at": "next_user_message"},
        {"role": "user", "content": "次の入力"},
        {"role": "system", "content": "現在の指示", "output_config": {"effort": "high"}},
        {"role": "system", "content": "", "output_config": {"effort": "low"}},
    ]}, MODEL, {"reasoning_efforts": ["low", "high", "max"]})
    assert payload["output_config"] == {"effort": "low"}
    assert payload["messages"] == [
        {"role": "user", "content": "次の入力"},
        {"role": "system", "content": "現在の指示"},
    ]


@pytest.mark.parametrize("control", [
    {"output_config": {"format": {"type": "json_schema"}}},
    {"output_config": None},
    {"clear_at": "unknown-expiry"},
    {"unrecognized_instruction": True},
])
@pytest.mark.parametrize("temporary", [False, True])
def test_unknown_inline_system_controls_fail_closed(control, temporary):
    with pytest.raises(ValueError):
        ADAPTER.NanoGptAdapter._payload({"messages": [
            {"role": "system", "content": "合成指示", **({"clear_at": "next_user_message"} if temporary else {}), **control},
            {"role": "user", "content": "合成入力"},
        ]}, MODEL, {"reasoning_efforts": ["low", "high"]})


def test_inline_normalization_preserves_tool_chain_and_active_expiry_without_mutation():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "fixture-call", "name": "echo", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "fixture-call", "content": "fixture"}]},
        {"role": "system", "content": [{"type": "text", "text": "現在だけの指示"}], "clear_at": "next_user_message", "output_config": {"effort": "high"}},
    ]
    original = json.loads(json.dumps(messages))
    payload = ADAPTER.NanoGptAdapter._payload({"messages": messages}, MODEL, {"reasoning_efforts": ["low", "high"]})
    assert payload["messages"][:2] == original[:2]
    assert payload["messages"][2] == {"role": "system", "content": [{"type": "text", "text": "現在だけの指示"}]}
    assert payload["output_config"] == {"effort": "high"}
    assert messages == original


def test_normal_cli_inline_effort_no_longer_triggers_upstream_400(monkeypatch, tmp_path):
    sent = []

    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        payload = json.loads(request.content)
        sent.append(payload)
        if any("output_config" in message for message in payload["messages"]):
            return httpx.Response(400, json={"error": {"message": "Inline effort updates are supported only for other models."}})
        assert payload["messages"][-1] == {"role": "system", "content": [{"type": "text", "text": "合成環境情報"}]}
        assert payload["output_config"]["effort"] == "high"
        return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "INLINE_OK"}]})

    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request(messages=[
        {"role": "user", "content": "合成入力"},
        {"role": "system", "content": [{"type": "text", "text": "合成環境情報"}], "output_config": {"effort": "xhigh"}},
    ], output_config={"effort": "xhigh"})))
    assert response.status_code == 200
    assert len(sent) == 1
    assert all(client.is_closed for client in clients)


def test_thinking_alias_uses_catalog_and_upstream_id(monkeypatch, tmp_path):
    local_id = "z-ai/glm-5.3-thinking"
    upstream_id = "z-ai/glm-5.3:thinking"
    sent = []

    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog(id=upstream_id))
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"type": "message", "model": upstream_id, "content": []})

    clients = _transport(monkeypatch, handler)
    adapter = _adapter(tmp_path)
    adapter.config.models_endpoint.append(types.SimpleNamespace(id=local_id))
    adapter.config.upstream_aliases = {local_id: upstream_id}
    response = asyncio.run(adapter.handle_request(_request(model="nanogpt/" + local_id)))
    assert response.status_code == 200
    assert sent[0]["model"] == upstream_id
    assert all(client.is_closed for client in clients)


def test_stream_round_trip_and_cleanup(monkeypatch, tmp_path):
    body = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'

    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    clients = _transport(monkeypatch, handler)

    async def run():
        response = await _adapter(tmp_path).handle_request(_request(stream=True))
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(run()) == body
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_upstream_status_does_not_leak_secrets(monkeypatch, tmp_path, status):
    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        return httpx.Response(status, text=TOKEN + " private content", headers={"location": "https://example.com"})

    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert response.status_code >= 400
    assert TOKEN.encode() not in response.body
    assert b"private content" not in response.body
    assert all(client.is_closed for client in clients)


def test_cancelled_request_closes_client(monkeypatch, tmp_path):
    def handler(request):
        raise asyncio.CancelledError()

    clients = _transport(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert all(client.is_closed for client in clients)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPIはWindows専用")
def test_dpapi_round_trip_does_not_store_plaintext(tmp_path):
    encrypted = CREDENTIALS.protect_key(TOKEN)
    assert TOKEN.encode() not in encrypted
    target = tmp_path / "nanogpt.dpapi"
    target.write_bytes(encrypted)
    assert CREDENTIALS.read_key(target) == TOKEN


@pytest.mark.parametrize("efforts", [None, 7, "high", {}, [None]])
def test_malformed_reasoning_catalog_does_not_raise(efforts):
    payload = ADAPTER.NanoGptAdapter._payload(
        {"output_config": {"effort": "xhigh"}}, MODEL,
        {"reasoning_efforts": efforts},
    )
    assert "effort" not in payload["output_config"]


def test_thinking_budget_tracks_clamped_output_limit():
    payload = ADAPTER.NanoGptAdapter._payload(
        {"max_tokens": 64000, "thinking": {"type": "enabled", "budget_tokens": 48000}},
        MODEL, {"max_output_tokens": 32768},
    )
    assert payload["max_tokens"] == 32768
    assert payload["thinking"]["budget_tokens"] == 32767


def test_disconnect_before_first_stream_chunk_closes_upstream(monkeypatch, tmp_path):
    responses = []

    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        response = httpx.Response(200, content=b"data: fixture\n\n", headers={"content-type": "text/event-stream"})
        responses.append(response)
        return response

    clients = _transport(monkeypatch, handler)

    async def run():
        response = await _adapter(tmp_path).handle_request(_request(stream=True))

        async def send(message):
            assert message["type"] == "http.response.start"
            raise OSError("fixture disconnect")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(Exception):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    asyncio.run(run())
    assert responses and all(response.is_closed for response in responses)
    assert all(client.is_closed for client in clients)


def test_streamed_tool_call_and_result_round_trip(monkeypatch, tmp_path):
    posts = []
    model_reply = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "nano-call-1", "name": "echo", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"text":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"fixture"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    wire = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in model_reply).encode()

    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
        assert posts[1]["messages"][-1]["content"][0]["tool_use_id"] == "nano-call-1"
        return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "fixture-finished"}]})

    clients = _transport(monkeypatch, handler)

    async def run():
        adapter = _adapter(tmp_path)
        response = await adapter.handle_request(_request(stream=True, tools=[{"name": "echo", "input_schema": {"type": "object"}}]))
        returned = b"".join([chunk async for chunk in response.body_iterator])
        assert returned == wire
        partial = "".join(event["delta"]["partial_json"] for event in model_reply if event["type"] == "content_block_delta")
        invocation = {"type": "tool_use", "id": "nano-call-1", "name": "echo", "input": json.loads(partial)}
        messages = [
            {"role": "assistant", "content": [invocation]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "nano-call-1", "content": "fixture"}]},
        ]
        return await adapter.handle_request(_request(messages=messages))

    result = asyncio.run(run())
    assert b"fixture-finished" in result.body
    assert len(posts) == 2
    assert all(client.is_closed for client in clients)


def test_observation_contains_only_selection_metadata(monkeypatch, tmp_path, caplog):
    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        return httpx.Response(200, json={"type": "message", "content": []})
    _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request(
        output_config={"effort": "max"}, system="BODY_MUST_NOT_BE_LOGGED",
        messages=[{"role": "user", "content": "PRIVATE_SYNTHETIC_BODY"}])))
    assert response.status_code == 200
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "requested_effort" in logs and "sent_effort" in logs
    assert "unsupported_effort_in_subscription_catalog" in logs
    assert not any(text in logs for text in (TOKEN, "BODY_MUST_NOT_BE_LOGGED", "PRIVATE_SYNTHETIC_BODY",
                                            "authorization", "unrelated-proxy-secret"))


@pytest.mark.parametrize("efforts,expected", [(["low", "high"], "high"), (["low"], "low"), ([], None)])
def test_normal_conversation_effort_fallback_order_is_preserved(efforts, expected):
    source = {"model": "nanogpt/" + MODEL, "output_config": {"effort": "xhigh"}}
    payload = ADAPTER.NanoGptAdapter._payload(source, MODEL, {"reasoning_efforts": efforts})
    assert payload["output_config"].get("effort") == expected
    observation = ADAPTER.NanoGptAdapter._selection_observation(source, payload)
    assert observation["sent_effort"] == (expected or "unspecified")


@pytest.mark.parametrize("failure", ["usage_http", "usage_invalid", "catalog_http", "catalog_invalid"])
def test_unavailable_policy_or_catalog_never_posts(monkeypatch, tmp_path, failure):
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(500)
        usage = request.url.path.endswith("usage")
        if failure == ("usage_http" if usage else "catalog_http"):
            return httpx.Response(503)
        if failure == ("usage_invalid" if usage else "catalog_invalid"):
            return httpx.Response(200, content=b"invalid")
        return httpx.Response(200, json=_policy() if usage else _catalog())
    clients = _transport(monkeypatch, handler)
    response = asyncio.run(_adapter(tmp_path).handle_request(_request()))
    assert response.status_code == 503 and posts == []
    assert all(client.is_closed for client in clients)


def test_stream_notice_preserves_tool_indices_and_cleanup(monkeypatch, tmp_path):
    events = [
        {"type": "message_start", "message": {"id": "synthetic", "type": "message", "role": "assistant",
         "model": MODEL, "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "synthetic-tool", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"synthetic.txt"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    wire = "".join("event: " + e["type"] + "\r\ndata: " + json.dumps(e) + "\r\n\r\n" for e in events).encode()
    def handler(request):
        if request.url.path.endswith("usage"):
            return httpx.Response(200, json=_policy())
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
    clients = _transport(monkeypatch, handler)
    async def consume():
        response = await _adapter(tmp_path).handle_request(_request(stream=True, output_config={"effort": "max"}))
        return response, b"".join([chunk async for chunk in response.body_iterator])
    response, content = asyncio.run(consume())
    output = [json.loads(line[6:]) for line in content.decode().splitlines() if line.startswith("data: ")]
    starts = [e for e in output if e["type"] == "content_block_start"]
    assert [e["index"] for e in starts] == [0, 1]
    assert starts[0]["content_block"]["type"] == "text"
    assert starts[1]["content_block"] == events[1]["content_block"]
    assert any(e.get("index") == 1 and e.get("delta") == events[2]["delta"] for e in output)
    assert b"effort adjusted: max -> high" in content
    assert output[-2:] == events[-2:] and response.closed
    assert all(client.is_closed for client in clients)
