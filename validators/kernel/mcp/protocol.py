"""MCP JSON-RPC 2.0 protocol implementation for a governed tool registry."""

from __future__ import annotations

import json
from typing import Any, TextIO

from .tools import GovernedToolRegistry


class McpProtocol:
    protocol_version = "2024-11-05"

    def __init__(
        self,
        tools: GovernedToolRegistry,
        server_name: str = "stackmind",
        server_version: str = "3.1.0",
    ) -> None:
        self.tools, self.server_name, self.server_version = tools, server_name, server_version

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(request_id, -32600, "Invalid Request")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
        try:
            result = self._dispatch(request["method"], params)
        except ValueError as error:
            return self._error(request_id, -32602, str(error))
        except Exception as error:
            if request["method"] == "tools/call":
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                return self._error(request_id, -32603, str(error))
        if "id" not in request:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve_stdio(self, input_stream: TextIO, output_stream: TextIO) -> None:
        for line in input_stream:
            try:
                response = self.handle(json.loads(line))
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error")
            if response is not None:
                output_stream.write(json.dumps(response) + "\n")
                output_stream.flush()

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": self.protocol_version,
                "serverInfo": {"name": self.server_name, "version": self.server_version},
                "capabilities": {"tools": {}},
            }
        if method == "tools/list":
            return {"tools": self.tools.definitions()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise ValueError("tool name must be a string")
            result = self.tools.call(name, params.get("arguments", {}))
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        if method == "ping":
            return {}
        raise ValueError("Method not found")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
