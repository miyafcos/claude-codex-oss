"""Bounded opaque reasoning references and capability-gated tool additions.

Only the local state store contains encrypted provider data. Claude receives an
unguessable reference, never the encrypted value or a textual representation.
"""
from __future__ import annotations

import copy
from contextvars import ContextVar
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

from .bridge_metadata import digest, file_lock

SIGNATURE_PREFIX = 'ccproxy-openai-replay-v2:'
LEGACY_PREFIX = 'ccproxy-openai-replay-v1:'
STATE_ENV = 'CCPROXY_REPLAY_STATE_DIR'
TTL_SECONDS = 6 * 60 * 60
SLOTS = 64
MAX_RECORD_BYTES = 512 * 1024
MAX_ITEM_BYTES = 128 * 1024
MAX_ITEMS = 16
# No wire acceptance receipt exists for this dispatch. Do not infer capability
# from a model name or enable it with an unverified environment flag.
VERIFIED_ADDITIONAL_TOOLS_MODELS: frozenset[str] = frozenset()
_TURN: ContextVar[Any] = ContextVar('codex_replay_turn', default=None)


def reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith((SIGNATURE_PREFIX, LEGACY_PREFIX))


def clean_reasoning(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        return []
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'reasoning':
            return []
        encrypted = item.get('encrypted_content')
        identifier = item.get('id')
        if not isinstance(encrypted, str) or not 0 < len(encrypted) <= MAX_ITEM_BYTES or not re.fullmatch(r'[A-Za-z0-9_+=/.-]+', encrypted):
            return []
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', identifier):
            return []
        if identifier in seen:
            return []
        seen.add(identifier)
        result.append({'type': 'reasoning', 'id': identifier, 'summary': [], 'encrypted_content': encrypted})
    return result


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _slot(self, handle: str) -> Path:
        return self.directory / f'state-{int(handle[:8], 16) % SLOTS:02d}.json'

    def _write(self, path: Path, raw: bytes) -> None:
        temporary = path.with_suffix('.pending')
        temporary.write_bytes(raw)
        os.replace(temporary, path)

    def put(self, record: dict[str, Any]) -> str | None:
        handle = secrets.token_hex(32)
        stored = dict(record, handle=handle, expires_at=time.time()+TTL_SECONDS, version=2)
        try:
            raw = json.dumps(stored, separators=(',', ':'), ensure_ascii=True).encode()
        except (ValueError, TypeError, RecursionError):
            return None
        if len(raw) > MAX_RECORD_BYTES:
            return None
        path = self._slot(handle)
        try:
            with file_lock(path.with_suffix('.lock')):
                self._write(path, raw)
        except (OSError, TimeoutError):
            return None
        return SIGNATURE_PREFIX + handle + "." + digest(stored)

    def get(self, signature: str) -> dict[str, Any] | None:
        if not isinstance(signature, str) or not signature.startswith(SIGNATURE_PREFIX):
            return None
        token = signature[len(SIGNATURE_PREFIX):]
        if not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{64}', token):
            return None
        handle, expected_digest = token.split(".")
        path = self._slot(handle)
        try:
            with file_lock(path.with_suffix('.lock')):
                if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
                    return None
                record = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(record, dict) or record.get('handle') != handle or record.get('version') != 2:
                    return None
                # The trusted conversation reference commits to every field.
                # This detects store-only edits; not simultaneous transcript edits.
                if not secrets.compare_digest(digest(record), expected_digest):
                    return None
                expiry = record.get('expires_at')
                if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry < time.time() or expiry > time.time()+TTL_SECONDS+5:
                    return None
                if not isinstance(record.get('scope'), dict) or not isinstance(record.get('history_hash'), str):
                    return None
                if type(record.get('start')) is not int or type(record.get('end')) is not int or not 0 <= record['start'] <= record['end']:
                    return None
                if record.get('reasoning') != clean_reasoning(record.get('reasoning')):
                    return None
                tools = record.get('tools')
                if not isinstance(tools, dict) or not all(isinstance(tools.get(key), list) and all(isinstance(item, dict) for item in tools[key]) for key in ('initial', 'current')):
                    return None
                updates = tools.get('updates')
                if not isinstance(updates, list) or len(updates) > 64:
                    return None
                rebuilt = list(tools['initial'])
                last_anchor = 0
                for update in updates:
                    if not isinstance(update, dict) or type(update.get('anchor')) is not int or not last_anchor <= update['anchor'] <= record['end']:
                        return None
                    if not isinstance(update.get('tools'), list) or not update['tools'] or not all(isinstance(item, dict) for item in update['tools']):
                        return None
                    rebuilt.extend(update['tools'])
                    last_anchor = update['anchor']
                if rebuilt != tools['current']:
                    return None
                return record
        except (OSError, TimeoutError, ValueError, TypeError, RecursionError):
            return None


def public_items(items: list[Any]) -> list[dict[str, Any]]:
    return [item for item in items if isinstance(item, dict) and item.get('type') not in {'reasoning', 'additional_tools'}]


def append_only(old: Any, new: Any) -> bool:
    return isinstance(old, list) and isinstance(new, list) and len(new) >= len(old) and new[:len(old)] == old


def plan_tool_additions(payload: dict[str, Any], previous: dict[str, Any] | None, *, verified: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only exact append operations can preserve the initial tools prefix."""
    current = payload.get('tools') or []
    full_state = {'initial': current, 'current': current, 'updates': []}
    if not verified or not previous or not all(isinstance(t, dict) and t.get('type') == 'function' for t in current):
        return payload, full_state
    old = previous.get('tools', {})
    if not append_only(old.get('current'), current) or not isinstance(old.get('initial'), list) or not isinstance(old.get('updates'), list):
        return payload, full_state
    updates = copy.deepcopy(old['updates'])
    added = current[len(old['current']):]
    if added:
        updates.append({'anchor': previous['end'], 'tools': added})
    if len(updates) > 64:
        return payload, full_state
    body = dict(payload, tools=copy.deepcopy(old['initial']))
    result = []
    index = 0
    for item in payload.get('input') or []:
        if item.get('type') not in {'reasoning', 'additional_tools'}:
            for update in updates:
                if update.get('anchor') == index:
                    result.append({'type': 'additional_tools', 'role': 'developer', 'tools': copy.deepcopy(update['tools'])})
            index += 1
        result.append(item)
    for update in updates:
        if update.get('anchor') == index:
            result.append({'type': 'additional_tools', 'role': 'developer', 'tools': copy.deepcopy(update['tools'])})
    body['input'] = result
    from ccproxy.llms.formatters.anthropic_to_openai.compaction import maybe_attach_prompt_cache_key
    maybe_attach_prompt_cache_key(body)
    return body, {'initial': body['tools'], 'current': current, 'updates': updates}


def prepare_replay(payload: dict[str, Any], headers: dict[str, str], request_context: Any, *, enabled: bool) -> dict[str, Any]:
    """Resolve only our own references after account and wire session are known."""
    _TURN.set(None)
    raw = payload.get('input') or []
    if not isinstance(raw, list):
        return payload
    clean = [item for item in raw if not (isinstance(item, dict) and item.get('type') == 'reasoning' and reference(item.get('encrypted_content')))]
    body = dict(payload, input=clean)
    directory = os.getenv(STATE_ENV)
    request_id = getattr(request_context, 'request_id', None)
    if not enabled or not directory or not isinstance(request_id, str) or not headers.get('session-id') or not headers.get('authorization'):
        return body
    scope = {
        'provider': 'codex-chatgpt', 'model': body.get('model'),
        'session': digest(headers['session-id']),
        'principal': digest(headers.get('chatgpt-account-id') or headers['authorization']),
        'instructions': digest(body.get('instructions')),
    }
    store = StateStore(Path(directory))
    public = []
    candidates = []
    seen_refs = set()
    duplicate_refs = set()
    latest = None
    current_tools = body.get('tools') or []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'reasoning' and reference(item.get('encrypted_content')):
            signature = item['encrypted_content']
            if signature in seen_refs:
                duplicate_refs.add(signature)
                continue
            seen_refs.add(signature)
            record = store.get(signature)
            if record and record['scope'] == scope and record['end'] == len(public) and record['history_hash'] == digest(public) and append_only(record['tools'].get('current'), current_tools):
                candidates.append((signature, record))
        elif item.get('type') not in {'reasoning', 'additional_tools'}:
            public.append(item)
    invalid = {i for i, (signature, _) in enumerate(candidates) if signature in duplicate_refs}
    for i, (_, left) in enumerate(candidates):
        left_ids = {item['id'] for item in left['reasoning']}
        for j in range(i):
            right = candidates[j][1]
            right_ids = {item['id'] for item in right['reasoning']}
            overlaps = left['start'] < right['end'] and right['start'] < left['end']
            if left_ids & right_ids or overlaps or (left['start'], left['end']) == (right['start'], right['end']):
                invalid.update((i, j))
    replacements = {}
    for i, (_, record) in enumerate(candidates):
        if i not in invalid and isinstance(record.get('replay_items'), list):
            replacements[record['start']] = record
            latest = record
    # Replace only the verified turn span. The saved wire order may contain
    # multiple message boundaries hidden by the client's text-block grouping.
    rebuilt = []
    index = 0
    skip_until = -1
    for item in clean:
        if item.get('type') not in {'reasoning', 'additional_tools'}:
            record = replacements.get(index)
            if record is not None:
                rebuilt.extend(copy.deepcopy(record['replay_items']))
                skip_until = record['end']
            if index >= skip_until:
                rebuilt.append(item)
            index += 1
        else:
            rebuilt.append(item)
    record = replacements.get(index)
    if record is not None and record['start'] == record['end']:
        rebuilt.extend(copy.deepcopy(record['replay_items']))
    body['input'] = rebuilt
    body, tool_state = plan_tool_additions(body, latest, verified=body.get('model') in VERIFIED_ADDITIONAL_TOOLS_MODELS)
    includes = list(body.get('include') or [])
    if 'reasoning.encrypted_content' not in includes:
        includes.append('reasoning.encrypted_content')
    body['include'] = includes
    _TURN.set({'request_id': request_id, 'scope': scope, 'store': store, 'input': public, 'tools': tool_state})
    return body


def active_turn() -> Any:
    from ccproxy.core.request_context import RequestContext
    context = RequestContext.get_current()
    turn = _TURN.get()
    return turn if context is not None and turn is not None and turn['request_id'] == context.request_id else None


def abort_turn() -> None:
    _TURN.set(None)


def ordered_response_output(output: Any, events: list[Any]) -> list[dict[str, Any]] | None:
    """Use authoritative terminal order or complete indexed done items."""
    indexed = {}
    for event in events:
        if not isinstance(event, dict) or event.get('type') != 'response.output_item.done':
            continue
        index, item = event.get('output_index'), event.get('item')
        if type(index) is not int or index < 0 or not isinstance(item, dict):
            return None
        if index in indexed and indexed[index] != item:
            return None
        indexed[index] = item
    if isinstance(output, list) and output:
        known_ids = {item.get('id') for item in output if isinstance(item, dict) and (item.get('type') != 'reasoning' or item.get('encrypted_content'))}
        missing = [item for item in indexed.values() if item.get('type') == 'reasoning' and item.get('id') not in known_ids]
        if not missing:
            return output
    if indexed and set(indexed) == set(range(len(indexed))):
        return [indexed[i] for i in range(len(indexed))]
    return None


def preserve_buffer_reasoning(response: dict[str, Any], events: list[Any]) -> dict[str, Any]:
    if active_turn() is None:
        return response
    output = ordered_response_output(response.get('output'), events)
    if output is None:
        return dict(response, _bridge_replay_ambiguous=True)
    return dict(response, output=output)


def _public_shape(items: list[dict[str, Any]]) -> list[Any] | None:
    """Compare public text/tools while allowing adjacent message regrouping."""
    result: list[Any] = []
    for item in items:
        kind = item.get('type', 'message')
        if kind == 'reasoning':
            continue
        if kind == 'message' and item.get('role') == 'assistant':
            text = ''
            for part in item.get('content') or []:
                if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                    return None
                text += part['text']
            if result and result[-1][0] == 'text':
                result[-1][1] += text
            else:
                result.append(['text', text])
        elif kind == 'function_call':
            try:
                arguments = json.loads(item.get('arguments', '{}'))
            except (ValueError, TypeError, RecursionError):
                return None
            result.append(['tool', item.get('call_id'), item.get('name'), arguments])
        else:
            return None
    return result


def finish_reply(content: list[dict[str, Any]], reasoning: list[dict[str, Any]], model: str | None,
                 *, output: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    turn = active_turn()
    abort_turn()
    from ccproxy.core.request_context import RequestContext
    context = RequestContext.get_current()
    metadata = getattr(context, 'metadata', {})
    if metadata.get('_bridge_provider_seen'):
        model = metadata.get('_bridge_served_model')
    if not turn or model != turn['scope']['model'] or not isinstance(output, list):
        return None
    kept = clean_reasoning(reasoning)
    if reasoning and not kept:
        return None
    if not kept and model not in VERIFIED_ADDITIONAL_TOOLS_MODELS:
        return None
    from ccproxy.llms.models import anthropic as anthropic_models
    from ccproxy.llms.formatters.anthropic_to_openai.requests import normalize_request_for_validation, _build_responses_input_items_from_anthropic_messages
    normalized = normalize_request_for_validation({'model': model, 'messages': [{'role': 'assistant', 'content': content}]})
    message = anthropic_models.Message.model_validate(normalized['messages'][0])
    reply = public_items(_build_responses_input_items_from_anthropic_messages([message]))
    shape = _public_shape(reply)
    if shape is None or shape != _public_shape(output):
        return None
    output_reasoning = [item for item in output if item.get('type') == 'reasoning']
    if clean_reasoning(output_reasoning) != kept:
        return None
    replay_items = []
    for item in output:
        if item.get('type') == 'reasoning':
            replay_items.extend(clean_reasoning([item]))
        elif item.get('type') == 'message':
            replay_items.append({'type': 'message', 'role': 'assistant', 'content': copy.deepcopy(item['content'])})
        elif item.get('type') == 'function_call':
            replay_items.append({key: item[key] for key in ('type', 'id', 'call_id', 'name', 'arguments') if key in item})
        else:
            return None
    history = turn['input'] + reply
    signature = turn['store'].put({
        'scope': turn['scope'], 'start': len(turn['input']), 'end': len(history),
        'history_hash': digest(history), 'reasoning': kept, 'tools': turn['tools'],
        'replay_items': replay_items,
    })
    return {'type': 'thinking', 'thinking': '', 'signature': signature} if signature else None
