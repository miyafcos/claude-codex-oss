"""Deterministic, account-scoped identity from transport metadata, never text."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Mapping

ID_HEADERS = ("session-id", "x-claude-code-session-id", "session_id", "conversation_id")
AGENT_HEADERS = ("x-claude-code-agent-id", "x-claude-code-parent-session-id")
_ID = re.compile(r"[A-Za-z0-9._:/-]{1,256}\Z")


def canonical_session(headers: Mapping[str, str], principal: str) -> tuple[str, str]:
    """Explicit canonical > Claude session > legacy session > conversation.

    Agent/parent metadata further separates children sharing a root session.
    Missing IDs get a per-request identity: no cross-conversation guessing.
    Accounts lacking a stable profile use a token digest supplied by the caller,
    conservatively invalidating affinity on credential rotation.
    """
    lowered: dict[str, str] = {}
    for key, value in headers.items():
        key = key.lower()
        if key not in ID_HEADERS + AGENT_HEADERS:
            continue
        if value == "":
            continue
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("Invalid session identity header")
        if key in lowered and lowered[key] != value:
            raise ValueError("Conflicting session identity headers")
        lowered[key] = value
    source = next((name for name in ID_HEADERS if name in lowered), "request-random")
    identity = lowered.get(source, str(uuid.uuid4()))
    material = ["codex-session-v1", principal, identity]
    material.extend(lowered.get(name, "") for name in AGENT_HEADERS)
    digest = hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
    return "ccproxy-v1-" + digest, source
