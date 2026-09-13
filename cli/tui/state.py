"""Delivery and session state models for the StackMind TUI.

Extracts the delivery lifecycle state, plan revision history, role execution status,
work order tracking, hierarchical operation tree, and live activity streams into a
dedicated, modular component.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ProjectPhase(str, Enum):
    INITIALIZING = "INITIALIZING"
    PLAN_PROPOSED = "PLAN_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PLAN_APPROVED = "PLAN_APPROVED"
    AUTONOMOUS_EXECUTION = "AUTONOMOUS_EXECUTION"
    PLAN_REJECTED = "PLAN_REJECTED"
    PROJECT_COMPLETE = "PROJECT_COMPLETE"


@dataclass
class PlanRevision:
    revision: int
    state: str
    feedback: str = ""
    timestamp: str = ""
    plan_id: str = ""


@dataclass
class RoleStatus:
    role: str
    backend: str = "Codex"
    model: str | None = None
    work_order_id: str | None = None
    state: str = "WAITING"  # WAITING, RUNNING, COMPLETED, CANCELLED, FAILED
    agent_id: str | None = None

    @property
    def display_state(self) -> str:
        s = self.state.upper()
        if s in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
            return "implementing" if self.role.lower() != "architecture" else "orchestrating"
        if s in {"COMPLETED", "DONE", "VERIFIED"}:
            return "completed" if self.role.lower() != "architecture" else "orchestrating"
        if s in {"CANCELLED", "CANCELED"}:
            return "cancelled"
        if s in {"FAILED", "ERROR"}:
            return "failed"
        return "waiting"


@dataclass
class WorkOrderItem:
    id: str
    title: str
    role: str = "Backend"
    priority: str = "P0"
    status: str = "WAITING"  # WAITING, RUNNING, COMPLETED, CANCELLED
    deliverable: str | None = None
    progress: str = "0%"


@dataclass
class OperationNode:
    operation_id: str
    name: str
    role: str = "Architecture"
    backend: str = "Claude"
    status: str = "RUNNING"
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class ActivityEntry:
    timestamp: str
    role: str
    action: str
    target: str = ""
    status: str = "OK"

    def format_line(self) -> str:
        t = self.timestamp.split("T")[-1][:5] if "T" in self.timestamp else self.timestamp[:5]
        target_part = f" {self.target}" if self.target else ""
        return f"{t:<5} {self.role:<10} {self.action}{target_part}"


class AutonomousDeliveryState:
    """Live in-memory state tracking the autonomous project delivery lifecycle."""

    def __init__(self, project_name: str = "stackmind-project", session_id: str = "SESSION-INIT") -> None:
        self.project_name = project_name
        self.session_id = session_id
        self.phase: ProjectPhase = ProjectPhase.INITIALIZING
        self.plan: dict[str, Any] = {}
        self.plan_revisions: list[PlanRevision] = []
        self.roles: dict[str, RoleStatus] = {
            "Architecture": RoleStatus("Architecture", backend="Claude", work_order_id="WO-001", state="RUNNING"),
            "Backend": RoleStatus("Backend", backend="Codex", work_order_id="WO-002", state="WAITING"),
            "Frontend": RoleStatus("Frontend", backend="AGY", work_order_id="WO-003", state="WAITING"),
            "Q/A": RoleStatus("Q/A", backend="Ollama", work_order_id="WO-004", state="WAITING"),
            "GitOps": RoleStatus("GitOps", backend="Ollama", work_order_id="WO-005", state="WAITING"),
        }
        self.work_orders: list[WorkOrderItem] = [
            WorkOrderItem("WO-001", "Architecture & Orchestration", role="Architecture", priority="P0", status="RUNNING"),
            WorkOrderItem("WO-002", "Backend API & Services", role="Backend", priority="P0", status="WAITING"),
            WorkOrderItem("WO-003", "Frontend Client & Views", role="Frontend", priority="P0", status="WAITING"),
            WorkOrderItem("WO-004", "Q/A Verification Suite", role="Q/A", priority="P0", status="WAITING"),
            WorkOrderItem("WO-005", "GitOps Release & Hygiene", role="GitOps", priority="P0", status="WAITING"),
        ]
        self.operations: dict[str, OperationNode] = {
            "op-root": OperationNode("op-root", "Architecture", role="Architecture", backend="Claude", status="RUNNING")
        }
        self.activity_log: list[ActivityEntry] = []
        self.completion_checklist: dict[str, bool] = {
            "PLAN.md": False,
            "Backend": False,
            "Frontend": False,
            "Q/A": False,
            "GitOps": False,
            "README.md": False,
            "Tests": False,
            "Git": False,
        }
        self.is_complete: bool = False

    def now_str(self) -> str:
        return datetime.datetime.now().strftime("%H:%M")

    def add_activity(self, role: str, action: str, target: str = "") -> None:
        self.activity_log.append(ActivityEntry(self.now_str(), role, action, target))

    def update_from_session(self, session: Mapping[str, Any]) -> None:
        if not session:
            return
        self.session_id = session.get("session_id", self.session_id)
        # Check plans in session
        plans = session.get("plans", {})
        if plans:
            latest_plan = list(plans.values())[-1]
            self.plan = dict(latest_plan)
            st = latest_plan.get("state", "").upper()
            if st == "AWAITING_APPROVAL":
                self.phase = ProjectPhase.AWAITING_APPROVAL
            elif st == "APPROVED":
                self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                self.completion_checklist["PLAN.md"] = True
            elif st == "REJECTED":
                self.phase = ProjectPhase.PLAN_REJECTED

            created_wos = latest_plan.get("created_work_orders", [])
            if created_wos:
                self.sync_work_orders(created_wos)

        # Check journal operations
        journal = session.get("journal", [])
        for entry in journal:
            op_id = entry.get("operation_id")
            if op_id and op_id not in self.operations:
                role = entry.get("role") or self._infer_role_from_op(entry.get("operation", ""))
                backend = entry.get("backend") or "Codex"
                status = entry.get("status", "RUNNING")
                parent_id = entry.get("parent_operation_id")
                node = OperationNode(op_id, entry.get("operation", role), role=role, backend=backend, status=status, parent_id=parent_id)
                self.operations[op_id] = node
                if parent_id and parent_id in self.operations:
                    if op_id not in self.operations[parent_id].children:
                        self.operations[parent_id].children.append(op_id)
                else:
                    if op_id not in self.operations["op-root"].children:
                        self.operations["op-root"].children.append(op_id)

    def sync_work_orders(self, wo_records: list[dict[str, Any]]) -> None:
        items = []
        for r in wo_records:
            wo_id = r.get("id") or r.get("wo_id") or "WO-xxx"
            title = r.get("title", f"Work order {wo_id}")
            agents = r.get("assigned_agents", [])
            role = agents[0].title() if agents else "Backend"
            priority = r.get("priority", "P0")
            status = r.get("status", "WAITING")
            items.append(WorkOrderItem(wo_id, title, role=role, priority=priority, status=status))
        if items:
            self.work_orders = items

    def _infer_role_from_op(self, op_name: str) -> str:
        low = op_name.lower()
        if "arch" in low or "plan" in low:
            return "Architecture"
        if "front" in low or "ui" in low or "view" in low:
            return "Frontend"
        if "qa" in low or "test" in low:
            return "Q/A"
        if "git" in low or "release" in low:
            return "GitOps"
        return "Backend"

    def process_event(self, event: Mapping[str, Any]) -> None:
        """Process incoming sequenced daemon / SSE events to update delivery state."""
        name = event.get("name", "")
        payload = event.get("payload", {})

        if name == "plan.proposed":
            self.phase = ProjectPhase.AWAITING_APPROVAL
            plan_id = payload.get("plan_id", "PLAN-001")
            self.plan = dict(payload)
            rev_num = len(self.plan_revisions) + 1
            self.plan["revision"] = rev_num
            self.plan_revisions.append(
                PlanRevision(
                    revision=rev_num,
                    state="AWAITING_APPROVAL",
                    plan_id=plan_id,
                    timestamp=self.now_str(),
                )
            )
            self.add_activity("Architecture", f"proposed plan {plan_id}")

        elif name == "plan.approved":
            self.phase = ProjectPhase.AUTONOMOUS_EXECUTION
            plan_id = payload.get("plan_id", "PLAN-001")
            self.completion_checklist["PLAN.md"] = True
            if self.plan:
                self.plan["state"] = "APPROVED"
                self.plan["approval_reason"] = payload.get("reason", "")
            if self.plan_revisions:
                self.plan_revisions[-1].state = "APPROVED"
            # Update roles to implementing
            if "Backend" in self.roles:
                self.roles["Backend"].state = "RUNNING"
            if "Frontend" in self.roles:
                self.roles["Frontend"].state = "RUNNING"
            if "Q/A" in self.roles:
                self.roles["Q/A"].state = "WAITING"
            # Sync created work orders
            created_records = payload.get("created_records") or payload.get("work_orders", [])
            if created_records and isinstance(created_records[0], dict):
                self.sync_work_orders(created_records)
            self.add_activity("Architecture", f"plan approved: {plan_id}")

        elif name == "plan.rejected":
            self.phase = ProjectPhase.PLAN_REJECTED
            plan_id = payload.get("plan_id", "PLAN-001")
            feedback = payload.get("reason", "")
            if self.plan:
                self.plan["state"] = "REJECTED"
                self.plan["feedback"] = feedback
            if self.plan_revisions:
                self.plan_revisions[-1].state = "REJECTED"
                self.plan_revisions[-1].feedback = feedback
            self.add_activity("Operator", f"plan rejected: {feedback or 'Needs revision'}")

        elif name in {"event.agentSpawned", "agent.spawned"}:
            agent_id = payload.get("agent_id") or payload.get("agentId") or "agent-sub"
            role_name = (payload.get("role") or self._infer_role_from_op(agent_id)).title()
            backend = payload.get("backend") or payload.get("executionBackend") or "Codex"
            wo_id = payload.get("work_order_id") or payload.get("workOrderId")
            parent_id = payload.get("parent_operation_id") or payload.get("parentOperationId") or "op-root"
            op_id = payload.get("operation_id") or payload.get("operationId") or f"op-{agent_id}"

            # Update roles
            normalized_role = "Q/A" if role_name.upper() in {"QA", "Q/A"} else role_name
            self.roles[normalized_role] = RoleStatus(
                normalized_role,
                backend=backend,
                work_order_id=wo_id,
                state="RUNNING",
                agent_id=agent_id,
            )

            # Update tree
            node = OperationNode(op_id, normalized_role, role=normalized_role, backend=backend, status="RUNNING", parent_id=parent_id)
            self.operations[op_id] = node
            if parent_id in self.operations and op_id not in self.operations[parent_id].children:
                self.operations[parent_id].children.append(op_id)
            elif op_id not in self.operations["op-root"].children:
                self.operations["op-root"].children.append(op_id)

            self.add_activity(normalized_role, f"spawned ({backend})", wo_id or "")

        elif name in {"operation.started", "turn.started"}:
            op_name = payload.get("operation", "")
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            wo_id = payload.get("work_order_id")
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "RUNNING"
                if wo_id:
                    self.roles[normalized_role].work_order_id = wo_id
            for wo in self.work_orders:
                if wo.id == wo_id or wo.role.lower() == normalized_role.lower():
                    wo.status = "RUNNING"
            self.add_activity(normalized_role, "started", op_name or (wo_id or ""))

        elif name in {"operation.completed", "turn.completed"}:
            op_name = payload.get("operation", "")
            role = (payload.get("role") or self._infer_role_from_op(op_name)).title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            wo_id = payload.get("work_order_id")
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "COMPLETED"
            for wo in self.work_orders:
                if wo.id == wo_id or wo.role.lower() == normalized_role.lower():
                    wo.status = "COMPLETED"
            # Update checklist
            if "backend" in normalized_role.lower():
                self.completion_checklist["Backend"] = True
            elif "front" in normalized_role.lower():
                self.completion_checklist["Frontend"] = True
            elif "q" in normalized_role.lower():
                self.completion_checklist["Q/A"] = True
                self.completion_checklist["Tests"] = True
            elif "git" in normalized_role.lower():
                self.completion_checklist["GitOps"] = True
                self.completion_checklist["README.md"] = True
                self.completion_checklist["Git"] = True

            self.add_activity(normalized_role, "completed", op_name or (wo_id or ""))

        elif name in {"operation.cancelled", "agent.cancelled"}:
            role = (payload.get("role") or "Agent").title()
            normalized_role = "Q/A" if role.upper() in {"QA", "Q/A"} else role
            if normalized_role in self.roles:
                self.roles[normalized_role].state = "CANCELLED"
            self.add_activity(normalized_role, "cancelled", payload.get("reason", ""))

        elif name.startswith("tool_call.") or name == "tool.call":
            tool_name = name.partition(".")[2] if "." in name else payload.get("tool", "tool")
            role = (payload.get("role") or "Backend").title()
            target = payload.get("path") or payload.get("file") or payload.get("command") or payload.get("symbol") or ""
            self.add_activity(role, tool_name, str(target))

        elif name == "project.completed" or name == "project.complete":
            self.phase = ProjectPhase.PROJECT_COMPLETE
            for k in self.completion_checklist:
                self.completion_checklist[k] = True
            self.is_complete = True
            self.add_activity("StackMind", "delivered project complete handover")


__all__ = [
    "ActivityEntry",
    "AutonomousDeliveryState",
    "OperationNode",
    "PlanRevision",
    "ProjectPhase",
    "RoleStatus",
    "WorkOrderItem",
]
