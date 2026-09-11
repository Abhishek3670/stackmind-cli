"""Durable session lifecycle and cancellation coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Callable
from uuid import uuid4

from .events import EventDispatcher, RuntimeEvent
from .storage import DaemonStorage

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_OPERATION_STATES = {
    "REQUESTED",
    "AUTHORIZED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
}
_OPERATION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_narrower_or_equal(child_pat: str, parent_pat: str) -> bool:
    if child_pat == parent_pat:
        return True
    child_p = child_pat.replace("\\", "/").strip("/")
    parent_p = parent_pat.replace("\\", "/").strip("/")
    if parent_p in ("*", "**", ""):
        return True
    p_base = parent_p
    while p_base.endswith("/*"):
        p_base = p_base[:-2]
    if p_base.endswith("/**"):
        p_base = p_base[:-3]
    if p_base.endswith("*"):
        p_base = p_base[:-1]
    p_base = p_base.rstrip("/")
    if not p_base:
        return True
    return child_p == p_base or child_p.startswith(p_base + "/")


def _verify_contract_scope_narrowing(parent_scope: Any, child_scope: Any) -> None:
    """Verify child's contract_scope is a subset of parent's contract_scope."""
    if parent_scope is None:
        return
    if child_scope is None:
        raise ValueError("Child contract_scope cannot be None when parent has contract_scope")

    def _normalize(scope: Any) -> tuple[set[str] | None, set[str] | None]:
        if scope is None:
            return None, None
        if isinstance(scope, (list, set, tuple)):
            return set(str(x) for x in scope), None
        if isinstance(scope, dict):
            inner = scope.get("scope") if isinstance(scope.get("scope"), dict) else scope
            allow = (
                set(str(x) for x in inner["allow"])
                if "allow" in inner and isinstance(inner["allow"], (list, set, tuple))
                else None
            )
            deny = (
                set(str(x) for x in inner["deny"])
                if "deny" in inner and isinstance(inner["deny"], (list, set, tuple))
                else None
            )
            if allow is None and deny is None:
                return set(str(k) for k in inner.keys()), None
            return allow, deny
        return {str(scope)}, None

    parent_allow, parent_deny = _normalize(parent_scope)
    child_allow, child_deny = _normalize(child_scope)

    if parent_allow is not None:
        if child_allow is None:
            raise ValueError("child contract_scope cannot be wider than parent contract_scope")
        for c in child_allow:
            if not any(_is_narrower_or_equal(c, p) for p in parent_allow):
                raise ValueError(
                    f"child contract_scope allow pattern '{c}' is out of parent allow scope: {parent_allow}"
                )

    if parent_deny is not None:
        if child_deny is None:
            raise ValueError(
                f"Child contract_scope must include all parent deny patterns: {parent_deny}"
            )
        for p in parent_deny:
            if not any(p == c or _is_narrower_or_equal(p, c) for c in child_deny):
                raise ValueError(
                    f"Child contract_scope must include all parent deny patterns: {parent_deny}"
                )


class SessionManager:
    """Owns daemon sessions, their audit journals, and active-operation cancellation."""

    def __init__(
        self,
        storage: DaemonStorage,
        runner_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.storage = storage
        self._lock = RLock()
        recovered = storage.load()
        self._sessions: dict[str, dict[str, Any]] = recovered["sessions"]
        self._active: dict[str, Event] = {}
        self._turn_threads: dict[str, Thread] = {}
        self._runner_factory = runner_factory or self._default_runner
        self.events = EventDispatcher(recovered.get("events", []), self._persist_event)
        for session in self._sessions.values():
            if session["state"] == "RUNNING":
                session["state"] = "WAITING"
                session["updated_at"] = _now()
                self.events.publish("session.recovered", session["session_id"], state="WAITING")
            records = {
                r["operation_id"]: r
                for r in session.get("journal", [])
                if isinstance(r, dict) and "operation_id" in r
            }
            for r in records.values():
                r.setdefault("children", [])
            for r in records.values():
                pid = r.get("parent_operation_id")
                if pid and pid in records:
                    parent_children = records[pid].setdefault("children", [])
                    if r["operation_id"] not in parent_children:
                        parent_children.append(r["operation_id"])
        self._save()

    def _persist_event(self, _: RuntimeEvent) -> None:
        # Event listeners may publish from runner threads; serialize persistence
        # with all session/operation transitions to avoid competing temp-file replaces.
        with self._lock:
            self._save()

    def _save(self) -> None:
        self.storage.save({"sessions": self._sessions, "events": self.events.dump()})

    @staticmethod
    def _default_runner(workspace: str, agent: str) -> Any:
        from validators.harness.runner import AgentRunner

        return AgentRunner(Path(workspace), agent)

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
        with self._lock:
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
        with self._lock:
            try:
                return self._view(self._sessions[session_id])
            except KeyError as error:
                raise KeyError("unknown session") from error

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(session) for session in self._sessions.values()]

    def session_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return the durable audit journal for one session."""
        with self._lock:
            try:
                return [dict(record) for record in self._sessions[session_id]["journal"]]
            except KeyError as error:
                raise KeyError("unknown session") from error

    def close_session(self, session_id: str) -> dict[str, Any]:
        """Explicitly close a session without conflating it with operation cancellation."""
        with self._lock:
            return self._set_state(session_id, "COMPLETED", "session.completed")

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
        with self._lock:
            return self._set_state(session_id, "PAUSED", "session.paused")

    def resume_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session["state"] != "PAUSED":
                raise ValueError("only paused sessions can be resumed")
            return self._set_state(session_id, "RUNNING", "session.resumed")

    def begin_operation(
        self,
        session_id: str,
        operation_name: str,
        metadata: dict[str, Any] | None = None,
        *,
        parent_operation_id: str | None = None,
        work_order_id: str | None = None,
        contract_scope: Any = None,
    ) -> tuple[Event, str]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session["state"] != "RUNNING":
                raise ValueError("session is not running")

            parent_record = None
            if parent_operation_id is not None:
                for r in session.get("journal", []):
                    if r.get("operation_id") == parent_operation_id:
                        parent_record = r
                        break
                if parent_record is None:
                    raise KeyError(f"parent operation '{parent_operation_id}' not found in session")
                if parent_record.get("status") in _OPERATION_TERMINAL:
                    raise ValueError(
                        f"parent operation '{parent_operation_id}' is terminal ({parent_record.get('status')})"
                    )
                _verify_contract_scope_narrowing(parent_record.get("contract_scope"), contract_scope)
            else:
                if session.get("active_operation"):
                    raise ValueError("session already has an active operation")

            operation_id = str(uuid4())
            cancel = Event()
            now = _now()
            record = {
                "operation_id": operation_id,
                "parent_operation_id": parent_operation_id,
                "children": [],
                "operation": operation_name,
                "metadata": metadata or {},
                "work_order_id": work_order_id,
                "contract_scope": contract_scope,
                "status": "RUNNING",
                "started_at": now,
                "transitions": [
                    {"status": "REQUESTED", "at": now},
                    {"status": "AUTHORIZED", "at": now},
                    {"status": "RUNNING", "at": now},
                ],
            }
            if parent_record is not None:
                parent_record.setdefault("children", []).append(operation_id)
            else:
                session["active_operation"] = operation_id

            self._active[operation_id] = cancel
            session["journal"].append(record)
            payload = {
                "operation_id": operation_id,
                "operation": operation_name,
                "parent_operation_id": parent_operation_id,
                "metadata": metadata or {},
                "work_order_id": work_order_id,
                "contract_scope": contract_scope,
            }
            self.events.publish("operation.requested", session_id, **payload)
            self.events.publish("operation.authorized", session_id, operation_id=operation_id)
            self.events.publish("operation.started", session_id, operation_id=operation_id)
            self._save()
            return cancel, operation_id

    def _operation(self, operation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for session in self._sessions.values():
            for record in session["journal"]:
                if record.get("operation_id") == operation_id:
                    return session, record
        raise KeyError("unknown operation")

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            _, record = self._operation(operation_id)
            return dict(record)

    def list_children(self, operation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            _, record = self._operation(operation_id)
            children_ids = record.get("children", [])
            children = []
            for child_id in children_ids:
                try:
                    children.append(self.get_operation(child_id))
                except KeyError:
                    pass
            return children

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = (
                [self._sessions[session_id]] if session_id is not None else self._sessions.values()
            )
            return [
                dict(record)
                for session in sessions
                for record in session["journal"]
                if "operation_id" in record
            ]

    @staticmethod
    def _transition(record: dict[str, Any], status: str) -> None:
        if status not in _OPERATION_STATES:
            raise ValueError("invalid operation status")
        record["status"] = status
        record.setdefault("transitions", []).append({"status": status, "at": _now()})

    def cancel_operation(self, operation_id: str, cascade: bool = True) -> dict[str, Any]:
        """Request cooperative cancellation without changing the session lifecycle."""
        with self._lock:
            session, record = self._operation(operation_id)
            if record["status"] not in _OPERATION_TERMINAL:
                self._transition(record, "CANCEL_REQUESTED")
                cancel = self._active.get(operation_id)
                if cancel:
                    cancel.set()
                self.events.publish(
                    "operation.cancel_requested",
                    session["session_id"],
                    operation_id=operation_id,
                    cascade=cascade,
                )
            if cascade:
                for child_id in list(record.get("children", [])):
                    try:
                        self.cancel_operation(child_id, cascade=True)
                    except KeyError:
                        pass
            self._save()
            return dict(record)

    def complete_operation(
        self,
        session_id: str | None = None,
        operation_id: str | None = None,
        result: Any = None,
        status: str = "COMPLETED",
    ) -> dict[str, Any]:
        with self._lock:
            if operation_id is None:
                target_op_id = session_id
                target_session_id = None
            else:
                target_op_id = operation_id
                target_session_id = session_id

            if not target_op_id:
                raise KeyError("operation_id is required")

            found_session, record = self._operation(target_op_id)
            if target_session_id is not None and found_session["session_id"] != target_session_id:
                raise KeyError("unknown operation in session")
            session = found_session

            if record["status"] in _OPERATION_TERMINAL:
                return dict(record)
            cancel = self._active.get(target_op_id)
            final_status = (
                "CANCELLED"
                if record["status"] == "CANCEL_REQUESTED" or (cancel is not None and cancel.is_set())
                else status
            )
            if final_status not in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise ValueError("operation completion status must be terminal")

            if final_status == "COMPLETED":
                children_ids = record.get("children", [])
                for child_id in children_ids:
                    _, child_record = self._operation(child_id)
                    if child_record.get("status") not in _OPERATION_TERMINAL:
                        raise ValueError(
                            f"cannot complete operation '{target_op_id}': child operation '{child_id}' is active ({child_record.get('status')})"
                        )
                    if child_record.get("status") == "FAILED":
                        raise ValueError(
                            f"cannot complete operation '{target_op_id}' as COMPLETED: child operation '{child_id}' failed"
                        )

            self._transition(record, final_status)
            record.update(completed_at=_now(), result=result)
            self._active.pop(target_op_id, None)
            if session.get("active_operation") == target_op_id:
                session["active_operation"] = None
            event = (
                "operation.cancelled" if final_status == "CANCELLED"
                else "operation.failed" if final_status == "FAILED"
                else "operation.completed"
            )
            self.events.publish(event, session["session_id"], operation_id=target_op_id, status=final_status)
            self._save()
            return dict(record)

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            operation_id = session.get("active_operation")
            if operation_id:
                self.cancel_operation(operation_id)
                return self._view(session)
            return self._set_state(session_id, "CANCELLED", "session.cancelled")

    def start_turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        """Start a governed turn in a background runner without holding the manager lock."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            cancel_event, operation_id = self.begin_operation(
                session_id,
                "turn",
                {"prompt": prompt, **params},
                work_order_id=params.get("work_order_id"),
                contract_scope=params.get("contract_scope"),
            )
            self.events.publish("turn.started", session_id, operation_id=operation_id, prompt=prompt)
            thread = Thread(
                target=self._run_turn,
                args=(session_id, operation_id, cancel_event, prompt),
                name=f"stackmind-turn-{operation_id}",
                daemon=True,
            )
            self._turn_threads[operation_id] = thread
            self._save()
        thread.start()
        return self.get_operation(operation_id)

    def _run_turn(
        self, session_id: str, operation_id: str, cancel_event: Event, prompt: str
    ) -> None:
        """Run outside the manager lock; terminal state is resolved under it."""
        tool_name = "harness.run_once"
        self.events.tool_call(
            session_id, tool_name, operation_id, {"prompt": prompt}, operation_id
        )
        try:
            with self._lock:
                session = self._sessions[session_id]
                workspace = str(session["workspace"])
                agent = str(session["agent"])
            runner = self._runner_factory(workspace, agent)
            result = runner.run_once(cancel_event=cancel_event, operation_id=operation_id)
            result_data = {
                "status": result.status,
                "persisted": result.persisted,
                "task_id": result.task_id,
                "reason": result.reason,
            }
            if result.status == "cancelled" or cancel_event.is_set():
                self.events.tool_result(
                    session_id, tool_name, operation_id, "cancelled", operation_id=operation_id
                )
                self.complete_operation(session_id, operation_id, result_data, status="CANCELLED")
            elif result.status == "completed":
                self.events.tool_result(
                    session_id, tool_name, operation_id, "success", operation_id=operation_id,
                    result=result_data,
                )
                self.complete_operation(session_id, operation_id, result_data)
            else:
                self.events.tool_result(
                    session_id, tool_name, operation_id, "failure", operation_id=operation_id,
                    error=result_data,
                )
                self.complete_operation(session_id, operation_id, result_data, status="FAILED")
        except Exception as error:
            self.events.tool_result(
                session_id, tool_name, operation_id, "failure", operation_id=operation_id,
                error={"type": type(error).__name__},
            )
            self.complete_operation(
                session_id, operation_id, {"error": type(error).__name__}, status="FAILED"
            )
        finally:
            with self._lock:
                self._turn_threads.pop(operation_id, None)

    def record_verification(self, session_id: str, result: Any) -> None:
        with self._lock:
            self.events.publish("verification.started", session_id)
            self.events.publish("verification.completed", session_id, result=result)

    def record_experience(self, session_id: str, experience_id: str) -> None:
        with self._lock:
            self.events.publish("experience.recorded", session_id, experience_id=experience_id)

    def record_approval(self, session_id: str, approved: bool, reason: str = "") -> None:
        """Persist a human decision; the UI may request this, never make it itself."""
        with self._lock:
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
