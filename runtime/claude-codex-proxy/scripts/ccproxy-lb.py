"""Small local reverse proxy for multiple ccproxy workers.

Claude Code can only target one Anthropic base URL per process. ccproxy-api
does not expose a worker-count flag, so claude-codex starts several ccproxy
servers on backend ports and keeps the public URL stable by placing this
round-robin proxy on 127.0.0.1:4026.
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

MAX_UPSTREAM_RETRIES = 2
RETRYABLE_UPSTREAM_ERRORS = (
    urllib.error.URLError,
    ConnectionError,
    http.client.IncompleteRead,
    socket.timeout,
    http.client.RemoteDisconnected,
)


class EmptyUpstreamResponse(ConnectionError):
    """Raised when a successful upstream response has no body."""


class BackendPicker:
    """Thread-safe round-robin backend selector."""

    def __init__(self, backends: Iterable[str]) -> None:
        normalized = [backend.rstrip("/") for backend in backends if backend.strip()]
        if not normalized:
            raise ValueError("at least one backend is required")
        self._cycle = itertools.cycle(normalized)
        self._lock = threading.Lock()

    def next_backend(self) -> str:
        with self._lock:
            return next(self._cycle)


class LoadBalancerHandler(BaseHTTPRequestHandler):
    """Forward HTTP requests to the next ccproxy backend."""

    picker: BackendPicker
    upstream_timeout: float
    protocol_version = "HTTP/1.0"
    server_version = "claude-codex-lb/1.0"
    _response_started = False

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward(send_body=False)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def _forward(self, *, send_body: bool = True) -> None:
        self._response_started = False
        body = self._read_body()
        headers = self._forward_headers()
        last_error: Exception | None = None

        max_retries = (
            MAX_UPSTREAM_RETRIES
            if self.command in {"GET", "HEAD", "OPTIONS"}
            else 0
        )
        for attempt in range(max_retries + 1):
            backend = self.picker.next_backend()
            target_url = urllib.parse.urljoin(backend + "/", self.path.lstrip("/"))
            request = urllib.request.Request(
                target_url,
                data=body,
                headers=headers,
                method=self.command,
            )

            try:
                with urllib.request.urlopen(
                    request, timeout=self.upstream_timeout
                ) as response:
                    self._send_upstream_response(
                        response, send_body=send_body, backend=backend
                    )
                return
            except urllib.error.HTTPError as exc:
                try:
                    with exc:
                        self._send_upstream_response(
                            exc, send_body=send_body, backend=backend
                        )
                except Exception as transfer_error:
                    self._send_gateway_error(_exception_summary(transfer_error))
                return
            except RETRYABLE_UPSTREAM_ERRORS as exc:
                last_error = exc
                if self._response_started or attempt == max_retries:
                    break
            except Exception as exc:
                self._send_gateway_error(_exception_summary(exc))
                return

        if last_error is not None:
            self._send_gateway_error(_exception_summary(last_error))

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _forward_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() == "host":
                continue
            headers[key] = value
        return headers

    def _send_upstream_response(
        self,
        response: urllib.request.addinfourl | urllib.error.HTTPError,
        *,
        send_body: bool,
        backend: str,
    ) -> None:
        status = int(getattr(response, "status", getattr(response, "code", 502)))
        if not send_body:
            self._start_upstream_response(response, status)
            return

        first_chunk = response.read(1)
        if not first_chunk and 200 <= status < 300:
            raise EmptyUpstreamResponse(f"upstream returned empty HTTP {status} body")

        self._start_upstream_response(response, status)
        if not first_chunk:
            return

        content_type = response.headers.get("Content-Type", "").lower()
        bytes_forwarded = 0
        try:
            self.wfile.write(first_chunk)
            self.wfile.flush()
            bytes_forwarded += len(first_chunk)
            while True:
                try:
                    chunk = response.read(65536)
                except http.client.IncompleteRead as exc:
                    if exc.partial:
                        self.wfile.write(exc.partial)
                        self.wfile.flush()
                        bytes_forwarded += len(exc.partial)
                    raise
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                bytes_forwarded += len(chunk)
        except Exception as exc:
            if "text/event-stream" in content_type:
                self._send_sse_error(exc)
            summary = _exception_summary(exc)
            sys.stderr.write(
                "upstream stream failed: "
                f"type={type(exc).__name__} bytes={bytes_forwarded} "
                f"path={self.path} backend={backend} error={summary}\n"
            )

    def _start_upstream_response(
        self,
        response: urllib.request.addinfourl | urllib.error.HTTPError,
        status: int,
    ) -> None:
        self._response_started = True
        self.send_response(status)
        for key, value in response.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS:
                continue
            if lower == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_sse_error(self, exc: Exception) -> None:
        payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": _exception_summary(exc),
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        block = f"event: error\ndata: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(block)
            self.wfile.flush()
        except OSError:
            pass

    def _send_gateway_error(self, message: str) -> None:
        if self._response_started:
            return
        payload = json.dumps(
            {"error": {"type": "bad_gateway", "message": message}},
            ensure_ascii=True,
        ).encode("utf-8")
        self._response_started = True
        self.send_response(502)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


def _exception_summary(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return message or type(exc).__name__


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=4026)
    parser.add_argument("--backend", action="append", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    LoadBalancerHandler.picker = BackendPicker(args.backend)
    LoadBalancerHandler.upstream_timeout = args.timeout

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), LoadBalancerHandler)
    backends = ", ".join(args.backend)
    print(
        f"ccproxy-lb listening on http://{args.listen_host}:{args.listen_port} "
        f"with backends: {backends}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
