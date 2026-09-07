"""Portable MCP configuration blocks and explicit trust-boundary guidance."""

from __future__ import annotations

from typing import Any

GUIDANCE = (
    "Native IDE tools are unmanaged and make learning ineligible; "
    "use stackmind.* MCP tools for governed, traceable work."
)


def _server(command: str, arguments: list[str] | None = None) -> dict[str, Any]:
    return {"command": command, "args": arguments or [], "env": {"STACKMIND_MODE": "governed"}}


def cursor_config(
    command: str = "stackmind-mcp", arguments: list[str] | None = None
) -> dict[str, Any]:
    return {"_comment": GUIDANCE, "mcpServers": {"stackmind": _server(command, arguments)}}


def antigravity_config(
    command: str = "stackmind-mcp", arguments: list[str] | None = None
) -> dict[str, Any]:
    return {"_comment": GUIDANCE, "mcp": {"servers": {"stackmind": _server(command, arguments)}}}


def vscode_config(
    command: str = "stackmind-mcp", arguments: list[str] | None = None
) -> dict[str, Any]:
    return {"_comment": GUIDANCE, "mcp": {"servers": {"stackmind": _server(command, arguments)}}}


def claude_desktop_config(
    command: str = "stackmind-mcp", arguments: list[str] | None = None
) -> dict[str, Any]:
    return {"_comment": GUIDANCE, "mcpServers": {"stackmind": _server(command, arguments)}}
