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
import traceback
from .manager import SessionManager
from .protocol import JsonRpcProtocol
from .storage import DaemonStorage


class _DaemonHTTPServer(ThreadingHTTPServer):
    """Threading HTTPServer that gracefully handles errors and isolates logs."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        state_dir: Path | None = None,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.state_dir = Path(state_dir) if state_dir is not None else None

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, _, _ = sys.exc_info()
        if exc_type is not None and issubclass(exc_type, (ConnectionError, BrokenPipeError)):
            return
        # Log to daemon.log in state_dir rather than blasting to sys.stderr,
        # which would corrupt the active TUI alternate screen buffer.
        if self.state_dir:
            try:
                log_file = self.state_dir / "daemon.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n--- Exception occurred during processing of request from {client_address} ---\n")
                    traceback.print_exc(file=f)
                return
            except Exception:
                pass
        import logging
        logging.getLogger("daemon").error("Exception during request from %s", client_address, exc_info=True)


class LocalDaemon:
    def __init__(
        self,
        state_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = 0,
        mcp_protocol: Any | None = None,
        runner_factory: Any | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if auth_token is None:
            token_file = self.state_dir / ".daemon_token"
            if token_file.is_file():
                self.auth_token = token_file.read_text(encoding="utf-8").strip()
            else:
                import secrets
                self.auth_token = secrets.token_hex(32)
                token_file.write_text(self.auth_token, encoding="utf-8")
                try:
                    import os
                    os.chmod(token_file, 0o600)
                except Exception:
                    pass
        else:
            self.auth_token = auth_token
        self.manager = SessionManager(DaemonStorage(state_dir), runner_factory=runner_factory)
        self.protocol = JsonRpcProtocol(self.manager, auth_token=self.auth_token)
        self.mcp_protocol = mcp_protocol
        self._server = _DaemonHTTPServer((host, port), self._handler(), state_dir=Path(state_dir))
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
                try:
                    encoded = json.dumps(body).encode("utf-8")
                except (MemoryError, OverflowError):
                    error_payload = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Response payload exceeded memory limits"},
                        "id": getattr(body, "get", lambda k, d=None: None)("id") if isinstance(body, dict) else None,
                    }
                    encoded = json.dumps(error_payload).encode("utf-8")
                    status = 500
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
                            "sessions": len(daemon.manager._sessions),
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
                auth_header = self.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:].strip()
                    if isinstance(request, dict) and isinstance(request.get("params"), dict):
                        request["params"].setdefault("auth_token", token)
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
