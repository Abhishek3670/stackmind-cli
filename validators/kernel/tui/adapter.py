"""Presentation adapter: maps TUI commands to daemon calls and replays daemon events."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .client import DaemonClient
from .views import diff_viewer, verification_matrix


class StackMindTuiAdapter:
    def __init__(self, client: DaemonClient) -> None:
        self.client = client
        self._sequences: dict[str, int] = {}

    def command(self, text: str, **params: Any) -> Any:
        stripped = text.strip()
        command, _, argument = stripped.partition(" ")
        argument = argument.strip()
        if command == ":new":
            return self.client.create_session(**params)
        if command == ":status":
            target = argument or params.get("session_id")
            if not target:
                raise ValueError("session_id required for :status")
            return self.client.get_session(target)
        if command == ":resume":
            target = argument or params.get("session_id")
            if not target:
                raise ValueError("session_id required for :resume")
            return self.client.resume(target)
        if command == ":pause":
            target = argument or params.get("session_id")
            if not target:
                raise ValueError("session_id required for :pause")
            return self.client.pause(target)
        if command == ":cancel":
            target = argument or params.get("session_id")
            if not target:
                raise ValueError("session_id required for :cancel")
            return self.client.cancel(target)
        if command == ":diff":
            target_diff = params.get("diff", "No staged daemon diff has been published.")
            return diff_viewer(target_diff)
        if command == ":matrix":
            dims = params.get("dimensions", {
                "scope": True, "state": True, "ast": True, "behavioral": True,
                "security": True, "outcome": True,
            })
            return verification_matrix(dims)
        if command == ":events":
            target = argument or params.get("session_id")
            if not target:
                raise ValueError("session_id required for :events")
            after = self._sequences.get(target, 0)
            if hasattr(self.client, "events"):
                evs = self.client.events(target, after)
                for ev in evs:
                    if isinstance(ev, dict) and "sequence" in ev and isinstance(ev["sequence"], int):
                        self._sequences[target] = max(self._sequences.get(target, 0), ev["sequence"])
                return evs
            return list(self.stream(target))
        if command == ":approve":
            target = params.get("session_id")
            if not target:
                raise ValueError("session_id required for :approve")
            return self.decide(target, True, argument or "Approved by operator")
        if command == ":reject":
            target = params.get("session_id")
            if not target:
                raise ValueError("session_id required for :reject")
            return self.decide(target, False, argument or "Rejected by operator")
        if command == ":prompt":
            prompt = argument
        elif not command.startswith(":"):
            prompt = stripped
        else:
            raise ValueError("unknown TUI command")
        if "session_id" not in params:
            raise ValueError(
                "Prompts and tool execution are runtime-owned and require a runtime turn endpoint"
            )
        turn_params = {key: value for key, value in params.items() if key != "session_id"}
        return self.client.turn(params["session_id"], prompt, **turn_params)

    def stream(
        self,
        session_id: str,
        timeout: float | None = None,
        after: int | None = None,
        live: bool = False,
    ) -> Iterator[dict[str, Any]]:
        last_seq = self._sequences.get(session_id, 0) if after is None else after
        # Consume native SSE event streaming if available
        if hasattr(self.client, "stream_events"):
            try:
                for event in self.client.stream_events(session_id=session_id, after=last_seq, timeout=timeout):
                    if isinstance(event, dict) and event.get("_heartbeat"):
                        if live:
                            yield event
                        else:
                            break
                        continue
                    if isinstance(event, dict):
                        seq = event.get("sequence")
                        if isinstance(seq, int):
                            self._sequences[session_id] = max(self._sequences.get(session_id, 0), seq)
                    yield event
                return
            except Exception:
                # Fall back to polling client.events if SSE stream is interrupted or unsupported
                pass

        if hasattr(self.client, "events"):
            for event in self.client.events(session_id, last_seq):
                if isinstance(event, dict):
                    seq = event.get("sequence")
                    if isinstance(seq, int):
                        self._sequences[session_id] = max(self._sequences.get(session_id, 0), seq)
                yield event

    def decide(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        return self.client.approve(session_id, approved, reason)
