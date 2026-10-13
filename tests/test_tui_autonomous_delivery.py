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

from cli.init import init
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
    get_status_str,
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
        self.turns.append((session_id, prompt))
        if not hasattr(self, "turn_calls"):
            self.turn_calls = []
        self.turn_calls.append({"session_id": session_id, "prompt": prompt, "params": params})
        return {"operation_id": "op-turn-1"}

    def operation_get(self, operation_id: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "status": "COMPLETED", "result": {"summary": "Goal planned"}}

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

    # :goal validation (empty)
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":goal", state)  # type: ignore[arg-type]
    assert not should_exit
    assert any("Usage: :goal" in msg.content for msg in state.messages)

    # :goal routing to Architecture
    s, should_exit = dispatch_delivery_command(adapter, mock_client, session, ":goal Build user authentication", state)  # type: ignore[arg-type]
    assert not should_exit
    assert any(op.role == "Architecture" for op in state.operations.values())
    assert any(a.action == "submitted goal to Architecture" for a in state.activity_log)
    goal_call = mock_client.turn_calls[-1]
    assert goal_call["params"]["role"] == "architecture"
    assert goal_call["params"]["agent_id"] == "claude"

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
    init(tmp_path, name="TestProject", no_git=True)
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


def test_tui_state_modular_extraction_and_backward_compatibility():
    """Verify clean state extraction into cli.tui.state and backward-compatible re-exports."""
    import cli.tui as tui_pkg
    import cli.tui.app as tui_app
    import cli.tui.state as tui_state

    # Verify identical class identities across state, app, and package root
    exported_symbols = [
        "ActivityEntry",
        "AutonomousDeliveryState",
        "OperationNode",
        "PlanRevision",
        "ProjectPhase",
        "RoleStatus",
        "WorkOrderItem",
    ]
    for sym in exported_symbols:
        state_cls = getattr(tui_state, sym)
        app_cls = getattr(tui_app, sym)
        pkg_cls = getattr(tui_pkg, sym)
        assert state_cls is app_cls, f"{sym} mismatch between state.py and app.py"
        assert state_cls is pkg_cls, f"{sym} mismatch between state.py and __init__.py"

    # Verify instantiation and methods in extracted state
    state = tui_state.AutonomousDeliveryState(project_name="test-proj", session_id="test-session")
    assert state.phase == tui_state.ProjectPhase.INITIALIZING
    assert state.project_name == "test-proj"
    assert state.session_id == "test-session"

    # RoleStatus display_state
    rs = tui_state.RoleStatus("Backend", backend="Codex", state="RUNNING")
    assert rs.display_state == "implementing"
    rs_arch = tui_state.RoleStatus("Architecture", backend="Claude", state="RUNNING")
    assert rs_arch.display_state == "orchestrating"
    rs_done = tui_state.RoleStatus("Frontend", backend="AGY", state="COMPLETED")
    assert rs_done.display_state == "completed"

    # ActivityEntry format_line
    entry = tui_state.ActivityEntry("2026-09-13T12:00:00", "Backend", "edit", "cli/tui/state.py")
    line = entry.format_line()
    assert "12:00" in line
    assert "Backend" in line
    assert "edit cli/tui/state.py" in line


def test_tui_populate_from_runtime_syncs_disk_work_orders_and_plan(tmp_path: Path) -> None:
    """populate_from_runtime correctly loads active work orders and PLAN.md from disk."""
    import yaml
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "title": "Install dependencies",
        "assigned_agents": ["codex"],
        "priority": "P0",
        "status": "COMPLETED",
    }), encoding="utf-8")
    (active_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002",
        "title": "Build FastAPI login backend",
        "assigned_agents": ["codex"],
        "priority": "P0",
        "status": "RUNNING",
    }), encoding="utf-8")
    (tmp_path / ".sync" / "PLAN.md").write_text("# Project Plan\n\nLogin implementation plan.", encoding="utf-8")

    state = AutonomousDeliveryState(project_name="clean-test", session_id="sess-123")
    state.populate_from_runtime(workspace=tmp_path)

    assert len(state.work_orders) == 2
    assert state.work_orders[0].id == "WO-001"
    assert state.work_orders[0].title == "Install dependencies"
    assert state.work_orders[1].id == "WO-002"
    assert state.work_orders[1].title == "Build FastAPI login backend"
    assert state.completion_checklist["PLAN.md"] is True
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION


