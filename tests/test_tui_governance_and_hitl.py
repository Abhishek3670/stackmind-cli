"""Automated test suite for StackMind TUI Contextual Governance Panels & HITL Plan Actions (WO-032)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.tui import (
    AutonomousDeliveryState,
    ProjectPhase,
    create_tui_adapter,
    dispatch_delivery_command,
    render_contract_hud_str,
    render_plan_panel_str,
    render_roles_panel,
    render_verification_matrix_str,
    render_work_orders_panel,
)
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


class MockDaemonClient:
    """Mock DaemonClient recording HITL approval, rejection, and plan calls."""

    def __init__(self) -> None:
        self.plan_approvals: list[tuple[str, str, str]] = []
        self.plan_rejections: list[tuple[str, str, str]] = []
        self.session_approvals: list[tuple[str, bool, str]] = []
        self.plan_data: dict[str, Any] = {
            "plan_id": "PLAN-032",
            "title": "Phase 4 Governance & HITL Plan",
            "state": "AWAITING_APPROVAL",
            "metadata": {
                "work_orders": [
                    {"id": "WO-030", "title": "Landing & Chat", "role": "Backend"},
                    {"id": "WO-031", "title": "Events & Diff", "role": "Backend"},
                    {"id": "WO-032", "title": "Governance & HITL", "role": "Backend"},
                ]
            },
        }

    def plan_get(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        return self.plan_data

    def plan_approve(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
        self.plan_approvals.append((session_id, plan_id, reason))
        self.plan_data["state"] = "APPROVED"
        return {"session_id": session_id, "plan_id": plan_id, "status": "APPROVED", "reason": reason}

    def plan_reject(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
        self.plan_rejections.append((session_id, plan_id, reason))
        self.plan_data["state"] = "REJECTED"
        return {"session_id": session_id, "plan_id": plan_id, "status": "REJECTED", "reason": reason}

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        self.session_approvals.append((session_id, approved, reason))
        return {"session_id": session_id, "approved": approved, "reason": reason}

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "state": "RUNNING",
            "provider": "daemon",
            "contract": {
                "write_mode": "governed",
                "allow": ["cli/tui/*", "tests/*"],
                "deny": [".sync/runtime/boot/*"],
            },
        }

    def list_roles(self) -> list[dict[str, Any]]:
        return [
            {"role": "Backend", "backend": "Codex", "model": "gpt-4o"},
            {"role": "Frontend", "backend": "AGY", "model": "claude-3-5"},
        ]

    def list_agents(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {"agent_id": "codex-1", "role": "Backend", "status": "RUNNING", "backend": "Codex"}
        ]

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return []


def test_6d_verification_matrix_rendering():
    """Verify 6-dimensional verification matrix renders PASS and FAIL correctly."""
    # 1. All pass
    all_pass = render_verification_matrix_str({
        "scope": True,
        "state": True,
        "ast": True,
        "behavioral": True,
        "security": True,
        "outcome": True,
    })
    assert "6D VERIFICATION MATRIX" in all_pass
    assert "Scope: PASS" in all_pass
    assert "State: PASS" in all_pass
    assert "AST: PASS" in all_pass
    assert "Behavioral: PASS" in all_pass
    assert "Security: PASS" in all_pass
    assert "Outcome: PASS" in all_pass
    assert "✓ All 6 verification dimensions passed" in all_pass

    # 2. Selective failure
    partial_fail = render_verification_matrix_str({
        "scope": True,
        "state": True,
        "ast": False,
        "behavioral": True,
        "security": False,
        "outcome": True,
    })
    assert "AST: FAIL" in partial_fail
    assert "Security: FAIL" in partial_fail
    assert "Scope: PASS" in partial_fail
    assert "✗ One or more verification gates failed" in partial_fail


def test_state_verification_event_processing():
    """Verify AutonomousDeliveryState updates verification dimensions upon events."""
    state = AutonomousDeliveryState(project_name="my-app")
    assert state.verification_dimensions["ast"] is True

    # Process failure event
    state.process_event({
        "name": "verification.completed",
        "payload": {
            "result": {
                "scope": True,
                "ast": False,
                "behavioral": True,
                "security": True,
            }
        }
    })
    assert state.verification_dimensions["ast"] is False
    assert state.verification_dimensions["scope"] is True


def test_contract_boundary_hud_rendering():
    """Verify Contract HUD renders write mode, allow paths, and deny boundaries."""
    contract = {
        "write_mode": "governed",
        "allow": [
            {"path": "cli/tui/*", "ops": ["read", "write"]},
            {"path": "tests/*", "ops": ["read", "write"]},
        ],
        "deny": [
            {"path": ".sync/runtime/boot/*", "reason": "Canonical runtime"},
            {"path": ".sync/work-orders/*", "reason": "Architect authority"},
        ],
        "budget": {"max_tokens": 45000, "max_files_touched": 6},
        "governance": ["CODEX-01", "CONTRACT-01"],
    }
    hud = render_contract_hud_str(contract)
    assert "[CONTRACT BOUNDARY HUD]" in hud
    assert "GOVERNED" in hud
    assert "cli/tui/*" in hud
    assert "[read, write]" in hud
    assert ".sync/runtime/boot/*" in hud
    assert "Canonical runtime" in hud
    assert "45000 tokens" in hud
    assert "CODEX-01" in hud


def test_plan_hitl_approval_dispatch():
    """Verify :approve command calls daemon plan_approve RPC and records confirmation."""
    client = MockDaemonClient()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    session = {"session_id": "session-42", "contract": {}}
    state = AutonomousDeliveryState(session_id="session-42")
    state.plan = dict(client.plan_data)
    state.phase = ProjectPhase.AWAITING_APPROVAL

    # Plan surface displays controls
    plan_text = render_plan_panel_str(state)
    assert "PLAN READY" in plan_text
    assert "PLAN-032" in plan_text
    assert "AWAITING_APPROVAL" in plan_text
    assert ":approve" in plan_text
    assert ":reject" in plan_text

    # Execute :approve
    sess, should_exit = dispatch_delivery_command(
        adapter, client, session, ":approve looks good to proceed", state  # type: ignore[arg-type]
    )
    assert not should_exit
    assert client.plan_approvals == [("session-42", "PLAN-032", "looks good to proceed")]
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION
    assert state.completion_checklist["PLAN.md"] is True
    # Verify inline assistant confirmation in conversation history
    assert state.has_conversation
    assert "Plan 'PLAN-032' approved" in state.messages[-1].content


def test_plan_hitl_rejection_dispatch():
    """Verify :reject command calls daemon plan_reject RPC and records revision feedback."""
    client = MockDaemonClient()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    session = {"session_id": "session-42", "contract": {}}
    state = AutonomousDeliveryState(session_id="session-42")
    state.plan = dict(client.plan_data)
    state.phase = ProjectPhase.AWAITING_APPROVAL

    sess, should_exit = dispatch_delivery_command(
        adapter, client, session, ":reject add integration tests", state  # type: ignore[arg-type]
    )
    assert not should_exit
    assert client.plan_rejections == [("session-42", "PLAN-032", "add integration tests")]
    assert state.phase == ProjectPhase.PLAN_REJECTED
    # Verify inline assistant rejection feedback in conversation history
    assert state.has_conversation
    assert "Plan 'PLAN-032' rejected" in state.messages[-1].content


def test_contextual_delivery_panels_inspection():
    """Verify contextual delivery inspection commands (:roles, :wo, :tree, :matrix, :contract)."""
    state = AutonomousDeliveryState(project_name="my-project")

    # 1. Roles panel
    roles = render_roles_panel(state, detailed=True)
    assert "AGENT ROLES & EXECUTION BACKENDS" in roles
    assert "Architecture Agent" in roles
    assert "Backend Agent" in roles

    # 2. Work orders panel
    wos = render_work_orders_panel(state)
    assert "WORK ORDERS" in wos
    assert "WO-001" in wos
    assert "Architecture & Orches" in wos

    # 3. Contract HUD string
    hud = render_contract_hud_str({"write_mode": "governed", "allow": ["src/*"]})
    assert "Write Mode: GORVERNED" not in hud  # check spelling
    assert "Write Mode:" in hud
    assert "src/*" in hud


def test_tui_repl_governance_and_hitl_interactive(tmp_path: Path):
    """Verify interactive REPL routes :matrix, :contract, :plan, and HITL commands."""
    user_inputs = "\n".join([
        ":contract",
        ":matrix",
        ":plan",
        ":approve ready for execution",
        ":reject needs revisions",
        ":exit",
    ]) + "\n"

    result = CliRunner().invoke(
        cli,
        ["tui", "--workspace", str(tmp_path)],
        input=user_inputs,
    )
    assert result.exit_code == 0, result.output
    # Contract HUD is displayed
    assert "[CONTRACT BOUNDARY HUD]" in result.output
    # 6D Matrix is displayed
    assert "Scope: PASS" in result.output
    # HITL feedback is recorded
    assert "Approval recorded." in result.output
    assert "Rejection recorded." in result.output
