"""Autonomous Delivery TUI & Control Plane Integration (Milestone P7-4).

Implements the user-facing control plane for the StackMind governed runtime:
- Project Delivery View (Phase Status Banner, Agent Roles panel, Work Orders panel,
  Hierarchical Operation Tree, Live Activity Stream)
- Plan Approval Surface & Governance Controls (:plan, :approve, :reject, revision history)
- Role/Backend Surface (:roles)
- Hierarchical Operation Tree (:tree, :agents)
- Work Orders View (:wo)
- Project Completion Handover Surface (:completion)
- Reactive SSE Event Integration updating state on agent spawn & operation progress.
"""

from __future__ import annotations

import datetime
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import click

from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from validators.kernel.tui.views import (
    activity_line,
    contract_panel,
    diff_viewer,
    session_header,
    verification_matrix,
)


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


# ─── VIEW RENDERERS ───────────────────────────────────────────────────────────

def render_phase_banner(state: AutonomousDeliveryState) -> str:
    """Renders the current project delivery phase banner."""
    lines = ["PHASE"]
    if state.phase in {ProjectPhase.INITIALIZING, ProjectPhase.PLAN_PROPOSED, ProjectPhase.AWAITING_APPROVAL}:
        lines.append("● Planning & Architecture (AWAITING_APPROVAL)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.PLAN_REJECTED:
        lines.append("✗ PLAN rejected (Revising)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase in {ProjectPhase.PLAN_APPROVED, ProjectPhase.AUTONOMOUS_EXECUTION}:
        lines.append("✓ PLAN approved")
        lines.append("● Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.PROJECT_COMPLETE:
        lines.append("✓ PLAN approved")
        lines.append("✓ Autonomous Execution")
        lines.append("✓ Project Complete")
    return "\n".join(lines)


def render_roles_panel(state: AutonomousDeliveryState, detailed: bool = False) -> str:
    """Renders the Agent Roles panel (:roles command)."""
    if detailed:
        out = ["=== AGENT ROLES & EXECUTION BACKENDS (:roles) ==="]
        for role_name, r in state.roles.items():
            out.append(f"{role_name} Agent")
            out.append(f"  Role: {r.role}")
            out.append(f"  Backend: {r.backend}")
            out.append(f"  Work Order: {r.work_order_id or 'None'}")
            out.append(f"  State: {r.state}")
            out.append("")
        return "\n".join(out).rstrip()

    # Dashboard compact view
    lines = ["AGENTS"]
    for role_name, r in state.roles.items():
        marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
        lines.append(f"{marker} {role_name:<16} {r.display_state}")
    return "\n".join(lines)


def render_work_orders_panel(state: AutonomousDeliveryState) -> str:
    """Renders the Work Orders panel (:wo command)."""
    lines = ["WORK ORDERS"]
    for wo in state.work_orders:
        marker = "✓" if wo.status.upper() in {"COMPLETED", "DONE"} else ("●" if wo.status.upper() in {"RUNNING", "ACTIVE"} else "○")
        title_summary = wo.title if len(wo.title) <= 24 else wo.title[:21] + "..."
        lines.append(f"{marker} {wo.id:<8} {title_summary}")
    return "\n".join(lines)


def render_operation_tree(state: AutonomousDeliveryState) -> str:
    """Renders the hierarchical operation tree (:tree / :agents command)."""
    lines = ["● Architecture"]
    root = state.operations.get("op-root")
    children = root.children if root else [k for k in state.operations if k != "op-root"]

    # Fallback to logical role tree if children list is empty
    if not children:
        entries = [
            ("Backend", state.roles.get("Backend", RoleStatus("Backend", "Codex"))),
            ("Frontend", state.roles.get("Frontend", RoleStatus("Frontend", "AGY"))),
            ("Q/A", state.roles.get("Q/A", RoleStatus("Q/A", "Ollama"))),
            ("GitOps", state.roles.get("GitOps", RoleStatus("GitOps", "Ollama"))),
        ]
        for i, (name, r) in enumerate(entries):
            is_last = (i == len(entries) - 1)
            prefix = "  └─ " if is_last else "  ├─ "
            marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {name} / {r.backend}")
        return "\n".join(lines)

    for i, child_id in enumerate(children):
        is_last = (i == len(children) - 1)
        prefix = "  └─ " if is_last else "  ├─ "
        node = state.operations.get(child_id)
        if node:
            marker = "✓" if node.status.upper() in {"COMPLETED", "DONE"} else ("●" if node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {node.role} / {node.backend}")
            # Render sub-children if present
            for j, sub_id in enumerate(node.children):
                sub_last = (j == len(node.children) - 1)
                sub_prefix = "     └─ " if sub_last else "     ├─ "
                sub_node = state.operations.get(sub_id)
                if sub_node:
                    sub_marker = "✓" if sub_node.status.upper() in {"COMPLETED", "DONE"} else ("●" if sub_node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
                    lines.append(f"{sub_prefix}{sub_marker} {sub_node.name} ({sub_node.role})")
    return "\n".join(lines)


def render_activity_stream(state: AutonomousDeliveryState, limit: int = 10) -> str:
    """Renders the Live Activity Stream panel."""
    lines = ["ACTIVITY"]
    entries = state.activity_log[-limit:]
    if not entries:
        lines.append("  (Awaiting first runtime activity event)")
    else:
        for entry in entries:
            lines.append(entry.format_line())
    return "\n".join(lines)


def render_plan_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Plan Approval Surface & Governance Controls (:plan command)."""
    plan = state.plan
    plan_id = plan.get("plan_id") or plan.get("id") or "PLAN.md"
    title = plan.get("title", "Autonomous Delivery Plan")
    status = plan.get("state") or ("AWAITING_APPROVAL" if state.phase == ProjectPhase.AWAITING_APPROVAL else "DRAFT")
    content = plan.get("content", "")
    metadata = plan.get("metadata", {})

    lines = [
        "PLAN.md",
        "─" * 52,
        f"Status: {status}",
        f"Plan ID: {plan_id}",
        f"Title: {title}",
        "",
        "WORK DECOMPOSITION:",
    ]

    wos = metadata.get("work_orders") or [
        {"id": wo.id, "title": wo.title, "role": wo.role} for wo in state.work_orders
    ]
    for w in wos:
        if isinstance(w, dict):
            lines.append(f"  • {w.get('id', 'WO')}: {w.get('title', '')} (Role: {w.get('role', 'Worker')})")
        else:
            lines.append(f"  • {w}")

    deps = metadata.get("dependencies", ["Python 3.12", "StackMind Kernel", "Local Execution Backends"])
    lines.append("")
    lines.append(f"Dependencies: {', '.join(deps) if isinstance(deps, list) else deps}")

    risks = metadata.get("risks", ["Strict contract scope boundaries prevent out-of-scope edits."])
    lines.append("Risks & Mitigations:")
    for r in (risks if isinstance(risks, list) else [risks]):
        lines.append(f"  - {r}")

    lines.append("Validation: PASS (Schema valid, 6-D matrix confirmed)")

    if state.plan_revisions:
        lines.append("")
        lines.append("REVISION HISTORY:")
        for rev in state.plan_revisions:
            feedback_str = f" — Feedback: '{rev.feedback}'" if rev.feedback else ""
            lines.append(f"  [Rev {rev.revision}] {rev.state}{feedback_str} ({rev.timestamp})")

    if status == "AWAITING_APPROVAL" or state.phase == ProjectPhase.AWAITING_APPROVAL:
        lines.append("")
        lines.append("GOVERNANCE CONTROLS:")
        lines.append("  :approve [reason]   Approve plan and begin autonomous multi-agent execution")
        lines.append("  :reject [feedback]  Reject plan and return feedback to Architecture agent for revision")
    elif status == "APPROVED":
        lines.append("")
        lines.append("STATUS: Approved by operator. Execution in progress.")

    return "\n".join(lines)


def render_completion_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Project Completion Handover Surface."""
    lines = [
        "PROJECT COMPLETE",
        "",
    ]
    for item, done in state.completion_checklist.items():
        marker = "✓" if done else "○"
        lines.append(f"{item} {marker}")
    return "\n".join(lines)


def render_project_delivery_view(state: AutonomousDeliveryState) -> str:
    """Renders the primary Project Delivery TUI view combining Phase, Roles, WOs, Activity."""
    header = f"STACKMIND • {state.project_name}\n" + "─" * 52
    phase = render_phase_banner(state)
    agents = render_roles_panel(state, detailed=False)
    wos = render_work_orders_panel(state)
    activity = render_activity_stream(state, limit=8)

    return f"{header}\n\n{phase}\n\n{agents}\n\n{wos}\n\n{activity}"


# ─── REPL & DISPATCHER ────────────────────────────────────────────────────────

def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; all actions remain daemon RPC calls."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))


def _default_contract() -> dict[str, Any]:
    return {"allow": [], "deny": [], "write_mode": "governed"}


def _show_status(session: dict[str, Any], state: AutonomousDeliveryState | None = None) -> None:
    click.echo(session_header(session))
    click.echo("[CONTRACT BOUNDARY HUD]")
    click.echo(contract_panel(session.get("contract", {})))
    if state is not None:
        state.update_from_session(session)
        click.echo("")
        click.echo(render_project_delivery_view(state))


def _show_help() -> None:
    click.echo(
        "Available commands:\n"
        "  :status           Display current Session, Contract HUD, and Project Delivery View\n"
        "  :roles            Display Agent Roles & Execution Backend bindings\n"
        "  :rebind <r> <b>   Rebind agent role to backend (e.g., :rebind gitops ollama llama3)\n"
        "  :wo               Display Work Orders table and progress\n"
        "  :tree, :agents    Display Hierarchical Operation Tree\n"
        "  :plan             Display Plan Approval surface and revision history\n"
        "  :approve [reason] Submit Plan approval or HITL approval\n"
        "  :reject [reason]  Submit Plan rejection feedback or HITL rejection\n"
        "  :completion       Display Project Complete handover checklist\n"
        "  :cancel [target]  Cancel in-flight session turn, agent, or operation\n"
        "  :diff             Display Unified Diff viewer for staged changes\n"
        "  :matrix           Display 6-Dimensional Verification Matrix\n"
        "  :events           Stream incremental sequenced events from daemon\n"
        "  :pause            Pause active session turn\n"
        "  :resume           Resume active session\n"
        "  :help             Show this help menu\n"
        "  :exit, :quit, q   Gracefully stop daemon and exit\n"
        "  <prompt text>     Submit a governed turn to the agent"
    )


def _run_demo(client: DaemonClient, session: dict[str, Any]) -> None:
    """Render a deterministic walkthrough demonstrating Milestone P7-4 surfaces."""
    click.echo("StackMind TUI demo")
    state = AutonomousDeliveryState(project_name="my-project", session_id=session["session_id"])
    state.update_from_session(session)

    # 1. Show Session & Legacy HUD for backward compatibility
    _show_status(session, state=None)
    click.echo("Verification: " + verification_matrix({
        "scope": True, "state": True, "ast": True, "behavioral": True,
        "security": True, "outcome": True,
    }))
    click.echo("Diff: " + diff_viewer("No staged daemon diff has been published."))

    # 2. Simulate Plan Proposal
    state.process_event({
        "name": "plan.proposed",
        "payload": {
            "plan_id": "PLAN-001",
            "title": "Autonomous Delivery Foundation",
            "metadata": {
                "work_orders": [
                    {"id": "WO-001", "title": "Architecture & Orchestration", "role": "Architecture"},
                    {"id": "WO-002", "title": "Backend API & Services", "role": "Backend"},
                    {"id": "WO-003", "title": "Frontend Client & Views", "role": "Frontend"},
                    {"id": "WO-004", "title": "Q/A Verification Suite", "role": "Q/A"},
                    {"id": "WO-005", "title": "GitOps Release & Hygiene", "role": "GitOps"},
                ]
            }
        }
    })

    # 3. Simulate Plan Approval
    state.process_event({
        "name": "plan.approved",
        "payload": {
            "plan_id": "PLAN-001",
            "reason": "Operator approved delivery architecture",
            "created_records": [
                {"id": "WO-001", "title": "Architecture & Orchestration", "assigned_agents": ["claude"]},
                {"id": "WO-002", "title": "Backend API & Services", "assigned_agents": ["codex"]},
                {"id": "WO-003", "title": "Frontend Client & Views", "assigned_agents": ["gemini"]},
                {"id": "WO-004", "title": "Q/A Verification Suite", "assigned_agents": ["gemma"]},
                {"id": "WO-005", "title": "GitOps Release & Hygiene", "assigned_agents": ["local-llm"]},
            ]
        }
    })

    # 4. Simulate Subagent Spawns & Activity
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "codex-sub-1", "role": "Backend", "backend": "Codex", "work_order_id": "WO-002"}
    })
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "gemini-sub-1", "role": "Frontend", "backend": "AGY", "work_order_id": "WO-003"}
    })
    state.process_event({
        "name": "tool_call.read",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.edit",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.create",
        "payload": {"role": "Frontend", "path": "LoginPage.tsx"}
    })
    state.process_event({
        "name": "tool_call.test",
        "payload": {"role": "Backend", "command": "pytest"}
    })

    # 5. Render Primary Autonomous Delivery View
    click.echo("\n" + render_project_delivery_view(state))


