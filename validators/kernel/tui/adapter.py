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
            if hasattr(self.client, "resume"):
                try:
                    return self.client.resume(target)
                except Exception:
                    pass
            if hasattr(self.client, "get_session"):
                try:
                    return self.client.get_session(target)
                except Exception:
                    pass
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
            clean_reason = argument or "Approved by operator"
            if hasattr(self.client, "run_approve"):
                try:
                    run_info = self.client.run_get(target) if hasattr(self.client, "run_get") else None
                    if isinstance(run_info, dict) and run_info.get("phase") in {
                        "AWAITING_APPROVAL", "INIT", "PLANNING", "planning", "awaiting_approval"
                    }:
                        return self.client.run_approve(target, reason=clean_reason)
                except Exception:
                    pass
            if hasattr(self.client, "plan_get") and hasattr(self.client, "plan_approve"):
                try:
                    plan = self.client.plan_get(target)
                    if isinstance(plan, dict) and plan.get("state") == "AWAITING_APPROVAL":
                        plan_id = plan.get("plan_id", "PLAN-001")
                        return self.client.plan_approve(target, plan_id, reason=clean_reason)
                except Exception:
                    pass
            return self.decide(target, True, clean_reason)
        if command == ":reject":
            target = params.get("session_id")
            if not target:
                raise ValueError("session_id required for :reject")
            clean_reason = argument or "Rejected by operator"
            if hasattr(self.client, "run_reject"):
                try:
                    run_info = self.client.run_get(target) if hasattr(self.client, "run_get") else None
                    if isinstance(run_info, dict) and run_info.get("phase") in {
                        "AWAITING_APPROVAL", "INIT", "PLANNING", "planning", "awaiting_approval"
                    }:
                        return self.client.run_reject(target, reason=clean_reason)
                except Exception:
                    pass
            if hasattr(self.client, "plan_get") and hasattr(self.client, "plan_reject"):
                try:
                    plan = self.client.plan_get(target)
                    if isinstance(plan, dict) and plan.get("state") == "AWAITING_APPROVAL":
                        plan_id = plan.get("plan_id", "PLAN-001")
                        return self.client.plan_reject(target, plan_id, reason=clean_reason)
                except Exception:
                    pass
            return self.decide(target, False, clean_reason)
        turn_params = {key: value for key, value in params.items() if key != "session_id"}
        if command == ":prompt":
            prompt = argument
        elif command == ":goal":
            if not argument:
                raise ValueError("goal description required for :goal")
            prompt = argument
            # Convention note: Daemon kernel uses lowercase canonical role names ("architecture", "backend").
            # The TUI presentation layer (state.roles) maps these to Title-cased display keys ("Architecture", "Q/A").
            turn_params.setdefault("role", "architecture")
            turn_params.setdefault("agent_id", "claude")
            turn_params.setdefault("is_goal", True)
        elif not command.startswith(":"):
            prompt = stripped
        else:
            raise ValueError("unknown TUI command")
        if "session_id" not in params:
            raise ValueError(
                "Prompts and tool execution are runtime-owned and require a runtime turn endpoint"
            )
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
