"""Incremental complete-frame SSE JSON decoding shared by streaming and buffer."""
from __future__ import annotations

import codecs
import json
import re
from typing import Any, Iterable, Iterator


class SSEJSONDecoder:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line = ""
        self._data: list[str] = []
        self._event = ""
        self._skip_lf = False

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        text = self._decoder.decode(chunk)
        if not text:
            return
        if self._skip_lf and text.startswith("\n"):
            text = text[1:]
        self._skip_lf = False
        start = 0
        for boundary in re.finditer(r"\r\n|\r|\n", text):
            line = self._line + text[start:boundary.start()]
            self._line = ""
            start = boundary.end()
            self._skip_lf = boundary.group() == "\r" and start == len(text)
            if not line:
                data, event = "\n".join(self._data), self._event
                self._data, self._event = [], ""
                if data and data != "[DONE]":
                    try:
                        value = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        if event and "type" not in value:
                            value["type"] = event
                        yield value
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    self._data.append(value)
                elif field == "event":
                    self._event = value
        self._line += text[start:]
        # An unterminated frame is deliberately never emitted at EOF.


def iter_sse_json(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    decoder = SSEJSONDecoder()
    for chunk in chunks:
        yield from decoder.feed(chunk)


async def observe_provider_event(context: Any, event: dict[str, Any]) -> None:
    """Call the opt-in observer before formatters/aliases. Telemetry is optional."""
    metadata = getattr(context, "metadata", None)
    observer = metadata.get("_bridge_provider_observer") if isinstance(metadata, dict) else None
    if isinstance(metadata, dict) and "bridge_identity" in metadata:
        response = event.get("response")
        if isinstance(response, dict):
            metadata["_bridge_provider_seen"] = True
            if isinstance(response.get("model"), str) and response["model"]:
                metadata["_bridge_served_model"] = response["model"]
    if callable(observer):
        try:
            await observer(event)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("bridge_metadata_observer_unavailable")


def accumulate_private(accumulator: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Validate before the upstream accumulator can log a raw validation error.

    Accounting remains in the original terminal payload, not typed defaults.
    Other accumulator protocols retain their existing behavior.
    """
    from ccproxy.llms.streaming.accumulators import ResponsesAccumulator, _RESPONSES_STREAM_EVENT_ADAPTER
    from pydantic import ValidationError
    if not isinstance(accumulator, ResponsesAccumulator):
        accumulator.accumulate(event_type, payload)
        return
    candidate = dict(payload)
    candidate.setdefault('sequence_number', 0)
    response = candidate.get('response')
    if isinstance(response, dict):
        response = dict(response)
        for key, value in {'created_at':0, 'model':'', 'output':[], 'parallel_tool_calls':False}.items():
            response.setdefault(key, value)
        if not response.get('status'):
            response['status'] = event_type.removeprefix('response.') if event_type in {'response.completed','response.incomplete','response.failed'} else 'in_progress'
        candidate['response'] = response
    try:
        event = _RESPONSES_STREAM_EVENT_ADAPTER.validate_python(candidate).root
    except (ValidationError, ValueError, TypeError, RecursionError):
        # Unknown event shapes cannot contribute to reconstruction. They remain
        # available to the original formatter and explicit terminal validation.
        return
    accumulator.accumulate(event_type, event)
