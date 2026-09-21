"""Background HTTP server hosting the daemon JSON-RPC protocol."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

import sys
from .manager import SessionManager
from .protocol import JsonRpcProtocol
from .storage import DaemonStorage


class _DaemonHTTPServer(ThreadingHTTPServer):
    """Threading HTTPServer that gracefully ignores normal client disconnect errors."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, _, _ = sys.exc_info()
        if exc_type is not None and issubclass(exc_type, (ConnectionError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class LocalDaemon:
    def __init__(
        self,
        state_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = 0,
        mcp_protocol: Any | None = None,
        runner_factory: Any | None = None,
    ) -> None:
        self.manager = SessionManager(DaemonStorage(state_dir), runner_factory=runner_factory)
        self.protocol = JsonRpcProtocol(self.manager)
        self.mcp_protocol = mcp_protocol
        self._server = _DaemonHTTPServer((host, port), self._handler())
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[:2]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: Any) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(
                        200,
                        {
                            "status": "ok",
                            "sessions": len(daemon.manager.list_sessions()),
                        },
                    )
                elif parsed.path == "/events":
                    query = parse_qs(parsed.query)
                    session_id = query.get("session_id", [None])[0]
                    try:
                        after = int(query.get("after", ["0"])[0])
                    except ValueError:
                        self._send(400, {"error": "after must be an integer"})
                        return
                    self._stream_events(session_id, after)
                else:
                    self._send(404, {"error": "not found"})

            def _stream_events(self, session_id: str | None, after: int) -> None:
                live_events: Queue[Any] = Queue()
                unsubscribe = daemon.manager.events.subscribe(live_events.put)
                last_sequence = after
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    for event in daemon.manager.events.events(session_id, after):
                        self._emit_sse(event)
                        last_sequence = event.sequence

                    while True:
                        try:
                            event = live_events.get(timeout=0.25)
                        except Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if event.sequence <= last_sequence:
                            continue
                        if session_id is not None and event.session_id != session_id:
                            continue
                        self._emit_sse(event)
                        last_sequence = event.sequence
                except (ConnectionError, BrokenPipeError):
                    return
                finally:
                    unsubscribe()

            def _emit_sse(self, event: Any) -> None:
                payload = json.dumps(event.as_dict(), separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {"/rpc", "/mcp"}:
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request = json.loads(self.rfile.read(length))
                except (ValueError, json.JSONDecodeError):
                    self._send(400, {"error": "invalid json"})
                    return
                protocol = daemon.mcp_protocol if self.path == "/mcp" else daemon.protocol
                if protocol is None:
                    self._send(404, {"error": "MCP is not configured"})
                    return
                response = protocol.handle(request)
                if response is None:
                    self._send(202, {})
                else:
                    self._send(200, response)

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def start(self) -> "LocalDaemon":
        if self._thread is None:
            self._thread = Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "LocalDaemon":
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()
