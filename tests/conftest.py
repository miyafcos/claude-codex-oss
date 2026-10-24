"""Shared fixtures and helpers for claude-codex-hybrid tests.

Tests are split into two tiers:
  - Standalone tests (no ccproxy dependency): delta_utils, normalize, reasoning
  - Integration tests (require ccproxy models): message_convert, tool_result

Integration tests use pytest.importorskip to gracefully skip when ccproxy
is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add patches/ccproxy to the Python path so tests can import the patched modules
# without requiring a full ccproxy installation for standalone tests.
_PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches" / "ccproxy"
if str(_PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(_PATCHES_DIR))
