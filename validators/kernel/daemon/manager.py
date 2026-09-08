"""Durable session lifecycle and cancellation coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from typing import Any
from uuid import uuid4

from .events import EventDispatcher, RuntimeEvent
from .storage import DaemonStorage

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionManager:
    """Owns daemon sessions, their audit journals, and active-operation cancellation."""

    def __init__(self, storage: DaemonStorage) -> None:
        self.storage = storage
        recovered = storage.load()
        self._sessions: dict[str, dict[str, Any]] = recovered["sessions"]
        self._active: dict[str, Event] = {}
        self.events = EventDispatcher(recovered.get("events", []), self._persist_event)
        for session in self._sessions.values():
            if session["state"] == "RUNNING":
                session["state"] = "WAITING"
                session["updated_at"] = _now()
                self.events.publish("session.recovered", session["session_id"], state="WAITING")
        self._save()

    def _persist_event(self, _: RuntimeEvent) -> None:
        self._save()

    def _save(self) -> None:
        self.storage.save({"sessions": self._sessions, "events": self.events.dump()})

    @staticmethod
    def _view(session: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in session.items() if key != "active_operation"}

    def create_session(
        self,
        agent: str,
        provider: str,
        contract: dict[str, Any],
        workspace: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not all(isinstance(value, str) and value for value in (agent, provider, workspace)):
            raise ValueError("agent, provider, and workspace are required")
        if not isinstance(contract, dict):
            raise ValueError("contract must be an object")
        identifier = session_id or str(uuid4())
        if identifier in self._sessions:
            raise ValueError("session already exists")
        session = {
            "session_id": identifier,
            "agent": agent,
            "provider": provider,
            "contract": contract,
            "workspace": workspace,
            "state": "RUNNING",
            "created_at": _now(),
            "updated_at": _now(),
            "journal": [],
            "active_operation": None,
        }
        self._sessions[identifier] = session
        self.events.publish("session.started", identifier, agent=agent, provider=provider)
        self.events.publish("attempt.started", identifier)
        self.events.publish("contract.loaded", identifier)
        self._save()
        return self._view(session)

    def get_session(self, session_id: str) -> dict[str, Any]:
        try:
            return self._view(self._sessions[session_id])
        except KeyError as error:
            raise KeyError("unknown session") from error

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._view(session) for session in self._sessions.values()]

    def _set_state(self, session_id: str, state: str, event: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown session")
        if session["state"] in _TERMINAL:
            raise ValueError("session is terminal")
        session["state"] = state
        session["updated_at"] = _now()
        self.events.publish(event, session_id, state=state)
        self._save()
        return self._view(session)

    def pause_session(self, session_id: str) -> dict[str, Any]:
        return self._set_state(session_id, "PAUSED", "session.paused")

    def resume_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session or session["state"] != "PAUSED":
            raise ValueError("only paused sessions can be resumed")
        return self._set_state(session_id, "RUNNING", "session.resumed")

    def begin_operation(
        self, session_id: str, operation_name: str, metadata: dict[str, Any] | None = None
    ) -> tuple[Event, str]:
        session = self._sessions.get(session_id)
        if not session or session["state"] != "RUNNING":
            raise ValueError("session is not running")
        operation_id = str(uuid4())
        cancel = Event()
        self._active[operation_id] = cancel
        session["active_operation"] = operation_id
        session["journal"].append(
            {
                "operation_id": operation_id,
                "operation": operation_name,
                "status": "STARTED",
                "started_at": _now(),
            }
        )
        self.events.publish(
            "operation.requested",
            session_id,
            operation_id=operation_id,
            operation=operation_name,
            metadata=metadata or {},
        )
        self.events.publish("operation.authorized", session_id, operation_id=operation_id)
        self.events.publish("operation.started", session_id, operation_id=operation_id)
        self._save()
        return cancel, operation_id

    def complete_operation(self, session_id: str, operation_id: str, result: Any = None) -> None:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown session")
        record = next(
            (item for item in session["journal"] if item["operation_id"] == operation_id), None
        )
        if record is None:
            raise KeyError("unknown operation")
        cancelled = operation_id not in self._active or self._active[operation_id].is_set()
        record.update(
            status="CANCELLED" if cancelled else "COMPLETED", completed_at=_now(), result=result
        )
        self._active.pop(operation_id, None)
        if session["active_operation"] == operation_id:
            session["active_operation"] = None
        self.events.publish(
            "operation.completed", session_id, operation_id=operation_id, status=record["status"]
        )
        self._save()

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown session")
        operation_id = session.get("active_operation")
        if operation_id and operation_id in self._active:
            self._active[operation_id].set()
            self.events.publish("operation.cancelled", session_id, operation_id=operation_id)
        return self._set_state(session_id, "CANCELLED", "session.completed")

    def record_verification(self, session_id: str, result: Any) -> None:
        self.events.publish("verification.started", session_id)
        self.events.publish("verification.completed", session_id, result=result)

    def record_experience(self, session_id: str, experience_id: str) -> None:
        self.events.publish("experience.recorded", session_id, experience_id=experience_id)

    def record_approval(self, session_id: str, approved: bool, reason: str = "") -> None:
        """Persist a human decision; the UI may request this, never make it itself."""
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown session")
        decision = "APPROVED" if approved else "REJECTED"
        session["journal"].append(
            {
                "operation": "human.approval",
                "status": decision,
                "reason": reason,
                "completed_at": _now(),
            }
        )
        self.events.publish("approval.recorded", session_id, approved=approved, reason=reason)
        self._save()
