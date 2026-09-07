"""Background HTTP server hosting the daemon JSON-RPC protocol."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from .manager import SessionManager
from .protocol import JsonRpcProtocol
from .storage import DaemonStorage


class LocalDaemon:
    def __init__(
        self,
        state_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = 0,
        mcp_protocol: Any | None = None,
    ) -> None:
        self.manager = SessionManager(DaemonStorage(state_dir))
        self.protocol = JsonRpcProtocol(self.manager)
        self.mcp_protocol = mcp_protocol
        self._server = ThreadingHTTPServer((host, port), self._handler())
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
            def _send(self, status: int, body: Any) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._send(
                        200,
                        {
                            "status": "ok",
                            "sessions": len(daemon.manager.list_sessions()),
                        },
                    )
                else:
                    self._send(404, {"error": "not found"})

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
