"""Regression tests for conversational scoping turns and QA-delegated security checks.

Validates:
1. Normal conversational consultation turns on WO-000 / Architect (Claude) pass
   outcome_verified even when PLAN.md has not yet been authored or modified.
2. Authoring turns by the Architect that modify PLAN.md still enforce full
   deliverable verification.
3. Supervisor in Phase.PLANNING transitions to WAITING_FOR_HUMAN rather than FAILED
   when a planning turn completes conversationally without proposing a plan.
4. Security-related static credential scans are handled by the Q/A agent (Gemma),
   so worker implementation turns (Backend / Codex, Frontend / Gemini) are never
   blocked by credential leak scanning during development.
5. Insecure auth AST scanner strictly ignores non-Python files (e.g. package.json)
   and suppresses false positives on package.json scripts containing "test".
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from validators.harness.runner import HarnessRunner
from validators.harness.runner.models import HarnessDecision, HarnessTask
from validators.harness.snapshot import WorkspaceSnapshot
from validators.kernel.daemon.supervisor import LifecycleSupervisor
from validators.kernel.daemon.supervisor.models import AdvanceResult, Phase, RunState
from validators.kernel.security import scan_for_credential_leaks, scan_for_insecure_auth


# ─── 1. CONVERSATIONAL SCOPING TURN RESILIENCE (WO-000 / ARCHITECT) ───────────

def test_architect_conversational_turn_without_plan_passes_outcome_verified(tmp_path: Path):
    """Conversational scoping message on WO-000 passes outcome_verified with summary."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".sync").mkdir()

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".sync").mkdir()

    runner = HarnessRunner(ws, agent="claude")
    before_snap = WorkspaceSnapshot.capture(ws)
    after_snap = WorkspaceSnapshot.capture(staged)
    diff = before_snap.diff(after_snap)

    task = HarnessTask(
        kind="work_order",
        identifier="WO-000",
        work_order_id="WO-000",
        path=ws / "PLAN.md",
        deliverable_path="PLAN.md",
        deliverable_type="doc",
        title="System architecture & execution plan",
        body="Architectural design",
        query="",
    )

    decision = HarnessDecision(
        status="completed",
        summary=(
            "Hey! Good to see you. I'm Claude, Lead Architect. "
            "What are you building today? Let's co-design the architecture."
        ),
        report_markdown="",
        blockers=(),
        modified_files=(),
        release_target="PLAN.md",
        retrieval_queries=(),
        uncertainty=(),
    )

    staged_errors: list[str] = []
    dimensions = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=staged,
        staged_errors=staged_errors,
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=False,
    )

    assert dimensions.outcome_verified is True
    assert dimensions.security_verified is True
    assert not any("declared deliverable 'PLAN.md' was not added or modified" in err for err in staged_errors)
    assert not any("Deliverable verification failed" in err for err in staged_errors)