def test_clean_workspace_initializes_with_no_work_orders(tmp_path: Path) -> None:
    """When starting in a clean workspace with no work orders, work_orders is empty and panel renders 'No active orders'."""
    import yaml
    from cli.tui.runtime_panel import format_work_orders
    from cli.tui.state import AutonomousDeliveryState
    from cli.tui.views import render_work_orders_panel

    # Case 1: Workspace with scaffolded empty INDEX.yaml (like stackmind init)
    sync_wos = tmp_path / ".sync" / "work-orders"
    sync_wos.mkdir(parents=True, exist_ok=True)
    (sync_wos / "INDEX.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "orders": [],
            "total_active": 0,
            "total_completed": 0,
            "total_blocked": 0,
        }),
        encoding="utf-8",
    )

    state = AutonomousDeliveryState(project_name="clean-proj", session_id="sess-clean", work_orders=[])
    state.populate_from_runtime(workspace=tmp_path)

    assert state.work_orders == []
    for r in state.roles.values():
        assert r.work_order_id is None

    wo_panel = render_work_orders_panel(state)
    assert "No active orders" in wo_panel

    formatted = format_work_orders(state.work_orders)
    assert any("No active orders" in str(line) for line in formatted)

    # Case 2: Even if created without work_orders=[], populate_from_runtime clears dummy placeholders in clean ws
    state2 = AutonomousDeliveryState(project_name="clean-proj-2", session_id="sess-clean-2")
    state2.populate_from_runtime(workspace=tmp_path)
    assert state2.work_orders == []
    for r in state2.roles.values():
        assert r.work_order_id is None


def test_tui_update_from_session_clears_idle_op_root() -> None:
    """When a session has no active_operation, op-root is cleared so current_operation is None."""
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(project_name="clean-test", session_id="sess-123")
    assert state.get_current_operation() is not None  # Default initial op-root is RUNNING

    # Updating with session that has active_operation: None clears op-root
    state.update_from_session({"session_id": "sess-123", "state": "RUNNING", "active_operation": None})
    assert state.get_current_operation() is None


def test_tui_blocked_phase_surfaced_in_state_and_banner() -> None:
    """A blocked daemon run must be surfaced in state.phase and render_phase_banner."""
    from cli.tui.app import render_phase_banner
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    state = AutonomousDeliveryState(project_name="clean-test", session_id="sess-123")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION

    session = {
        "session_id": "sess-123",
        "phase": "BLOCKED",
        "error": "Recovery repair for WO-011 could not start — repair attempts exhausted (2/2)",
        "run": {
            "phase": "BLOCKED",
            "error": "Recovery repair for WO-011 could not start — repair attempts exhausted (2/2)",
        },
    }
    state.update_from_session(session)

    assert state.phase == ProjectPhase.BLOCKED
    assert state.error == "Recovery repair for WO-011 could not start — repair attempts exhausted (2/2)"

    banner = render_phase_banner(state)
    assert "PHASE: BLOCKED" in banner
    assert "⊘ Autonomous Execution (BLOCKED)" in banner
    assert "Blocker: Recovery repair for WO-011 could not start — repair attempts exhausted (2/2)" in banner
    assert "Use :resume to continue" in banner


def test_tui_blocked_event_updates_state() -> None:
    """Processing run.blocked event must update state.phase and state.error."""
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    state = AutonomousDeliveryState(project_name="clean-test", session_id="sess-123")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION

    event = {
        "sequence": 1,
        "name": "run.blocked",
        "payload": {
            "run_id": "run-6b31b8f5",
            "phase": "BLOCKED",
            "error": "Recovery repair for WO-011 could not start",
        },
    }
    state.process_event(event)

    assert state.phase == ProjectPhase.BLOCKED
    assert state.error == "Recovery repair for WO-011 could not start"


def test_tui_current_operation_shows_blocked_when_runtime_blocked() -> None:
    """Current operation section must display ⊘ Blocked when runtime is in BLOCKED phase."""
    from cli.tui.runtime_panel import format_current_operation
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    state = AutonomousDeliveryState(project_name="clean-test", session_id="sess-123")
    state.phase = ProjectPhase.BLOCKED

    lines = format_current_operation(None, state=state)
    assert any("Blocked" in line.plain for line in lines)


# ─── 6. :STATUS COMMAND TESTS ──────────────────────────────────────────────────

def test_status_idle_state():
    """Test :status output for idle state - no session header line."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.INITIALIZING
    session = {"session_id": "test-session", "state": "RUNNING", "provider": "daemon"}

    output = get_status_str(session, state=state)
    # Session metadata should NOT appear in output
    assert "Session" not in output
    assert "test-session" not in output
    assert "provider" not in output
    # Status fields should render correctly
    assert "Status: planning" in output
    assert "Next: Waiting for work..." in output


def test_status_awaiting_approval():
    """Test :status output for awaiting approval state - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001"}
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: awaiting_approval" in output
    assert "Plan: awaiting approval" in output
    assert "Next: Approve or reject pending plan" in output


