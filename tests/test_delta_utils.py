"""Tests for services/adapters/delta_utils.py — delta accumulation logic.

These tests are standalone (no ccproxy dependency) since delta_utils.py
only uses standard library types.
"""

from __future__ import annotations

import pytest

from services.adapters.delta_utils import accumulate_delta, _accumulate_list


# --- accumulate_delta: basic types ---


class TestAccumulateDeltaBasic:
    def test_empty_delta_returns_accumulated(self):
        acc = {"text": "hello"}
        assert accumulate_delta(acc, {}) == {"text": "hello"}

    def test_empty_accumulated_returns_delta_copy(self):
        delta = {"text": "hello"}
        result = accumulate_delta({}, delta)
        assert result == {"text": "hello"}
        assert result is not delta  # must be a copy

    def test_new_key_added(self):
        acc = {"a": 1}
        result = accumulate_delta(acc, {"b": 2})
        assert result == {"a": 1, "b": 2}


# --- accumulate_delta: string concatenation ---


class TestAccumulateDeltaStrings:
    def test_strings_concatenated(self):
        result = accumulate_delta({"text": "hel"}, {"text": "lo"})
        assert result["text"] == "hello"

    def test_empty_string_concat(self):
        result = accumulate_delta({"text": "abc"}, {"text": ""})
        assert result["text"] == "abc"


# --- accumulate_delta: numeric addition ---


class TestAccumulateDeltaNumbers:
    def test_integers_added(self):
        result = accumulate_delta({"count": 5}, {"count": 3})
        assert result["count"] == 8

    def test_floats_added(self):
        result = accumulate_delta({"score": 1.5}, {"score": 2.5})
        assert result["score"] == pytest.approx(4.0)


# --- accumulate_delta: dict recursion ---


class TestAccumulateDeltaDictMerge:
    def test_nested_dicts_merged(self):
        acc = {"meta": {"a": "x"}}
        delta = {"meta": {"b": "y"}}
        result = accumulate_delta(acc, delta)
        assert result["meta"] == {"a": "x", "b": "y"}

    def test_deeply_nested_merge(self):
        acc = {"a": {"b": {"c": "1"}}}
        delta = {"a": {"b": {"d": "2"}}}
        result = accumulate_delta(acc, delta)
        assert result["a"]["b"] == {"c": "1", "d": "2"}


# --- accumulate_delta: preserved keys ---


class TestPreservedKeys:
    """id, index, type are overwritten rather than concatenated."""

    def test_id_overwritten_not_concatenated(self):
        result = accumulate_delta({"id": "old"}, {"id": "new"})
        assert result["id"] == "new"

    def test_index_overwritten(self):
        result = accumulate_delta({"index": 0}, {"index": 1})
        assert result["index"] == 1

    def test_type_overwritten(self):
        result = accumulate_delta({"type": "text"}, {"type": "tool_use"})
        assert result["type"] == "tool_use"

    def test_none_delta_preserves_current(self):
        result = accumulate_delta({"id": "keep"}, {"id": None})
        assert result["id"] == "keep"


# --- accumulate_delta: list handling ---


class TestAccumulateDeltaLists:
    def test_primitive_lists_concatenated(self):
        result = accumulate_delta({"tags": [1, 2]}, {"tags": [3]})
        assert result["tags"] == [1, 2, 3]

    def test_indexed_list_merged(self):
        acc = {"items": [{"index": 0, "text": "a"}]}
        delta = {"items": [{"index": 0, "text": "b"}]}
        result = accumulate_delta(acc, delta)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "ab"

    def test_indexed_list_new_index_appended(self):
        acc = {"items": [{"index": 0, "text": "a"}]}
        delta = {"items": [{"index": 1, "text": "b"}]}
        result = accumulate_delta(acc, delta)
        assert len(result["items"]) == 2

    def test_empty_delta_list(self):
        result = _accumulate_list([1, 2], [])
        assert result == [1, 2]

    def test_empty_current_list(self):
        result = _accumulate_list([], [3, 4])
        assert result == [3, 4]


# --- accumulate_delta: type mismatch fallback ---


class TestTypeMismatchFallback:
    def test_different_types_use_delta_value(self):
        result = accumulate_delta({"val": "string"}, {"val": 42})
        assert result["val"] == 42


# --- _accumulate_list: error case ---


class TestAccumulateListErrors:
    def test_indexed_dict_without_index_raises(self):
        """When a list has indexed objects, all dict items must have 'index'."""
        current = [{"index": 0, "text": "a"}]
        delta = [{"text": "b"}]  # missing 'index'
        with pytest.raises(ValueError, match="index"):
            _accumulate_list(current, delta)


# --- accumulate_delta: realistic tool-call delta scenario ---


class TestToolCallDeltaScenario:
    def test_tool_call_delta_accumulation(self):
        """Simulate typical OpenAI streaming tool call delta merging."""
        # First chunk: tool call starts
        acc = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "Read", "arguments": ""},
                }
            ]
        }
        # Second chunk: arguments appended
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": '{"file_path"'},
                }
            ]
        }
        result = accumulate_delta(acc, delta)
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_abc"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "Read"
        assert tc["function"]["arguments"] == '{"file_path"'

        # Third chunk: more arguments
        delta2 = {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": ': "/tmp/x"}'},
                }
            ]
        }
        result2 = accumulate_delta(result, delta2)
        assert result2["tool_calls"][0]["function"]["arguments"] == '{"file_path": "/tmp/x"}'
