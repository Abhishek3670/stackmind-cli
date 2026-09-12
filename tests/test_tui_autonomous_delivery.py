"""Automated test suite for Milestone P7-4: Autonomous Delivery TUI & Control Plane Integration.

Tests:
1. Project Delivery Layout rendering (Phase Banner, Roles Panel, Work Orders Panel, Operation Tree, Activity Stream).
2. Plan Approval Surface & Governance Controls (Plan view, Interactive Approve/Reject, Revision history loop).
3. Completion Handover Surface (PROJECT COMPLETE checklist).
4. TUI Command Dispatcher (:roles, :wo, :agents, :tree, :plan, :completion, :cancel).
5. Reactive Event Processor (plan.proposed, plan.approved, plan.rejected, event.agentSpawned, operation.started, operation.completed, tool.call).
6. End-to-end LocalDaemon integration walkthrough of autonomous delivery flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.tui.app import (
    ActivityEntry,
    AutonomousDeliveryState,
    OperationNode,
    PlanRevision,
    ProjectPhase,
    RoleStatus,
    WorkOrderItem,
    dispatch_delivery_command,
    render_activity_stream,
    render_completion_surface,
    render_operation_tree,
    render_phase_banner,
    render_plan_surface,
    render_project_delivery_view,
    render_roles_panel,
    render_work_orders_panel,
)
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


class _MockDaemonClient:
    """Mock DaemonClient for headless unit testing of delivery commands."""

    def __init__(self) -> None:
        self.approvals: list[tuple[str, bool, str]] = []
        self.plan_approvals: list[tuple[str, str, str]] = []
        self.plan_rejections: list[tuple[str, str, str]] = []
        self.cancelled_agents: list[tuple[str, str]] = []
        self.cancelled_ops: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.plans: dict[str, dict[str, Any]] = {
            "PLAN-001": {
                "plan_id": "PLAN-001",
                "title": "Autonomous Multi-Agent Architecture",
                "state": "AWAITING_APPROVAL",
                "metadata": {
                    "work_orders": [
                        {"id": "WO-001", "title": "Architecture & Planning", "role": "Architecture"},
                        {"id": "WO-002", "title": "Backend API Service", "role": "Backend"},
                        {"id": "WO-003", "title": "Frontend UI Client", "role": "Frontend"},
                    ],
                    "dependencies": ["Python 3.12", "SQLite"],
                    "risks": ["Child scope boundary drift"],
                },
            }
        }
        self.agents: list[dict[str, Any]] = [
            {"agent_id": "claude-arch", "role": "Architecture", "backend": "Claude", "status": "RUNNING", "operation_id": "op-root"},
            {"agent_id": "codex-backend", "role": "Backend", "backend": "Codex", "status": "RUNNING", "operation_id": "op-backend", "parent_operation_id": "op-root"},
            {"agent_id": "gemini-frontend", "role": "Frontend", "backend": "AGY", "status": "RUNNING", "operation_id": "op-frontend", "parent_operation_id": "op-root"},
        ]
        self.roles: list[dict[str, Any]] = [
            {"role": "Architecture", "backend": "Claude", "model": "claude-3-5-sonnet"},
            {"role": "Backend", "backend": "Codex", "model": "codex-v1"},
            {"role": "Frontend", "backend": "AGY", "model": "gemini-2.5-flash"},
            {"role": "Q/A", "backend": "Ollama", "model": "qwen2.5-coder"},
            {"role": "GitOps", "backend": "Ollama", "model": "llama3.2"},
        ]

    def plan_get(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        del session_id
        if plan_id:
            return self.plans[plan_id]
        return list(self.plans.values())[-1]

    def plan_approve(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
        self.plan_approvals.append((session_id, plan_id, reason))
        self.plans[plan_id]["state"] = "APPROVED"
        return {"session_id": session_id, "plan_id": plan_id, "approved": True}

    def plan_reject(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
        self.plan_rejections.append((session_id, plan_id, reason))
        self.plans[plan_id]["state"] = "REJECTED"
        self.plans[plan_id]["feedback"] = reason
        return {"session_id": session_id, "plan_id": plan_id, "rejected": True, "feedback": reason}

    def list_roles(self) -> list[dict[str, Any]]:
        return self.roles

    def list_agents(self, session_id: str | None = None, operation_id: str | None = None) -> list[dict[str, Any]]:
        del session_id, operation_id
        return self.agents

    def cancel_agent(self, agent_id: str, session_id: str | None = None, reason: str = "user_cancelled", cascade: bool = True) -> dict[str, Any]:
        del cascade
        self.cancelled_agents.append((agent_id, reason))
        return {"agent_id": agent_id, "status": "CANCELLED"}

    def operation_cancel(self, operation_id: str, cascade: bool = True) -> dict[str, Any]:
        del cascade
        self.cancelled_ops.append(operation_id)
        return {"operation_id": operation_id, "status": "CANCELLED"}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "state": "RUNNING",
            "provider": "daemon",
            "agent": "claude",
            "workspace": ".",
            "plans": self.plans,
            "contract": {"allow": ["cli/tui/*"], "deny": ["boot/*"], "write_mode": "governed"},
        }

    def turn(self, session_id: str, prompt: str, **params: object) -> dict[str, object]:
        del params
        self.turns.append((session_id, prompt))
        return {"operation_id": "op-turn-1"}

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        self.approvals.append((session_id, approved, reason))
        return {"session_id": session_id, "approved": approved, "reason": reason}

    def pause(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "state": "PAUSED"}

    def resume(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "state": "RUNNING"}

    def cancel(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "state": "CANCELLED"}

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        del session_id, after
        return []


# ─── 1. LAYOUT & PANELS RENDERING TESTS ───────────────────────────────────────

def test_phase_status_banner_transitions():
    state = AutonomousDeliveryState("demo-project")

    # Initial / Awaiting Approval
    state.phase = ProjectPhase.AWAITING_APPROVAL
    banner = render_phase_banner(state)
    assert "PHASE" in banner
    assert "● Planning & Architecture (AWAITING_APPROVAL)" in banner
    assert "○ Autonomous Execution" in banner
    assert "○ Project Complete" in banner

    # Autonomous Execution
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
    banner_exec = render_phase_banner(state)
    assert "✓ PLAN approved" in banner_exec
    assert "● Autonomous Execution" in banner_exec
    assert "○ Project Complete" in banner_exec

    # Rejected Plan
    state.phase = ProjectPhase.PLAN_REJECTED
    banner_rej = render_phase_banner(state)
    assert "✗ PLAN rejected (Revising)" in banner_rej

    # Project Complete
    state.phase = ProjectPhase.PROJECT_COMPLETE
    banner_comp = render_phase_banner(state)
    assert "✓ PLAN approved" in banner_comp
    assert "✓ Autonomous Execution" in banner_comp
    assert "✓ Project Complete" in banner_comp


def test_roles_panel_rendering_compact_and_detailed():
    state = AutonomousDeliveryState("demo-project")
    state.roles["Backend"].state = "RUNNING"
    state.roles["Frontend"].state = "RUNNING"
    state.roles["Q/A"].state = "WAITING"
    state.roles["GitOps"].state = "WAITING"

    # Compact dashboard view
    compact = render_roles_panel(state, detailed=False)
    assert "AGENTS" in compact
    assert "● Backend          implementing" in compact
    assert "● Frontend         implementing" in compact
    assert "○ Q/A              waiting" in compact
    assert "○ GitOps           waiting" in compact

    # Detailed :roles view
    detailed = render_roles_panel(state, detailed=True)
    assert "=== AGENT ROLES & EXECUTION BACKENDS (:roles) ===" in detailed
    assert "Backend Agent" in detailed
    assert "Role: Backend" in detailed
    assert "Backend: Codex" in detailed
    assert "Work Order: WO-002" in detailed
    assert "State: RUNNING" in detailed


def test_work_orders_panel_rendering():
    state = AutonomousDeliveryState("demo-project")
    state.work_orders[0].status = "COMPLETED"
    state.work_orders[1].status = "RUNNING"
    state.work_orders[2].status = "WAITING"

    wo_text = render_work_orders_panel(state)
    assert "WORK ORDERS" in wo_text
    assert "✓ WO-001" in wo_text
    assert "● WO-002" in wo_text
    assert "○ WO-003" in wo_text


def test_operation_tree_hierarchical_rendering():
    state = AutonomousDeliveryState("demo-project")
    # Add child operations under op-root
    state.operations["op-backend"] = OperationNode(
        "op-backend", "Backend", role="Backend", backend="Codex", status="RUNNING", parent_id="op-root"
    )
    state.operations["op-frontend"] = OperationNode(
        "op-frontend", "Frontend", role="Frontend", backend="AGY", status="RUNNING", parent_id="op-root"
    )
    state.operations["op-qa"] = OperationNode(
        "op-qa", "Q/A", role="Q/A", backend="Ollama", status="WAITING", parent_id="op-root"
    )
    state.operations["op-gitops"] = OperationNode(
        "op-gitops", "GitOps", role="GitOps", backend="Ollama", status="WAITING", parent_id="op-root"
    )
    state.operations["op-root"].children = ["op-backend", "op-frontend", "op-qa", "op-gitops"]

    tree_str = render_operation_tree(state)
    assert "● Architecture" in tree_str
    assert "├─ ● Backend / Codex" in tree_str
    assert "├─ ● Frontend / AGY" in tree_str
    assert "├─ ○ Q/A / Ollama" in tree_str
    assert "└─ ○ GitOps / Ollama" in tree_str


def test_live_activity_stream_rendering():
    state = AutonomousDeliveryState("demo-project")
    state.add_activity("Backend", "read", "auth/service.py")
    state.add_activity("Backend", "edit", "auth/service.py")
    state.add_activity("Frontend", "create", "LoginPage.tsx")
    state.add_activity("Backend", "pytest", "tests/test_auth.py")

    act_str = render_activity_stream(state, limit=10)
    assert "ACTIVITY" in act_str
    assert "Backend    read auth/service.py" in act_str
    assert "Backend    edit auth/service.py" in act_str
    assert "Frontend   create LoginPage.tsx" in act_str
    assert "Backend    pytest tests/test_auth.py" in act_str


def test_unified_project_delivery_view_rendering():
    state = AutonomousDeliveryState("sample-repo")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
    state.add_activity("Architecture", "dispatched work orders")

    view = render_project_delivery_view(state)
    assert "STACKMIND • sample-repo" in view
    assert "PHASE" in view
    assert "✓ PLAN approved" in view
    assert "● Autonomous Execution" in view
    assert "AGENTS" in view
    assert "WORK ORDERS" in view
    assert "ACTIVITY" in view


# ─── 2. PLAN APPROVAL SURFACE & REVISION CONTROLS ─────────────────────────────

def test_plan_surface_awaiting_approval_and_controls():
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {
        "plan_id": "PLAN-V1",
        "title": "Delivery Architecture & Work Orders",
        "state": "AWAITING_APPROVAL",
        "metadata": {
            "work_orders": [
                {"id": "WO-001", "title": "Setup Service Core", "role": "Backend"},
                {"id": "WO-002", "title": "Build Web Views", "role": "Frontend"},
            ],
            "dependencies": ["Python 3.12", "Textual"],
            "risks": ["Contract violation prevents out-of-scope edits"],
        },
    }
    state.plan_revisions.append(
        PlanRevision(revision=1, state="AWAITING_APPROVAL", plan_id="PLAN-V1", timestamp="12:00")
    )

    plan_str = render_plan_surface(state)
    assert "PLAN.md" in plan_str
    assert "Status: AWAITING_APPROVAL" in plan_str
    assert "Plan ID: PLAN-V1" in plan_str
    assert "Setup Service Core" in plan_str
    assert "Build Web Views" in plan_str
    assert "Dependencies: Python 3.12, Textual" in plan_str
    assert "Contract violation" in plan_str
    assert "GOVERNANCE CONTROLS:" in plan_str
    assert ":approve [reason]" in plan_str
    assert ":reject [feedback]" in plan_str
    assert "[Rev 1] AWAITING_APPROVAL" in plan_str


def test_plan_rejection_and_revision_loop():
    state = AutonomousDeliveryState("revision-project")

    # 1. Propose Rev 1
    state.process_event({
        "name": "plan.proposed",
        "payload": {"plan_id": "PLAN-100", "title": "Initial Plan"}
    })
    assert state.phase == ProjectPhase.AWAITING_APPROVAL
    assert len(state.plan_revisions) == 1

    # 2. Reject Rev 1 with feedback
    state.process_event({
        "name": "plan.rejected",
        "payload": {"plan_id": "PLAN-100", "reason": "Add integration test work order"}
    })
    assert state.phase == ProjectPhase.PLAN_REJECTED
    assert state.plan["feedback"] == "Add integration test work order"
    assert state.plan_revisions[0].state == "REJECTED"
    assert state.plan_revisions[0].feedback == "Add integration test work order"

    # 3. Propose Rev 2
    state.process_event({
        "name": "plan.proposed",
        "payload": {"plan_id": "PLAN-100", "title": "Revised Plan with Integration Tests"}
    })
    assert state.phase == ProjectPhase.AWAITING_APPROVAL
    assert len(state.plan_revisions) == 2
    assert state.plan_revisions[1].state == "AWAITING_APPROVAL"

    # 4. Approve Rev 2
    state.process_event({
        "name": "plan.approved",
        "payload": {"plan_id": "PLAN-100", "reason": "Looks complete"}
    })
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION
    assert state.plan_revisions[1].state == "APPROVED"
    assert state.completion_checklist["PLAN.md"] is True


def test_project_completion_checklist_surface():
    state = AutonomousDeliveryState("done-project")
    state.phase = ProjectPhase.PROJECT_COMPLETE
    for k in state.completion_checklist:
        state.completion_checklist[k] = True

    comp_str = render_completion_surface(state)
    assert "PROJECT COMPLETE" in comp_str
    assert "PLAN.md ✓" in comp_str
    assert "Backend ✓" in comp_str
    assert "Frontend ✓" in comp_str
    assert "Q/A ✓" in comp_str
    assert "GitOps ✓" in comp_str
    assert "README.md ✓" in comp_str
    assert "Tests ✓" in comp_str
    assert "Git ✓" in comp_str


# ─── 3. TUI COMMANDS ROUTING & EXECUTION ─────────────────────────────────────

def test_dispatch_delivery_commands():
    mock_client = _MockDaemonClient()
    adapter = StackMindTuiAdapter(mock_client)  # type: ignore[arg-type]
    session = mock_client.get_session("session-test-1")
    state = AutonomousDeliveryState("test-cmd", session_id="session-test-1")

    # :status
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":status", state)  # type: ignore[arg-type]
    assert not should_exit

    # :roles
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":roles", state)  # type: ignore[arg-type]
    assert not should_exit

    # :wo
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":wo", state)  # type: ignore[arg-type]
    assert not should_exit

    # :tree / :agents
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":tree", state)  # type: ignore[arg-type]
    assert not should_exit
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":agents", state)  # type: ignore[arg-type]
    assert not should_exit

    # :plan
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":plan", state)  # type: ignore[arg-type]
    assert not should_exit

    # :completion
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":completion", state)  # type: ignore[arg-type]
    assert not should_exit

    # :cancel with target agent
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":cancel codex-backend", state)  # type: ignore[arg-type]
    assert not should_exit
    assert mock_client.cancelled_agents == [("codex-backend", "user_cancelled")]

    # :exit
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":exit", state)  # type: ignore[arg-type]
    assert should_exit is True


def test_dispatch_plan_approve_and_reject_commands():
    mock_client = _MockDaemonClient()
    adapter = StackMindTuiAdapter(mock_client)  # type: ignore[arg-type]
    session = mock_client.get_session("session-test-2")
    state = AutonomousDeliveryState("test-plan-ctrl", session_id="session-test-2")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001", "state": "AWAITING_APPROVAL"}

    # Approve
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":approve ship it", state)  # type: ignore[arg-type]
    assert not should_exit
    assert mock_client.plan_approvals == [("session-test-2", "PLAN-001", "ship it")]
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION

    # Reset to Awaiting Approval and test Reject
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan["state"] = "AWAITING_APPROVAL"
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":reject add auth", state)  # type: ignore[arg-type]
    assert not should_exit
    assert mock_client.plan_rejections == [("session-test-2", "PLAN-001", "add auth")]
    assert state.phase == ProjectPhase.PLAN_REJECTED


# ─── 4. REACTIVE SSE EVENT PROCESSOR TESTS ────────────────────────────────────

def test_reactive_events_update_delivery_state():
    state = AutonomousDeliveryState("reactive-project")

    # 1. Plan proposed
    state.process_event({
        "name": "plan.proposed",
        "payload": {
            "plan_id": "PLAN-042",
            "title": "Distributed Delivery",
            "metadata": {"work_orders": [{"id": "WO-101", "title": "DB Migration", "role": "Backend"}]}
        }
    })
    assert state.phase == ProjectPhase.AWAITING_APPROVAL
    assert any("PLAN-042" in act.action for act in state.activity_log)

    # 2. Subagent spawned
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {
            "agent_id": "codex-child-9",
            "role": "Backend",
            "backend": "Codex",
            "work_order_id": "WO-101",
            "operation_id": "op-backend-child"
        }
    })
    assert state.roles["Backend"].backend == "Codex"
    assert state.roles["Backend"].work_order_id == "WO-101"
    assert "op-backend-child" in state.operations
    assert state.operations["op-backend-child"].role == "Backend"

    # 3. Operation started
    state.process_event({
        "name": "operation.started",
        "payload": {"role": "Backend", "operation": "apply_migrations", "work_order_id": "WO-101"}
    })
    assert state.roles["Backend"].state == "RUNNING"
    assert any("apply_migrations" in act.target for act in state.activity_log)

    # 4. Tool calls
    state.process_event({
        "name": "tool_call.read_file",
        "payload": {"role": "Backend", "path": "migrations/001.sql"}
    })
    state.process_event({
        "name": "tool_call.write_file",
        "payload": {"role": "Backend", "path": "src/schema.py"}
    })
    assert state.activity_log[-2].action == "read_file"
    assert state.activity_log[-1].action == "write_file"

    # 5. Operation completed
    state.process_event({
        "name": "operation.completed",
        "payload": {"role": "Backend", "operation": "apply_migrations", "work_order_id": "WO-101"}
    })
    assert state.roles["Backend"].state == "COMPLETED"
    assert state.completion_checklist["Backend"] is True

    # 6. Project completed
    state.process_event({"name": "project.completed", "payload": {}})
    assert state.phase == ProjectPhase.PROJECT_COMPLETE
    assert state.is_complete is True
    assert all(state.completion_checklist.values())


# ─── 5. LIVE LOCALDAEMON E2E CONTROL PLANE WALKTHROUGH ────────────────────────

def test_tui_local_daemon_autonomous_delivery_lifecycle(tmp_path: Path):
    """End-to-end headless integration: LocalDaemon + DaemonClient + Autonomous Delivery TUI."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        client = DaemonClient(daemon.url)
        adapter = StackMindTuiAdapter(client)

        session = client.create_session(
            agent="claude",
            provider="daemon",
            contract={"allow": ["src/*", "tests/*"], "deny": ["boot/*"], "write_mode": "governed"},
            workspace=str(tmp_path),
        )
        sid = session["session_id"]
        state = AutonomousDeliveryState("live-daemon-project", session_id=sid)

        # 1. Propose Plan via daemon RPC
        client.plan_propose(
            session_id=sid,
            plan_id="PLAN-AUTO-1",
            title="E2E Autonomous Multi-Agent Delivery",
            content="Full-stack autonomous feature delivery plan",
            metadata={
                "work_orders": [
                    {"id": "WO-901", "title": "Core API", "assigned_agents": ["codex"], "role": "Backend"},
                    {"id": "WO-902", "title": "Web UI", "assigned_agents": ["gemini"], "role": "Frontend"},
                ]
            }
        )

        # Sync events from daemon
        for evt in client.events(sid):
            state.process_event(evt)
        assert state.phase == ProjectPhase.AWAITING_APPROVAL

        # 2. Inspect with :plan command
        dispatch_delivery_command(adapter, client, session, ":plan", state)

        # 3. Approve Plan via :approve command
        dispatch_delivery_command(adapter, client, session, ":approve looks excellent", state)
        assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION

        # Verify created work orders exist in daemon
        plan_record = client.plan_get(sid, "PLAN-AUTO-1")
        assert plan_record["state"] == "APPROVED"
        assert len(plan_record["created_work_orders"]) == 2

        # 4. Dispatch child subagent operation through daemon
        cancel_evt, op_id = daemon.manager.begin_operation(
            session_id=sid,
            operation_name="implement_backend_api",
            role="backend",
            agent_id="codex-worker-1",
            work_order_id="WO-901",
        )
        del cancel_evt

        # Sync events
        for evt in client.events(sid):
            state.process_event(evt)

        # 5. Inspect :roles and :tree
        dispatch_delivery_command(adapter, client, session, ":roles", state)
        dispatch_delivery_command(adapter, client, session, ":tree", state)

        # 6. Complete operation
        daemon.manager.complete_operation(
            session_id=sid,
            operation_id=op_id,
            result={"status": "SUCCESS", "files": ["src/api.py"]},
        )
        daemon.manager.events.publish("operation.completed", sid, role="backend", operation="implement_backend_api", work_order_id="WO-901")

        # Sync completion event
        for evt in client.events(sid):
            state.process_event(evt)
        assert state.completion_checklist["Backend"] is True

        # 7. Deliver Project Complete
        daemon.manager.events.publish("project.completed", sid)
        for evt in client.events(sid):
            state.process_event(evt)
        assert state.phase == ProjectPhase.PROJECT_COMPLETE

        # Verify completion checklist rendering
        comp_text = render_completion_surface(state)
        assert "PROJECT COMPLETE" in comp_text
        assert "PLAN.md ✓" in comp_text
        assert "Backend ✓" in comp_text


def test_tui_cli_repl_autonomous_delivery_commands(tmp_path: Path):
    """Verify CliRunner execution of new P7-4 colon commands."""
    with LocalDaemon(tmp_path / "daemon", port=0) as daemon:
        user_inputs = "\n".join([
            ":status",
            ":roles",
            ":wo",
            ":tree",
            ":agents",
            ":plan",
            ":completion",
            ":exit",
        ]) + "\n"

        result = CliRunner().invoke(
            cli,
            ["tui", "--daemon-url", daemon.url, "--workspace", str(tmp_path)],
            input=user_inputs,
        )

        assert result.exit_code == 0, result.output
        assert "PHASE" in result.output
        assert "AGENTS" in result.output
        assert "WORK ORDERS" in result.output
        assert "ACTIVITY" in result.output
        assert "=== AGENT ROLES & EXECUTION BACKENDS (:roles) ===" in result.output
        assert "● Architecture" in result.output
        assert "PLAN.md" in result.output
        assert "PROJECT COMPLETE" in result.output