def test_status_active_with_progress():
    """Test :status output for active execution with progress - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
    state.work_orders[0].status = "COMPLETED"
    state.work_orders[1].status = "RUNNING"
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: active" in output
    assert "Progress: 1/5 WOs completed" in output


def test_status_blocked():
    """Test :status output for blocked state - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.BLOCKED
    state.error = "Recovery repair failed (2/2)"
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: blocked" in output
    assert "Blocker: Recovery repair failed (2/2)" in output
    assert "Next: Review and resolve blocker, then :run" in output


def test_status_failed():
    """Test :status output for failed state - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.FAILED
    state.error = "Critical error during execution"
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: failed" in output
    assert "Next: Review error, then :run or :cancel" in output


def test_status_project_complete():
    """Test :status output for project complete state - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.PROJECT_COMPLETE
    state.is_complete = True
    for k in state.completion_checklist:
        state.completion_checklist[k] = True
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: complete" in output
    assert "Next: Project delivered" in output


def test_status_plan_rejected():
    """Test :status output for rejected plan state - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.PLAN_REJECTED
    state.plan = {"feedback": "Needs more detail"}
    session = {"session_id": "test-session", "state": "RUNNING"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Status: rejected" in output
    assert "Plan: rejected" in output
    assert "Next: Revise plan and resubmit" in output


def test_status_with_blocked_work_orders():
    """Test :status output when individual work orders are blocked - no session header."""
    state = AutonomousDeliveryState("test-project")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
    state.work_orders[0].status = "BLOCKED"
    state.work_orders[1].status = "RUNNING"
    session = {"session_id": "test-session"}

    output = get_status_str(session, state=state)
    assert "Session" not in output
    assert "Blocker: Blocked WOs: WO-001" in output


# ─── 7. SIDE PANEL UPDATE AFTER PLAN APPROVAL ────────────────────────────────

def test_side_panel_updates_after_plan_approve():
    """After :approve, state should reflect AUTONOMOUS_EXECUTION with active agents."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    mock_client = _MockDaemonClient()
    adapter = StackMindTuiAdapter(mock_client)  # type: ignore[arg-type]
    session = mock_client.get_session("session-test-approve")
    state = AutonomousDeliveryState("test-approve", session_id="session-test-approve")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001", "state": "AWAITING_APPROVAL"}
    state.work_orders[0].status = "WAITING"
    state.roles["Backend"].state = "WAITING"

    # Run :approve command
    s, should_exit = dispatch_delivery_command(
        adapter, mock_client, session, ":approve ship it", state
    )  # type: ignore[arg-type]

    assert not should_exit
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION
    assert state.plan["state"] == "APPROVED"
    assert state.completion_checklist["PLAN.md"] is True

    # Side panel data should be updated (work orders and roles reflect execution state)
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION


def test_side_panel_reflects_work_order_transitions():
    """Work-order state changes should be visible in side panel data."""
    state = AutonomousDeliveryState("test-wo")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION

    # Initial state
    assert state.work_orders[0].status == "RUNNING"

    # Simulate work order completion (as would happen from daemon events)
    state.work_orders[0].status = "COMPLETED"
    state.work_orders[1].status = "RUNNING"

    # State should reflect the changes
    assert state.work_orders[0].status == "COMPLETED"
    assert state.work_orders[1].status == "RUNNING"


def test_agent_transitions_visible_in_state():
    """Agent state transitions (waiting→running→completed) should be tracked."""
    state = AutonomousDeliveryState("test-transitions")

    # Initial state
    assert state.roles["Backend"].state == "WAITING"

    # Simulate backend starting work
    state.roles["Backend"].state = "RUNNING"
    assert state.roles["Backend"].state == "RUNNING"

    # Simulate backend completion
    state.roles["Backend"].state = "COMPLETED"
    assert state.roles["Backend"].state == "COMPLETED"


def test_blocked_state_update_in_state():
    """Blocked/failed transitions should be visible in state without :status."""
    state = AutonomousDeliveryState("test-blocked")
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION

    # Block the run
    state.phase = ProjectPhase.BLOCKED
    state.error = "Recovery failed"

    assert state.phase == ProjectPhase.BLOCKED
    assert state.error == "Recovery failed"


def test_rejected_plan_state_update():
    """Rejected plan state should be visible without running :status."""
    state = AutonomousDeliveryState("test-rejected")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001"}

    state.phase = ProjectPhase.PLAN_REJECTED
    state.plan["state"] = "REJECTED"
    state.plan["feedback"] = "Needs revision"

    assert state.phase == ProjectPhase.PLAN_REJECTED
    assert state.plan["state"] == "REJECTED"