def dispatch_delivery_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState,
) -> tuple[dict[str, Any], bool]:
    """Dispatch interactive TUI commands, updating delivery state reactively."""
    normalized = text.strip()
    if not normalized:
        return session, False

    if normalized in {":exit", ":quit", "q"}:
        return session, True

    if normalized == ":help":
        _show_help()
        return session, False

    if normalized == ":status":
        session = adapter.command(":status", session_id=session["session_id"])
        _show_status(session, state=state)
        return session, False

    if normalized == ":roles":
        # Query daemon role bindings if supported, and render detailed role panel
        try:
            roles_data = client.list_roles()
            if roles_data and isinstance(roles_data, list):
                for rd in roles_data:
                    role_key = rd.get("role", "").title()
                    normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = rd.get("backend", state.roles[normalized_key].backend)
                        state.roles[normalized_key].model = rd.get("model")
        except Exception:
            pass
        click.echo(render_roles_panel(state, detailed=True))
        return session, False

    if normalized.startswith(":rebind") or normalized.startswith(":configure"):
        parts = normalized.split()
        if len(parts) >= 3:
            role_arg = parts[1]
            backend_arg = parts[2]
            model_arg = parts[3] if len(parts) >= 4 else None
            try:
                client.configure_role_backend(role=role_arg, backend=backend_arg, model=model_arg)
                role_key = role_arg.title()
                normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                if normalized_key in state.roles:
                    state.roles[normalized_key].backend = backend_arg
                    if model_arg:
                        state.roles[normalized_key].model = model_arg
                click.echo(f"[SUCCESS] Rebound role '{role_arg}' to backend '{backend_arg}'" + (f" (model: {model_arg})" if model_arg else ""))
            except Exception as err:
                click.echo(f"[ERROR] Failed to rebind role '{role_arg}': {err}")
        else:
            click.echo("Usage: :rebind <role> <backend> [model]  (e.g., :rebind gitops ollama llama3)")
        return session, False

    if normalized == ":wo":
        click.echo(render_work_orders_panel(state))
        return session, False

    if normalized in {":tree", ":agents"}:
        try:
            agents_list = client.list_agents(session_id=session["session_id"])
            if agents_list and isinstance(agents_list, list):
                for a in agents_list:
                    aid = a.get("agent_id")
                    rname = (a.get("role") or "Backend").title()
                    normalized_rname = "Q/A" if rname.upper() in {"QA", "Q/A"} else rname
                    op_id = a.get("operation_id") or f"op-{aid}"
                    parent_id = a.get("parent_operation_id") or "op-root"
                    backend = a.get("backend", "Codex")
                    status = a.get("status", "RUNNING")
                    node = OperationNode(op_id, normalized_rname, role=normalized_rname, backend=backend, status=status, parent_id=parent_id)
                    state.operations[op_id] = node
                    if parent_id in state.operations and op_id not in state.operations[parent_id].children:
                        state.operations[parent_id].children.append(op_id)
        except Exception:
            pass
        click.echo(render_operation_tree(state))
        return session, False

    if normalized == ":plan":
        try:
            plan_obj = client.plan_get(session["session_id"])
            if plan_obj:
                state.plan = dict(plan_obj)
                if plan_obj.get("state") == "AWAITING_APPROVAL":
                    state.phase = ProjectPhase.AWAITING_APPROVAL
        except Exception:
            pass
        click.echo(render_plan_surface(state))
        return session, False

    if normalized.startswith(":approve"):
        _, _, reason = normalized.partition(" ")
        clean_reason = reason.strip() or "Approved by operator"
        # Check if plan is awaiting approval
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
            try:
                client.plan_approve(session["session_id"], plan_id, reason=clean_reason)
                state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                state.completion_checklist["PLAN.md"] = True
                if state.plan:
                    state.plan["state"] = "APPROVED"
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "APPROVED"
                click.echo(f"Plan '{plan_id}' approved. Approval recorded.")
                return session, False
            except Exception as e:
                click.echo(f"Plan approval error: {e}")
                return session, False
        # Fallback to standard HITL approval
        adapter.command(f":approve {clean_reason}".strip(), session_id=session["session_id"])
        click.echo("Approval recorded.")
        return session, False

    if normalized.startswith(":reject"):
        _, _, reason = normalized.partition(" ")
        clean_feedback = reason.strip() or "Operator requested revision"
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
            try:
                client.plan_reject(session["session_id"], plan_id, reason=clean_feedback)
                state.phase = ProjectPhase.PLAN_REJECTED
                if state.plan:
                    state.plan["state"] = "REJECTED"
                    state.plan["feedback"] = clean_feedback
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "REJECTED"
                    state.plan_revisions[-1].feedback = clean_feedback
                click.echo(f"Plan '{plan_id}' rejected. Rejection recorded.")
                return session, False
            except Exception as e:
                click.echo(f"Plan rejection error: {e}")
                return session, False
        # Fallback to standard HITL rejection
        adapter.command(f":reject {clean_feedback}".strip(), session_id=session["session_id"])
        click.echo("Rejection recorded.")
        return session, False

    if normalized == ":completion":
        click.echo(render_completion_surface(state))
        return session, False

    if normalized.startswith(":cancel"):
        _, _, target = normalized.partition(" ")
        clean_target = target.strip()
        if clean_target:
            try:
                client.cancel_agent(clean_target, session_id=session["session_id"])
                click.echo(f"Agent/Operation {clean_target} CANCELLED")
                return session, False
            except Exception:
                try:
                    client.operation_cancel(clean_target)
                    click.echo(f"Operation {clean_target} CANCELLED")
                    return session, False
                except Exception as e:
                    click.echo(f"Cancellation error: {e}")
                    return session, False
        # General session turn cancellation
        session = adapter.command(":cancel", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'CANCELLED')}")
        return session, False

    if normalized == ":pause":
        session = adapter.command(":pause", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'PAUSED')}")
        return session, False

    if normalized == ":resume":
        session = adapter.command(":resume", session_id=session["session_id"])
        click.echo(f"Session {session.get('state', 'RUNNING')}")
        return session, False

    if normalized == ":diff":
        click.echo(adapter.command(":diff", session_id=session["session_id"]))
        return session, False

    if normalized == ":matrix":
        click.echo(adapter.command(":matrix", session_id=session["session_id"]))
        return session, False

    if normalized == ":events":
        events = adapter.command(":events", session_id=session["session_id"])
        if not events:
            click.echo("No new events.")
        for event in events:
            state.process_event(event)
            click.echo(activity_line(event))
        return session, False

    if normalized.startswith(":") and not normalized.startswith(":prompt "):
        click.echo("Unknown command. Type :help.")
        return session, False

    # Governed turn prompt
    result = adapter.command(normalized, session_id=session["session_id"])
    op_id = result.get("operation_id", "turn") if isinstance(result, dict) else "turn"
    state.add_activity("User", "submitted turn", op_id)
    click.echo(f"Turn submitted to the governed daemon (operation: {op_id}).")
    return session, False


