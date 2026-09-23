"""Convenience MCP server with stdio and optional LocalDaemon HTTP dispatch."""

from __future__ import annotations

import sys
from typing import TextIO

from .constants import MCP_PROTOCOL_VERSION
from .protocol import McpProtocol
from .tools import GovernedToolRegistry


class McpServer:
    protocol_version = MCP_PROTOCOL_VERSION

    def __init__(self, tools: GovernedToolRegistry) -> None:
        self.protocol = McpProtocol(tools)

    def handle(self, request: dict) -> dict | None:
        return self.protocol.handle(request)

    def serve_stdio(
        self, input_stream: TextIO | None = None, output_stream: TextIO | None = None
    ) -> None:
        self.protocol.serve_stdio(input_stream or sys.stdin, output_stream or sys.stdout)
