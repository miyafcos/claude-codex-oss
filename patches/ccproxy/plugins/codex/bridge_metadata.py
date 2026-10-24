"""Allowlisted, bounded metadata; never serialize request/response bodies."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import asyncio
import threading
from typing import Any

MAX_LOG_BYTES = 8 * 1024 * 1024
# Existing compat rotator triggers strictly above 64 MiB. Leave bounded
# headroom so a file stopped by this writer remains eligible for rotation.
COMPAT_MAX_LOG_BYTES = 65 * 1024 * 1024
_IO_SLOTS = threading.BoundedSemaphore(2)


async def offload_metadata(callback, *args, **kwargs):
    """At most two submitted jobs, no waiting task queue; fail open at capacity."""
    if not _IO_SLOTS.acquire(blocking=False):
        import logging
        logging.getLogger(__name__).warning("bridge_metadata_capacity_reached")
        return False

    def run():
        try:
            return callback(*args, **kwargs)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("bridge_metadata_sink_unavailable")
            return False
        finally:
            # Release in the worker even if the awaiting request is cancelled.
            _IO_SLOTS.release()

    try:
        future = asyncio.create_task(asyncio.to_thread(run))
    except BaseException:
        _IO_SLOTS.release()
        raise
    return await asyncio.shield(future)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@contextmanager
def file_lock(path: Path):
    """Bounded inter-process lock. The small lock file is intentionally retained."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 1
        while True:
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Metadata lock unavailable") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _sink_status(path: Path, state: str, limit: int) -> None:
    # One bounded sidecar per configured sink; no message, path, or identifiers.
    try:
        status = path.with_name(path.name + ".status.json")
        if state != "ok" or status.exists():
            status.write_text(json.dumps({"schema": 1, "state": state, "limit_bytes": limit}), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        # A read-only directory cannot persist a sidecar either. Emit a fixed
        # structural diagnostic instead of logging the failed path or payload.
        import logging
        logging.getLogger(__name__).warning("bridge_metadata_sink_unavailable")


def append_bounded(path: Path, record: dict[str, Any], limit: int = MAX_LOG_BYTES) -> bool:
    try:
        encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        with file_lock(path.with_name(path.name + ".lock")):
            if (path.stat().st_size if path.exists() else 0) + len(encoded) > limit:
                _sink_status(path, "saturated", limit)
                return False
            with path.open("ab") as handle:
                handle.write(encoded)
            _sink_status(path, "ok", limit)
        return True
    except (OSError, TimeoutError, ValueError, TypeError, RecursionError):
        _sink_status(path, "unavailable", limit)
        return False


def metadata_record(phase: str, request_id: Any, payload: Any, context: Any = None, *, protocol: str = "openai.responses", diagnostics: Any = None) -> dict[str, Any]:
    from ccproxy.llms.formatters.usage_contract import normalize_openai_usage, normalize_anthropic_usage

    record: dict[str, Any] = {
        "schema": 1, "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase if re.fullmatch(r"[a-z_]{1,80}", phase) else "unknown",
        "request_id_hash": digest(str(request_id)) if request_id is not None else None,
        "protocol": protocol if protocol in {"openai.responses", "anthropic.messages"} else "unknown",
        "usage_basis": "inclusive_input_output",
    }
    if isinstance(diagnostics, dict):
        for key in ("status_code", "actual_tokens", "limit_tokens"):
            value = diagnostics.get(key)
            if type(value) is int and 0 <= value <= 10**12:
                record[key] = value
        for key in ("mode", "adapter_type", "limit_source", "actual_source"):
            value = diagnostics.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", value):
                record[key] = value
    if not isinstance(payload, dict):
        return record
    response = payload.get("response") or payload.get("message")
    data = response if isinstance(response, dict) else payload
    event_type = payload.get("type")
    if isinstance(event_type, str) and event_type.startswith(("response.", "message_")) and re.fullmatch(r"[a-z_.]{1,80}", event_type):
        record["event_type"] = event_type
    if data.get("id") is not None:
        record["response_id_hash"] = digest(str(data["id"]))
    model = data.get("model")
    if isinstance(model, str) and re.fullmatch(r"(?:gpt|o[1-9])[a-zA-Z0-9._-]{0,80}", model):
        record["model"] = model
    effort = (data.get("reasoning") or {}).get("effort") if isinstance(data.get("reasoning"), dict) else None
    if isinstance(effort, str) and effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        record["effort"] = effort
    if "instructions" in data or "tools" in data:
        tools = data.get("tools") or []
        tools_hash = digest(tools)
        # A metadata-only fingerprint of component digests, NOT the upstream
        # compute_prefix_hash or prompt_cache_key. Serialize tools once here.
        record.update(metadata_prefix_hash=digest([digest(data.get("instructions")), tools_hash]), tools_hash=tools_hash, tool_count=len(tools) if isinstance(tools, list) else 0)
    if isinstance(data.get("prompt_cache_key"), str):
        record["prompt_cache_key_hash"] = digest(data["prompt_cache_key"])
    if "usage" in data:
        try:
            normalizer = normalize_anthropic_usage if protocol == "anthropic.messages" else normalize_openai_usage
            record["usage"] = normalizer(data.get("usage"))
        except ValueError:
            record["usage"] = {"status": "invalid"}
    items = data.get("input")
    if isinstance(items, list):
        record["input_items"] = len(items)
        record["has_compaction"] = any(isinstance(x, dict) and x.get("type") in {"compaction", "context_compaction"} for x in items)
    if isinstance(context, dict):
        for key in ("session_hash", "principal_hash", "trace_id_hash"):
            value = context.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                record[key] = value
        source = context.get("session_source")
        if source in {"session-id", "x-claude-code-session-id", "session_id", "conversation_id", "request-random"}:
            record["session_source"] = source
    return record