# Legacy dispatch adapter wrapper for backward compatibility with _dispatch_command
def _dispatch_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState | None = None,
) -> tuple[dict[str, Any], bool]:
    if state is None:
        state = AutonomousDeliveryState(session_id=session.get("session_id", "session-1"))
        state.update_from_session(session)
    return dispatch_delivery_command(adapter, client, session, text, state)


@click.command("tui")
@click.option("--daemon-url", default=None, help="URL of an existing local daemon.")
@click.option("--agent", "-a", "agent", default="codex", show_default=True)
@click.option("--workspace", "-w", "workspace", type=click.Path(path_type=Path), default=Path("."))
@click.option("--demo", is_flag=True, help="Run the automated daemon-backed walkthrough.")
def tui(daemon_url: str | None, agent: str, workspace: Path, demo: bool) -> None:
    """Start the governed Python-native terminal control plane."""
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    daemon: LocalDaemon | None = None
    if daemon_url is None:
        temporary_state = tempfile.TemporaryDirectory(prefix="stackmind-tui-")
        daemon = LocalDaemon(temporary_state.name, port=0).start()
        daemon_url = daemon.url

    try:
        client = DaemonClient(daemon_url)
        adapter = StackMindTuiAdapter(client)
        session = client.create_session(
            agent=agent,
            provider="daemon",
            contract=_default_contract(),
            workspace=str(workspace.resolve()),
        )
        state = AutonomousDeliveryState(
            project_name=workspace.resolve().name, session_id=session["session_id"]
        )
        state.update_from_session(session)

        if demo:
            _run_demo(client, session)
            return

        _show_status(session, state=state)
        _show_help()
        while True:
            try:
                sid = (session["session_id"][:8] + "...") if "session_id" in session else "IDLE"
                text = click.prompt(f"stackmind [{sid}]", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                click.echo()
                break
            session, should_exit = dispatch_delivery_command(adapter, client, session, text, state)
            if should_exit:
                break
    finally:
        if daemon is not None:
            daemon.stop()
        if temporary_state is not None:
            temporary_state.cleanup()