def test_status_command_not_required_for_state_propagation():
    """State updates should propagate without needing to run :status."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase

    mock_client = _MockDaemonClient()
    adapter = StackMindTuiAdapter(mock_client)  # type: ignore[arg-type]
    session = mock_client.get_session("session-test-prop")
    state = AutonomousDeliveryState("test-prop", session_id="session-test-prop")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001"}

    # Approve plan
    dispatch_delivery_command(adapter, mock_client, session, ":approve", state)  # type: ignore[arg-type]

    # State should be updated immediately (no need for :status)
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION
    assert state.plan["state"] == "APPROVED"


def test_approve_in_interactive_terminal_streams_execution(monkeypatch):
    """In interactive terminal, :approve should enter _stream_turn_execution."""
    import sys
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState, ProjectPhase
    import cli.tui.commands as cmd_mod

    mock_client = _MockDaemonClient()
    adapter = StackMindTuiAdapter(mock_client)  # type: ignore[arg-type]
    session = mock_client.get_session("session-test-stream")
    state = AutonomousDeliveryState("test-stream", session_id="session-test-stream")
    state.phase = ProjectPhase.AWAITING_APPROVAL
    state.plan = {"plan_id": "PLAN-001"}

    streamed_calls = []

    def fake_stream(*args, **kwargs):
        streamed_calls.append((args, kwargs))
        return session, False

    monkeypatch.setattr(cmd_mod, "_stream_turn_execution", fake_stream)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    dispatch_delivery_command(adapter, mock_client, session, ":approve ready", state)
    assert len(streamed_calls) == 1
    _, kwargs = streamed_calls[0]
    assert kwargs.get("op_id") == "turn"
    assert kwargs.get("is_tty") is True


def test_keyboard_raw_prompt_input_invokes_on_idle_when_no_input(monkeypatch):
    """raw_prompt_input should call on_idle callback when no keystroke is received."""
    import sys
    from cli.tui import keyboard

    idle_calls = []

    def fake_idle(editor):
        idle_calls.append(editor)
        # Stop after 2 idle ticks by raising KeyboardInterrupt
        if len(idle_calls) >= 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(keyboard, "read_next_key", lambda *args, **kwargs: None)

    with pytest.raises(KeyboardInterrupt):
        keyboard.raw_prompt_input(on_idle=fake_idle)

    assert len(idle_calls) >= 2


def test_all_agents_and_roles_transition_to_completed_on_project_delivery():
    """Verify that after project delivery, Backend and Architecture transition to completed instead of implementing/orchestrating."""
    from cli.tui.state import AutonomousDeliveryState, RoleStatus, WorkOrderItem, ProjectPhase
    from cli.tui.views import render_roles_panel, render_operation_tree

    state = AutonomousDeliveryState(
        roles={
            "Architecture": RoleStatus("Architecture", backend="Claude", state="RUNNING"),
            "Backend": RoleStatus("Backend", backend="Ollama", model="gemma4:31b-cloud", state="RUNNING"),
            "Frontend": RoleStatus("Frontend", backend="AGY", state="COMPLETED"),
            "Q/A": RoleStatus("Q/A", backend="Ollama", state="COMPLETED"),
            "GitOps": RoleStatus("GitOps", backend="Ollama", state="COMPLETED"),
        },
        work_orders=[
            WorkOrderItem("WO-001", "Backend Task", role="Backend", status="COMPLETED"),
            WorkOrderItem("WO-002", "Frontend Task", role="Frontend", status="COMPLETED"),
        ]
    )

    # 1. Test per-role reconciliation: Backend has all completed work orders
    state.reconcile_roles_from_work_orders()
    assert state.roles["Backend"].state == "COMPLETED"
    assert state.roles["Backend"].display_state == "completed"

    # Reset Backend to RUNNING to verify project completion event transition
    state.roles["Backend"].state = "RUNNING"
    assert state.roles["Backend"].display_state == "implementing"

    # 2. Process run.complete event
    state.process_event({
        "name": "run.complete",
        "payload": {"status": "SUCCESS"},
    })

    assert state.is_complete is True
    assert state.phase == ProjectPhase.PROJECT_COMPLETE
    assert state.roles["Backend"].state == "COMPLETED"
    assert state.roles["Backend"].display_state == "completed"
    assert state.roles["Architecture"].state == "COMPLETED"
    assert state.roles["Architecture"].display_state == "completed"

    # 3. Test agent hierarchy tree status
    hierarchy = state.get_agent_hierarchy()
    for entry in hierarchy:
        assert entry["status"] == "completed", f"Entry {entry['name']} had status {entry['status']}"

    # 4. Test render_roles_panel
    rendered = render_roles_panel(state)
    assert "✓ Architecture" in rendered
    assert "✓ Backend" in rendered
    assert "implementing" not in rendered
    assert "orchestrating" not in rendered
    assert "[ollama/gemma4:31b-cloud]" not in rendered

    # 5. Test render_operation_tree
    tree_rendered = render_operation_tree(state)
    assert "✓ Architecture" in tree_rendered
    assert "✓ Backend" in tree_rendered





