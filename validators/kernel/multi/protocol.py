"""JSON-RPC multi-agent dispatcher hosted by the runtime daemon."""

from __future__ import annotations

from typing import Any

from .models import AgentRole
from .supervisor import MultiAgentSupervisor


class MultiAgentProtocol:
    def __init__(self, supervisor: MultiAgentSupervisor) -> None:
        self.supervisor = supervisor

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "multi.delegate_task":
            return self.supervisor.delegate_task(
                params["ensemble_id"],
                AgentRole(params["from_role"]),
                AgentRole(params["to_role"]),
                params["instructions"],
                params.get("parent_task"),
            ).__dict__
        if method == "multi.handoff":
            return self.supervisor.handoff(
                params["ensemble_id"], params["task_id"], params["summary"], params["verification"]
            ).__dict__
        if method == "multi.review_and_merge":
            return {"approved": self.supervisor.route_review(params["record"])}
        raise ValueError("Method not found")
