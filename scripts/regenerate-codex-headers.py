"""Regenerate ccproxy Codex headers cache for the current Codex CLI version.

ccproxy caches Codex CLI request headers at
`~/.cache/ccproxy/codex_headers_<VERSION>.json` and refuses to use a cache
whose version does not match the running Codex CLI. The built-in detection
spawns `codex exec` with `OPENAI_BASE_URL` redirected to a local capture
server, but that path no longer triggers a real HTTP request on Codex CLI
0.126+ (OAuth/ChatGPT auth bypasses `OPENAI_BASE_URL`), so detection raises
`RuntimeError: Failed to capture Codex CLI request` and ccproxy falls back to
a stale cache. The OpenAI Codex backend then rejects every model request
with `400: requires a newer version of Codex`.

Workaround: clone the most recent valid cache, bump the version-bearing
fields to the version of `codex --version`, and write the new file. ccproxy
loads the cache at startup based on filename match.

The version is read from `codex --version`, so this keeps working as Codex CLI
moves forward (0.126 -> 0.142.4 and beyond): current ccproxy still loads the header cache by
filename match, so the stale-cache / "requires a newer version of Codex" failure
mode is unchanged and this workaround still applies.

Usage:
    python scripts/regenerate-codex-headers.py
    python scripts/regenerate-codex-headers.py --target-version 0.142.4

After running, restart the proxy:
    claude-codex-proxy stop
    claude-codex-proxy start
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


CACHE_DIR = Path.home() / ".cache" / "ccproxy"
FILENAME_RE = re.compile(r"^codex_headers_(\d+\.\d+\.\d+)\.json$")


def detect_codex_version() -> str:
    out = subprocess.run(
        "codex --version", capture_output=True, text=True, check=True, shell=True
    ).stdout.strip()
    parts = out.split()
    return parts[-1] if parts else out


def find_latest_existing_cache() -> tuple[Path, str] | None:
    candidates: list[tuple[tuple[int, int, int], Path, str]] = []
    for entry in CACHE_DIR.glob("codex_headers_*.json"):
        m = FILENAME_RE.match(entry.name)
        if not m:
            continue
        version = m.group(1)
        try:
            key = tuple(int(p) for p in version.split("."))  # type: ignore[assignment]
        except ValueError:
            continue
        candidates.append((key, entry, version))  # type: ignore[arg-type]
    if not candidates:
        return None
    candidates.sort()
    _, path, version = candidates[-1]
    return path, version


def rewrite(data: dict, old_version: str, new_version: str) -> dict:
    data["codex_version"] = new_version
    headers = data.get("headers") or {}
    if "version" in headers:
        headers["version"] = new_version
    if "user-agent" in headers and isinstance(headers["user-agent"], str):
        headers["user-agent"] = headers["user-agent"].replace(old_version, new_version)
    data["headers"] = headers
    data["cached_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-version",
        help="Override the detected Codex CLI version (default: codex --version)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would happen without writing"
    )
    args = parser.parse_args()

    if not CACHE_DIR.exists():
        print(f"Cache dir not found: {CACHE_DIR}", file=sys.stderr)
        return 1

    target_version = args.target_version or detect_codex_version()

    found = find_latest_existing_cache()
    if not found:
        print(
            f"No existing codex_headers_*.json under {CACHE_DIR}. Start ccproxy at "
            "least once with a working Codex CLI to seed an initial cache.",
            file=sys.stderr,
        )
        return 1
    src_path, src_version = found

    if src_version == target_version:
        print(f"Cache already matches Codex CLI {target_version}: {src_path}")
        return 0

    dst_path = CACHE_DIR / f"codex_headers_{target_version}.json"
    if dst_path.exists():
        print(f"Target cache already exists: {dst_path}")
        return 0

    payload = json.loads(src_path.read_text(encoding="utf-8"))
    payload = rewrite(payload, src_version, target_version)

    print(f"Source : {src_path} (version {src_version})")
    print(f"Target : {dst_path} (version {target_version})")
    if args.dry_run:
        print("--dry-run set, not writing.")
        return 0

    dst_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("Written. Restart claude-codex-proxy to pick up the new cache.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
