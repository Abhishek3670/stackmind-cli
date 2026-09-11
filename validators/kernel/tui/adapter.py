"""Presentation adapter: maps TUI commands to daemon calls and replays daemon events."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .client import DaemonClient


class StackMindTuiAdapter:
    def __init__(self, client: DaemonClient) -> None:
        self.client = client
        self._sequences: dict[str, int] = {}

    def command(self, text: str, **params: Any) -> Any:
        command, _, argument = text.partition(" ")
        if command == ":new":
            return self.client.create_session(**params)
        if command == ":resume":
            return self.client.get_session(argument or params["session_id"])
        if command == ":pause":
            return self.client.pause(argument or params["session_id"])
        if command == ":cancel":
            return self.client.cancel(argument or params["session_id"])
        raise ValueError(
            "Prompts and tool execution are runtime-owned and require a runtime turn endpoint"
        )

    def stream(self, session_id: str) -> Iterator[dict[str, Any]]:
        after = self._sequences.get(session_id, 0)
        for event in self.client.stream_events(session_id, after):
            self._sequences[session_id] = event["sequence"]
            yield event

    def decide(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        return self.client.approve(session_id, approved, reason)
