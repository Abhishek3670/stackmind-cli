"""Durable session lifecycle and cancellation coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Callable
from uuid import uuid4

import yaml

from .events import EventDispatcher, RuntimeEvent
from .storage import DaemonStorage

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_PLAN_STATES = {"DRAFT", "AWAITING_APPROVAL", "APPROVED", "REJECTED", "SUPERSEDED"}
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

_LOGICAL_ROLES = ["architecture", "backend", "frontend", "qa", "gitops"]
_ROLE_ALIASES = {
    "claude": "architecture",
    "architecture": "architecture",
    "architect": "architecture",
    "codex": "backend",
    "backend": "backend",
    "gemini": "frontend",
    "frontend": "frontend",
    "gemma": "qa",
    "qa": "qa",
    "local-llm": "gitops",
    "gitops": "gitops",
}
_ROLE_TO_AGENTS = {
    "architecture": ["claude", "architecture", "architect"],
    "backend": ["codex", "backend"],
    "frontend": ["gemini", "frontend"],
    "qa": ["gemma", "qa"],
    "gitops": ["local-llm", "gitops"],
}
_DEFAULT_ROLE_CONFIG = {
    "architecture": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "backend": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "frontend": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "qa": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "gitops": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
}


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
        self._roles: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _DEFAULT_ROLE_CONFIG.items()}
        for r_name, r_cfg in recovered.get("roles", {}).items():
            if isinstance(r_cfg, dict):
                self._roles[r_name] = dict(r_cfg)
        self._runner_factory = runner_factory or self._default_runner
        self.events = EventDispatcher(recovered.get("events", []), self._persist_event)
        for session in self._sessions.values():
            session.setdefault("plans", {})
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
        self.storage.save({
            "sessions": self._sessions,
            "events": self.events.dump(),
            "roles": self._roles,
        })

    def _default_runner(self, workspace: str, agent: str) -> Any:
        from validators.harness.backend import get_default_registry
        from validators.harness.runner import AgentRunner

        if isinstance(self, SessionManager):
            canonical = self._canonical_role(agent)
            role_cfg = self._roles.get(canonical, {})
            backend_id = role_cfg.get("backend", "echo-agent")
            registry = get_default_registry()
            backend = registry.get(backend_id) if backend_id in registry else None
            return AgentRunner(Path(workspace), agent, backend=backend)
        return AgentRunner(Path(workspace), agent)

    def list_backends(self) -> list[dict[str, Any]]:
        from validators.harness.backend import get_default_registry

        return get_default_registry().list_backends()

    def list_roles(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for role_name, config in self._roles.items():
                entry: dict[str, Any] = {
                    "role": role_name,
                    "backend": config.get("backend", "echo-agent"),
                    "model": config.get("model"),
                    "status": config.get("status", "configured"),
                }
                if config.get("credential_ref"):
                    entry["credentialRef"] = config["credential_ref"]
                result.append(entry)
            return result

    def _canonical_role(self, role: str) -> str:
        role_lower = str(role or "").lower().strip()
        return _ROLE_ALIASES.get(role_lower, role_lower)

    def _role_target_identifiers(self, role: str) -> set[str]:
        canonical = self._canonical_role(role)
        agents = set(_ROLE_TO_AGENTS.get(canonical, [canonical]))
        agents.add(str(role).lower().strip())
        agents.add(canonical)
        return agents

    def configure_role_backend(
        self,
        role: str,
        backend: str,
        model: str | None = None,
        credential_ref: str | None = None,
    ) -> dict[str, Any]:
        """Configure the execution backend for an agent role.

        Strict Rebinding Guard:
        Must be rejected (raises ValueError) if target role has any non-terminal
        Work Order in flight or active operation. Rebinding is permitted only
        between assignments when the role has zero non-terminal work orders.
        """
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role is required")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend is required")

        canonical = self._canonical_role(role)
        target_ids = self._role_target_identifiers(role)

        with self._lock:
            for session in self._sessions.values():
                sess_agent = str(session.get("agent", "")).lower().strip()
                is_target_role = sess_agent in target_ids

                # 1. Active operation in session belonging to target role
                if is_target_role:
                    if session.get("active_operation") is not None and session.get("state") == "RUNNING":
                        raise ValueError(
                            f"Cannot rebind role '{role}': session '{session['session_id']}' has an active operation in flight"
                        )
                    for rec in session.get("journal", []):
                        if rec.get("status") not in _OPERATION_TERMINAL:
                            raise ValueError(
                                f"Cannot rebind role '{role}': operation '{rec.get('operation_id')}' is currently in flight ({rec.get('status')})"
                            )

                # 2. Any active operations explicitly tagged with target agent / role
                for rec in session.get("journal", []):
                    if rec.get("status") not in _OPERATION_TERMINAL:
                        op_agent = str(rec.get("metadata", {}).get("agent", sess_agent)).lower().strip()
                        if op_agent in target_ids:
                            raise ValueError(
                                f"Cannot rebind role '{role}': active operation '{rec.get('operation_id')}' belongs to role '{role}'"
                            )

                # 3. Created work orders in proposed / approved plans
                for plan in session.get("plans", {}).values():
                    for wo in plan.get("created_work_orders", []):
                        wo_status = str(wo.get("status", "ACTIVE")).upper()
                        if wo_status not in {"COMPLETED", "CANCELLED", "FAILED"}:
                            assigned = [str(a).lower().strip() for a in wo.get("assigned_agents", [])]
                            if any(a in target_ids for a in assigned):
                                raise ValueError(
                                    f"Cannot rebind role '{role}': in-flight non-terminal work order '{wo.get('id')}' assigned to role"
                                )

                # 4. Check workspace disk work orders
                workspace_str = session.get("workspace")
                if workspace_str:
                    active_wos_dir = Path(workspace_str) / ".sync" / "work-orders" / "ACTIVE"
                    if active_wos_dir.is_dir():
                        for wo_file in active_wos_dir.glob("*.yaml"):
                            try:
                                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                                if isinstance(data, dict):
                                    status = str(data.get("status", "ACTIVE")).upper()
                                    if status not in {"COMPLETED", "CANCELLED", "FAILED"}:
                                        assigned = [str(a).lower().strip() for a in data.get("assigned_agents", [])]
                                        if any(a in target_ids for a in assigned):
                                            raise ValueError(
                                                f"Cannot rebind role '{role}': in-flight non-terminal work order '{data.get('id', wo_file.stem)}' assigned to role"
                                            )
                            except ValueError:
                                raise
                            except Exception:
                                pass

            # Rebinding is permitted between assignments
            now = _now()
            entry: dict[str, Any] = {
                "backend": backend,
                "model": model,
                "status": "configured",
                "credential_ref": credential_ref,
                "updated_at": now,
            }
            self._roles[canonical] = entry
            self.events.publish(
                "role.backend_configured",
                "daemon",
                role=canonical,
                backend=backend,
                model=model,
                credential_ref=credential_ref,
            )
            self._save()
            return {
                "role": canonical,
                "backend": backend,
                "model": model,
                "appliedAt": now,
            }

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
                "plans": {},
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

    def propose_plan(
        self,
        session_id: str,
        plan_id: str,
        title: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose an architectural plan, placing it in AWAITING_APPROVAL state."""
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan_id is required")
        if not isinstance(title, str) or not title:
            raise ValueError("title is required")
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            if session["state"] in _TERMINAL:
                raise ValueError("session is terminal")

            plans = session.setdefault("plans", {})
            now = _now()
            meta = metadata or {}
            work_orders = meta.get("work_orders", [])

            if plan_id in plans:
                existing = plans[plan_id]
                if existing.get("state") == "APPROVED":
                    raise ValueError(f"Plan '{plan_id}' is already APPROVED")
                # Revision of existing plan
                revisions = existing.setdefault("revisions", [])
                revisions.append({
                    "title": existing.get("title"),
                    "content": existing.get("content"),
                    "state": existing.get("state"),
                    "feedback": existing.get("feedback"),
                    "updated_at": existing.get("updated_at"),
                })
                existing.update(
                    title=title,
                    content=content,
                    metadata=meta,
                    work_orders=work_orders,
                    state="AWAITING_APPROVAL",
                    feedback=None,
                    updated_at=now,
                )
                plan_record = existing
            else:
                plan_record = {
                    "plan_id": plan_id,
                    "session_id": session_id,
                    "title": title,
                    "content": content,
                    "metadata": meta,
                    "work_orders": work_orders,
                    "state": "AWAITING_APPROVAL",
                    "feedback": None,
                    "created_at": now,
                    "updated_at": now,
                }
                plans[plan_id] = plan_record

            self.events.publish(
                "plan.proposed",
                session_id,
                plan_id=plan_id,
                title=title,
                content=content,
                metadata=meta,
                state="AWAITING_APPROVAL",
            )
            self._save()
            return dict(plan_record)

    def get_plan(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        """Return the specified plan or the latest plan for the session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id is not None:
                if plan_id not in plans:
                    raise KeyError(f"plan '{plan_id}' not found in session")
                return dict(plans[plan_id])
            if not plans:
                raise KeyError(f"no plans found in session '{session_id}'")
            return dict(list(plans.values())[-1])

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        """Return all plans proposed in the session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            return [dict(p) for p in session.get("plans", {}).values()]

    def approve_plan(
        self, session_id: str, plan_id: str, reason: str = ""
    ) -> list[dict[str, Any]]:
        """Approve a proposed plan and create its persistent Work Orders."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id not in plans:
                raise KeyError(f"plan '{plan_id}' not found in session")

            plan = plans[plan_id]
            if plan.get("state") != "AWAITING_APPROVAL":
                raise ValueError("Plan is not in AWAITING_APPROVAL state")

            now = _now()
            plan["state"] = "APPROVED"
            plan["updated_at"] = now
            plan["approval_reason"] = reason

            created_work_orders: list[dict[str, Any]] = []
            proposed_wos = plan.get("metadata", {}).get("work_orders", [])
            workspace = Path(session["workspace"])
            wo_dir = workspace / ".sync" / "work-orders" / "ACTIVE"

            for item in proposed_wos:
                if isinstance(item, dict):
                    wo_id = str(item.get("id") or item.get("wo_id"))
                    wo_record = {
                        "id": wo_id,
                        "title": item.get("title", f"Work order {wo_id}"),
                        "type": item.get("type", "FEATURE"),
                        "status": "ACTIVE",
                        "priority": item.get("priority", "P0"),
                        "assigned_agents": item.get("assigned_agents", [session["agent"]]),
                        "description": item.get("description", ""),
                        "created": now,
                        "updated": now,
                    }
                    for k, v in item.items():
                        if k not in wo_record:
                            wo_record[k] = v
                else:
                    wo_id = str(item)
                    wo_record = {
                        "id": wo_id,
                        "title": f"Work order {wo_id}",
                        "type": "FEATURE",
                        "status": "ACTIVE",
                        "priority": "P0",
                        "assigned_agents": [session["agent"]],
                        "description": "",
                        "created": now,
                        "updated": now,
                    }

                created_work_orders.append(wo_record)
                if (workspace / ".sync").exists():
                    try:
                        wo_dir.mkdir(parents=True, exist_ok=True)
                        wo_path = wo_dir / f"{wo_id}.yaml"
                        with wo_path.open("w", encoding="utf-8") as handle:
                            yaml.safe_dump(wo_record, handle, sort_keys=False)
                    except Exception:
                        pass
                    index_path = workspace / ".sync" / "work-orders" / "INDEX.yaml"
                    if index_path.exists():
                        try:
                            index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
                            if isinstance(index_data, dict) and "orders" in index_data:
                                existing_ids = {
                                    o.get("id")
                                    for o in index_data["orders"]
                                    if isinstance(o, dict)
                                }
                                if wo_id not in existing_ids:
                                    index_data["orders"].append({
                                        "id": wo_id,
                                        "type": wo_record.get("type", "FEATURE"),
                                        "title": wo_record.get("title", f"Work order {wo_id}"),
                                        "status": wo_record.get("status", "ACTIVE"),
                                        "priority": wo_record.get("priority", "P0"),
                                        "assigned_agents": wo_record.get("assigned_agents", [session["agent"]]),
                                        "dependencies": wo_record.get("dependencies", []),
                                        "deliverable": wo_record.get("deliverable"),
                                        "created": wo_record.get("created"),
                                        "updated": wo_record.get("updated"),
                                        "file": f"work-orders/ACTIVE/{wo_id}.yaml",
                                    })
                                    with index_path.open("w", encoding="utf-8") as handle:
                                        yaml.safe_dump(index_data, handle, sort_keys=False)
                        except Exception:
                            pass

            plan["created_work_orders"] = created_work_orders
            self.events.publish(
                "plan.approved",
                session_id,
                plan_id=plan_id,
                reason=reason,
                work_orders=[w["id"] for w in created_work_orders],
                created_records=created_work_orders,
            )
            self._save()
            return [dict(w) for w in created_work_orders]

    def reject_plan(
        self, session_id: str, plan_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Reject a proposed plan with operator feedback, creating zero Work Orders."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id not in plans:
                raise KeyError(f"plan '{plan_id}' not found in session")

            plan = plans[plan_id]
            if plan.get("state") != "AWAITING_APPROVAL":
                raise ValueError("Plan is not in AWAITING_APPROVAL state")

            now = _now()
            plan["state"] = "REJECTED"
            plan["feedback"] = reason
            plan["reason"] = reason
            plan["updated_at"] = now
            plan["created_work_orders"] = []

            self.events.publish(
                "plan.rejected",
                session_id,
                plan_id=plan_id,
                reason=reason,
            )
            self._save()
            return dict(plan)

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

    def execute_work_order(
        self, session_id: str, work_order_id: str, prompt: str | None = None, **params: Any
    ) -> dict[str, Any]:
        """Start a turn to execute an assigned work order via the AgentRunner harness."""
        p = prompt or f"Execute work order {work_order_id}"
        return self.start_turn(session_id, p, work_order_id=work_order_id, **params)

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
            with self._lock:
                try:
                    _, op_rec = self._operation(operation_id)
                    if hasattr(runner, "backend_id"):
                        op_rec["backend_id"] = runner.backend_id
                    if hasattr(runner, "backend_model"):
                        op_rec["model"] = runner.backend_model
                except KeyError:
                    pass
            result = runner.run_once(cancel_event=cancel_event, operation_id=operation_id)
            result_data = {
                "status": result.status,
                "persisted": result.persisted,
                "task_id": result.task_id,
                "reason": result.reason,
                "backend_id": getattr(runner, "backend_id", None),
                "model": getattr(runner, "backend_model", None),
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