def test_architect_turn_with_plan_enforces_deliverable_verification(tmp_path: Path):
    """When the Architect actually authors PLAN.md, deliverable verification is enforced."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".sync").mkdir()

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".sync").mkdir()
    # Write a valid markdown plan
    (staged / "PLAN.md").write_text(
        "# Calculator Architecture & Execution Plan\n\n"
        "## Architecture Overview\n"
        "Engine: src/calculator.py\n"
        "UI: assets/index.html\n"
        "QA: tests/test_calculator.py\n",
        encoding="utf-8",
    )

    runner = HarnessRunner(ws, agent="claude")
    before_snap = WorkspaceSnapshot.capture(ws)
    after_snap = WorkspaceSnapshot.capture(staged)
    diff = before_snap.diff(after_snap)

    task = HarnessTask(
        kind="work_order",
        identifier="WO-000",
        work_order_id="WO-000",
        path=ws / "PLAN.md",
        deliverable_path="PLAN.md",
        deliverable_type="doc",
        title="System architecture & execution plan",
        body="Architectural design",
        query="",
    )

    decision = HarnessDecision(
        status="completed",
        summary="Authored full PLAN.md with milestones and contracts.",
        report_markdown="",
        blockers=(),
        modified_files=("PLAN.md",),
        release_target="PLAN.md",
        retrieval_queries=(),
        uncertainty=(),
    )

    staged_errors: list[str] = []
    dimensions = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=staged,
        staged_errors=staged_errors,
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=True,
    )

    assert dimensions.outcome_verified is True
    assert dimensions.security_verified is True
    assert len(staged_errors) == 0


# ─── 2. SUPERVISOR PLANNING PHASE CONVERSATIONAL RESILIENCE ──────────────────

def test_supervisor_planning_phase_conversational_turn_waits_for_human():
    """When a planning turn completes conversationally without a plan, it waits for human."""
    mock_mgr = MagicMock()
    mock_mgr.list_plans.return_value = []  # No plan proposed yet
    mock_mgr.get_operation.return_value = {
        "operation_id": "op-plan-001",
        "status": "COMPLETED",
        "result": {"status": "completed", "summary": "Clarifying requirements with user."},
    }

    supervisor = LifecycleSupervisor(mock_mgr)
    state = RunState(
        run_id="run-conv-001",
        session_id="sess-001",
        product_goal="hey",
        workspace=".",
        phase=Phase.PLANNING,
        planning_operation_id="op-plan-001",
    )

    result = supervisor._advance_planning(state)

    # Supervisor must NOT fail the run; it pauses in WAITING_FOR_HUMAN for user input
    assert result == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.planning_operation_id is None
    assert state.interrupted_phase == Phase.PLANNING.value
    assert state.error is None


# ─── 3. SECURITY CHECK DELEGATION TO QA AGENT ────────────────────────────────

def test_backend_worker_package_json_not_blocked_by_security_scan(tmp_path: Path):
    """Backend worker writing package.json with scripts.test is never blocked by security_verified."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".sync").mkdir()

    pkg_json_content = json.dumps({
        "name": "calculator",
        "version": "1.0.0",
        "scripts": {
            "test": "vitest run",
            "build": "vite build"
        }
    }, indent=2)

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".sync").mkdir()
    (staged / "package.json").write_text(pkg_json_content, encoding="utf-8")

    # Backend worker agent (Codex)
    runner = HarnessRunner(ws, agent="codex")
    before_snap = WorkspaceSnapshot.capture(ws)
    after_snap = WorkspaceSnapshot.capture(staged)
    diff = before_snap.diff(after_snap)

    task = HarnessTask(
        kind="work_order",
        identifier="WO-001",
        work_order_id="WO-001",
        path=ws / "package.json",
        deliverable_path="package.json",
        deliverable_type="code",
        title="Setup project dependencies",
        body="Initialize package.json",
        query="",
    )

    decision = HarnessDecision(
        status="completed",
        summary="Created package.json with vitest scripts.",
        report_markdown="",
        blockers=(),
        modified_files=("package.json",),
        release_target="package.json",
        retrieval_queries=(),
        uncertainty=(),
    )

    staged_errors: list[str] = []
    dimensions = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=staged,
        staged_errors=staged_errors,
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=True,
    )

    assert dimensions.security_verified is True
    assert dimensions.outcome_verified is True
    assert not any("credential leak detected" in err for err in staged_errors)


def test_qa_agent_actively_enforces_security_verified_on_real_leak(tmp_path: Path):
    """QA agent (Gemma) actively checks and blocks on real credential leaks."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".sync").mkdir()

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / ".sync").mkdir()
    (staged / "auth.py").write_text(
        "API_KEY = 'sk-proj-abc123456789012345678901234567890'\n",
        encoding="utf-8",
    )

    # QA agent (Gemma)
    runner = HarnessRunner(ws, agent="gemma")
    before_snap = WorkspaceSnapshot.capture(ws)
    after_snap = WorkspaceSnapshot.capture(staged)
    diff = before_snap.diff(after_snap)

    task = HarnessTask(
        kind="work_order",
        identifier="WO-003",
        work_order_id="WO-003",
        work_order_type="VALIDATION",
        path=ws / "tests" / "test_auth.py",
        deliverable_path="tests/test_auth.py",
        deliverable_type="code",
        title="Security and test audit",
        body="Verify credentials",
        query="",
    )

    decision = HarnessDecision(
        status="completed",
        summary="Security scan completed.",
        report_markdown="",
        blockers=(),
        modified_files=("auth.py",),
        release_target="auth.py",
        retrieval_queries=(),
        uncertainty=(),
    )

    staged_errors: list[str] = []
    dimensions = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=staged,
        staged_errors=staged_errors,
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=True,
    )

    assert dimensions.security_verified is False
    assert any("credential leak detected in auth.py" in err for err in staged_errors)


# ─── 4. INSECURE AUTH SCANNER NON-PYTHON & SCRIPT SUPPRESSION ────────────────

def test_scan_for_insecure_auth_ignores_json_and_yaml():
    """Insecure auth AST scanner strictly ignores non-Python files like package.json."""
    pkg_json = """{
      "name": "calc",
      "scripts": {
        "test": "vitest run"
      }
    }"""
    assert scan_for_insecure_auth(pkg_json, file_path="package.json") == []
    assert scan_for_credential_leaks(pkg_json, file_path="package.json") == []

    yaml_manifest = """
    name: test
    scripts:
      test: pytest
    """
    assert scan_for_insecure_auth(yaml_manifest, file_path="config.yaml") == []


def test_scan_for_insecure_auth_detects_real_python_hardcoded_credentials():
    """Insecure auth AST scanner detects real hardcoded credentials in Python files."""
    code = (
        "VALID_USERS = {\n"
        "    'admin': 'admin123',\n"
        "    'root': 'secret',\n"
        "}\n"
    )
    findings = scan_for_insecure_auth(code, file_path="src/auth.py")
    assert len(findings) >= 1
    assert any("Hardcoded credential dictionary" in f for f in findings)
