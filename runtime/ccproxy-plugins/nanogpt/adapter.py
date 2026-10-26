from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ccproxy.services.adapters.base import BaseAdapter

from .credentials import read_key

logger = logging.getLogger(__name__)


class _ManagedStream(StreamingResponse):
    def __init__(self, upstream: httpx.Response, client: httpx.AsyncClient, *, notice: str = "", **kwargs: Any):
        self.upstream = upstream
        self.client = client
        self.closed = False
        self.notice = notice
        self.notice_sent = False
        self.notice_offset = 0
        super().__init__(self._chunks(), **kwargs)

    async def _close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.upstream.aclose()
            finally:
                await self.client.aclose()

    async def _chunks(self) -> AsyncIterator[bytes]:
        try:
            if self.notice:
                lines: list[str] = []
                async for line in self.upstream.aiter_lines():
                    if line:
                        lines.append(line)
                    elif lines:
                        for chunk in self._notice_frame(lines):
                            yield chunk
                        lines = []
                if lines:
                    for chunk in self._notice_frame(lines):
                        yield chunk
            else:
                async for chunk in self.upstream.aiter_bytes():
                    yield chunk
        finally:
            await self._close()

    def _notice_frame(self, lines: list[str]) -> list[bytes]:
        raw = ("\n".join(lines) + "\n\n").encode("utf-8")
        try:
            event = json.loads("\n".join(line[5:].lstrip(" ") for line in lines if line.startswith("data:")))
        except ValueError:
            return [raw]
        if not isinstance(event, dict):
            return [raw]

        def encode(item: dict[str, Any]) -> bytes:
            return ("event: " + item["type"] + "\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n").encode("utf-8")

        prefix = []
        if (not self.notice_sent and (
                event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") != "text"
                or event.get("type") == "message_delta")):
            self.notice_sent = True
            self.notice_offset = 1
            prefix = [encode({"type": "content_block_start", "index": 0,
                              "content_block": {"type": "text", "text": ""}}),
                      encode({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": self.notice}}),
                      encode({"type": "content_block_stop", "index": 0})]
        if event.get("type", "").startswith("content_block_") and isinstance(event.get("index"), int):
            event = {**event, "index": event["index"] + self.notice_offset}
            # Workflow returns the last text block. Keep the notice inside text
            # blocks so it survives that extraction rather than being discarded.
            if event["type"] == "content_block_start" and event.get("content_block", {}).get("type") == "text":
                self.notice_sent = True
                return [*prefix, encode(event), encode({
                    "type": "content_block_delta", "index": event["index"],
                    "delta": {"type": "text_delta", "text": self.notice}})]
            return [*prefix, encode(event)]
        if prefix:
            return [*prefix, raw]
        return [raw]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close()


class NanoGptAdapter(BaseAdapter):
    async def handle_request(self, request: Request) -> Response | StreamingResponse:
        try:
            data = json.loads(await request.body())
        except (ValueError, TypeError):
            return self._error(400, "リクエストのJSONが不正です。")
        if not isinstance(data, dict):
            return self._error(400, "リクエストはオブジェクトで指定してください。")
        selected = data.get("model")
        if not isinstance(selected, str):
            return self._error(400, "モデルIDが必要です。")
        model_id = selected.removeprefix("nanogpt/")
        allowed = {card.id for card in self.config.models_endpoint}
        if model_id not in allowed:
            return self._error(400, "登録されていないNanoGPTモデルです。")
        aliases = getattr(self.config, "upstream_aliases", {})
        model_id = aliases.get(model_id, model_id)
        try:
            api_key = self._api_key()
        except (OSError, ValueError, UnicodeError):
            return self._error(503, "暗号化APIキーを読み取れません。再登録してください。")
        if not api_key:
            return self._error(503, "NanoGPTのサブスク専用APIキーを登録してください。")

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        upstream: httpx.Response | None = None
        handed_off = False
        try:
            headers = {"authorization": f"Bearer {api_key}"}
            usage = await client.get(
                "https://nano-gpt.com/api/subscription/v1/usage", headers=headers,
            )
            if usage.status_code != 200:
                return self._error(503, "サブスクの課金方針を確認できないため送信しません。")
            try:
                usage_data = usage.json()
            except ValueError:
                return self._error(503, "サブスクの確認応答が不正なため送信しません。")
            policy_error = self.policy_error(usage_data)
            if policy_error:
                return self._error(403, policy_error)

            catalog = await client.get(
                "https://nano-gpt.com/api/subscription/v1/models?detailed=true",
                headers=headers,
            )
            if catalog.status_code != 200:
                return self._error(503, "サブスク対象モデルを確認できないため送信しません。")
            try:
                catalog_data = catalog.json()
            except ValueError:
                return self._error(503, "モデル一覧が不正なため送信しません。")
            model = self._catalog_model(catalog_data, model_id)
            if model is None:
                return self._error(403, "選択モデルのサブスク対象・1M文脈・ツール対応を確認できません。")
            try:
                payload = self._payload(data, model_id, model)
            except ValueError:
                return self._error(400, "会話内のsystem制御を対応形式へ変換できません。")
            observation = self._selection_observation(data, payload)
            agent_id = request.headers.get("x-claude-code-agent-id", "")
            if agent_id and len(agent_id) <= 80 and all(c.isascii() and (c.isalnum() or c in "_-") for c in agent_id):
                observation["agent_id"] = agent_id
            upstream_request = client.build_request(
                "POST", "https://nano-gpt.com/api/v1/messages",
                headers={**headers, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json=payload,
            )
            logger.warning("nanogpt_selection %s", json.dumps(observation, sort_keys=True))
            upstream = await client.send(upstream_request, stream=bool(payload.get("stream")))
            if upstream.status_code != 200:
                status = upstream.status_code if 400 <= upstream.status_code <= 599 else 502
                return self._error(status, f"上流がHTTP {upstream.status_code}を返しました。別経路へ切り替えません。")
            safe_headers = {
                name: value for name, value in upstream.headers.items()
                if name.lower() in {"request-id", "x-request-id", "retry-after"}
            }
            safe_headers.update({
                "x-claude-codex-requested-model": selected,
                "x-claude-codex-sent-model": model_id,
                "x-claude-codex-requested-effort": observation["requested_effort"],
                "x-claude-codex-sent-effort": observation["sent_effort"],
                "x-claude-codex-adjustment": ",".join(observation["reasons"]) or "unchanged",
            })
            notice = ""
            if observation["requested_effort"] != observation["sent_effort"]:
                notice = ("[claude-codex] NanoGPT effort adjusted: " + observation["requested_effort"]
                          + " -> " + observation["sent_effort"] + " (subscription catalog).\n")
            if payload.get("stream"):
                if "text/event-stream" not in upstream.headers.get("content-type", ""):
                    return self._error(502, "上流のストリーミング応答形式が不正です。")
                response = _ManagedStream(
                    upstream, client, notice=notice, media_type="text/event-stream", headers=safe_headers,
                )
                handed_off = True
                return response
            content = await upstream.aread()
            if notice:
                try:
                    message = json.loads(content)
                    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                        raise ValueError("unsupported response")
                    blocks = list(message["content"])
                    last_text = next((i for i in range(len(blocks) - 1, -1, -1)
                                      if isinstance(blocks[i], dict) and blocks[i].get("type") == "text"), None)
                    if last_text is None:
                        blocks.insert(0, {"type": "text", "text": notice})
                    else:
                        blocks[last_text] = {**blocks[last_text], "text": notice + blocks[last_text].get("text", "")}
                    message["content"] = blocks
                    content = json.dumps(message, ensure_ascii=False).encode("utf-8")
                except ValueError:
                    return self._error(502, "NanoGPT effort notice could not be attached to the response.")
            return Response(content=content, media_type="application/json", headers=safe_headers)
        except httpx.TimeoutException:
            return self._error(504, "接続がタイムアウトしました。別経路へ切り替えません。")
        except httpx.HTTPError:
            return self._error(503, "接続に失敗しました。別経路へ切り替えません。")
        finally:
            if not handed_off:
                if upstream is not None:
                    await upstream.aclose()
                await client.aclose()

    async def handle_streaming(self, request: Request, endpoint: str, **kwargs: Any) -> Response | StreamingResponse:
        return await self.handle_request(request)

    async def cleanup(self) -> None:
        return None

    def _api_key(self) -> str:
        key = os.environ.get("NANOGPT_API_KEY", "").strip()
        if key:
            return key
        path = Path(self.config.key_file).expanduser()
        return read_key(path) if path.exists() else ""

    @staticmethod
    def policy_error(payload: Any) -> str:
        routing = payload.get("routing") if isinstance(payload, dict) else None
        if not isinstance(routing, dict):
            return "課金方針が不明なため送信しません。"
        if routing.get("paidSpendPolicyAllowsBalance") is not False:
            return "従量課金を許可するキーです。NanoGPT側でSubscription onlyにしてください。"
        if routing.get("recommendedMode") != "subscription" or routing.get("subscriptionQuotaAvailable") is not True:
            return "サブスク利用枠を確認できません。枠切れや確認不能時は停止します。"
        return ""

    @staticmethod
    def _catalog_model(payload: Any, model_id: str) -> dict[str, Any] | None:
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return None
        for model in models:
            if not isinstance(model, dict) or model.get("id") != model_id:
                continue
            context = model.get("context_length")
            capabilities = model.get("capabilities")
            if (
                isinstance(context, int) and not isinstance(context, bool) and context >= 1_000_000
                and isinstance(capabilities, dict) and capabilities.get("tool_calling") is True
            ):
                return model
        return None

    @staticmethod
    def _normalize_inline_controls(payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        following_user = False
        normalized = []
        effort_found = False
        latest_effort = None
        for message in reversed(messages):
            if not isinstance(message, dict):
                normalized.append(message)
                continue
            role = message.get("role")
            if role == "user":
                following_user = True
            if role != "system":
                normalized.append(message)
                continue
            if set(message) - {"role", "content", "output_config", "cache_control", "clear_at"}:
                raise ValueError("unsupported_system_control")
            clear_at = message.get("clear_at")
            if clear_at not in (None, "next_user_message"):
                raise ValueError("unsupported_system_expiry")
            config = message.get("output_config", {})
            if not isinstance(config, dict) or set(config) - {"effort"}:
                raise ValueError("unsupported_inline_output_config")
            if clear_at == "next_user_message" and following_user:
                continue
            if "effort" in config and not effort_found:
                latest_effort = config["effort"]
                effort_found = True
            content = message.get("content")
            if content in (None, "", []):
                continue
            normalized.append({name: value for name, value in message.items() if name not in {"output_config", "clear_at"}})
        payload["messages"] = list(reversed(normalized))
        if effort_found:
            output_config = payload.get("output_config", {})
            if not isinstance(output_config, dict):
                raise ValueError("unsupported_output_config")
            payload["output_config"] = {**output_config, "effort": latest_effort}

    @staticmethod
    def _payload(data: dict[str, Any], model_id: str, model: dict[str, Any]) -> dict[str, Any]:
        accepted = {"messages", "system", "tools", "tool_choice", "stream", "max_tokens", "thinking", "output_config", "stop_sequences"}
        payload = {name: value for name, value in data.items() if name in accepted}
        payload["model"] = model_id
        NanoGptAdapter._normalize_inline_controls(payload)
        output_limit = model.get("max_output_tokens")
        if isinstance(output_limit, int) and not isinstance(output_limit, bool) and output_limit > 0:
            requested = payload.get("max_tokens")
            if isinstance(requested, int) and not isinstance(requested, bool) and requested > output_limit:
                payload["max_tokens"] = output_limit
        output_config = payload.get("output_config")
        efforts = model.get("reasoning_efforts")
        if not isinstance(efforts, list) or not all(isinstance(value, str) for value in efforts):
            efforts = []
        thinking = payload.get("thinking")
        maximum = payload.get("max_tokens")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            budget = thinking.get("budget_tokens")
            if isinstance(budget, int) and not isinstance(budget, bool) and isinstance(maximum, int) and budget >= maximum:
                payload["thinking"] = {**thinking, "budget_tokens": max(1, maximum - 1)}
        if isinstance(output_config, dict):
            output_config = dict(output_config)
            effort = output_config.get("effort")
            if effort is not None and effort not in efforts:
                if "high" in efforts:
                    output_config["effort"] = "high"
                elif "low" in efforts:
                    output_config["effort"] = "low"
                else:
                    output_config.pop("effort", None)
            payload["output_config"] = output_config
        return payload

    @staticmethod
    def _selection_observation(data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data)
        NanoGptAdapter._normalize_inline_controls(normalized)

        def effort(value: Any) -> str:
            config = value.get("output_config")
            raw = config.get("effort") if isinstance(config, dict) else None
            if raw is None:
                return "unspecified"
            return raw if isinstance(raw, str) and raw in {"none", "low", "medium", "high", "xhigh", "max"} else "invalid"

        requested, sent = effort(normalized), effort(payload)
        reasons = []
        if data["model"].removeprefix("nanogpt/") != payload["model"]:
            reasons.append("upstream_model_alias")
        if requested != sent:
            reasons.append("unsupported_effort_in_subscription_catalog")
        return {"requested_model": data["model"], "sent_model": payload["model"],
                "requested_effort": requested, "sent_effort": sent, "reasons": reasons}

    @staticmethod
    def _error(status: int, message: str) -> Response:
        return Response(
            content=json.dumps({"type": "error", "error": {"type": "api_error", "message": "nanogpt: " + message}}, ensure_ascii=False),
            status_code=status, media_type="application/json",
        )
