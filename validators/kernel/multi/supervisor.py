"""The sole coordinator for role-scoped multi-agent work."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from ..contract import AgentContract
from ..daemon import SessionManager
from ..identity import AgentIdentity
from ..workspace import ScratchWorkspace
from .models import AgentRole, EnsembleMember, HandoffRecord, TaskDelegation


class MultiAgentSupervisor:
    def __init__(self, manager: SessionManager, authoritative_root: Path) -> None:
        self.manager, self.authoritative_root = manager, authoritative_root
        self.ensembles: dict[str, dict[AgentRole, EnsembleMember]] = {}
        self.delegations: dict[str, TaskDelegation] = {}

    def create_ensemble(
        self, ensemble_id: str, roles: dict[AgentRole, dict[str, Any]]
    ) -> dict[AgentRole, EnsembleMember]:
        if ensemble_id in self.ensembles:
            raise ValueError("ensemble already exists")
        members = {}
        for role, spec in roles.items():
            contract = spec["contract"]
            if not isinstance(contract, AgentContract):
                raise ValueError("each role requires an AgentContract")
            members[role] = EnsembleMember(
                AgentIdentity(spec["agent_id"], role.value),
                contract,
                ScratchWorkspace.create(self.authoritative_root, f"{ensemble_id}-{role.value}"),
                role,
            )
        if set(members) != set(AgentRole):
            raise ValueError("planner, worker, and reviewer are required")
        self.ensembles[ensemble_id] = members
        return members

    def _member(self, ensemble_id: str, role: AgentRole) -> EnsembleMember:
        return self.ensembles[ensemble_id][role]

    def delegate_task(
        self,
        ensemble_id: str,
        from_role: AgentRole,
        to_role: AgentRole,
        instructions: str,
        parent_task: str | None = None,
    ) -> TaskDelegation:
        if from_role is AgentRole.WORKER and to_role is AgentRole.REVIEWER:
            raise PermissionError("worker must hand off through supervisor")
        sender, recipient = self._member(ensemble_id, from_role), self._member(ensemble_id, to_role)
        task = TaskDelegation(
            str(uuid4()),
            parent_task,
            sender.identity.agent_id,
            recipient.identity.agent_id,
            instructions,
            "DELEGATED",
        )
        self.delegations[task.task_id] = task
        self.manager.events.publish(
            "multi.delegation",
            ensemble_id,
            task_id=task.task_id,
            from_role=from_role,
            to_role=to_role,
        )
        return task

    def enforce_isolation(self, ensemble_id: str, actor_id: str, target_path: str) -> bool:
        member = next(
            (m for m in self.ensembles[ensemble_id].values() if m.identity.agent_id == actor_id),
            None,
        )
        if member is None:
            raise PermissionError("unknown ensemble actor")
        try:
            Path(target_path).resolve().relative_to(member.workspace.root)
        except ValueError as exc:
            raise PermissionError("cross-workspace access is forbidden") from exc
        return True

    def handoff(
        self, ensemble_id: str, task_id: str, summary: str, verification: dict[str, bool]
    ) -> HandoffRecord:
        task = self.delegations[task_id]
        worker = self._member(ensemble_id, AgentRole.WORKER)
        task.status = "HANDED_OFF"
        record = HandoffRecord(
            task_id,
            task.from_agent,
            self._member(ensemble_id, AgentRole.REVIEWER).identity.agent_id,
            str(worker.workspace.root),
            summary,
            verification,
        )
        self.manager.events.publish("multi.handoff", ensemble_id, task_id=task_id)
        return record

    def route_review(self, record: HandoffRecord) -> bool:
        return all(record.verification.values())

    def complete_ensemble(self, ensemble_id: str) -> dict[str, Any]:
        self.manager.events.publish("multi.completed", ensemble_id)
        return {"ensemble_id": ensemble_id, "complete": True}
