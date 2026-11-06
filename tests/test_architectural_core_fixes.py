"""Tests for core architectural fixes from postmortem run-c5968e29:
1. Deliverable deduplication (completed deliverable cancellation, cross-milestone deduplication, revive guard).
2. Product git commit on feature completion (commit_feature_deliverable and archive_completed_work_orders).
3. Active human notification and working set pruning on WAITING_FOR_HUMAN.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
import yaml
import pytest

from validators.kernel.daemon.authoring import (
    reconcile_active_work_orders_with_plan,
)
from validators.kernel.daemon.supervisor.release import (
    archive_completed_work_orders,
    commit_feature_deliverable,
)
from validators.kernel.daemon.supervisor.models import (
    Phase,
    RunState,
)
from validators.kernel.daemon.supervisor.core import LifecycleSupervisor


def test_completed_deliverable_deduplication(tmp_path: Path):
    """Active work orders claiming already-completed deliverables must be cancelled

    with dependencies rewired and never resurrected on subsequent reconciliation turns.
    """
    plan_content = """# Project Plan: Calculator

## Current Architecture
Node.js web application.

## Milestones & Roadmap
- [ ] Milestone 1: Math engine (Agent: codex)
  - [ ] Task 1.1: Implement src/engine.js
- [ ] Milestone 2: Duplicate math engine task (Agent: codex)
  - [ ] Task 2.1: Implement src/engine.js
- [ ] Milestone 3: Web UI (Agent: gemini)
  - [ ] Task 3.1: In src/ui.js, import engine
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    sync_dir = tmp_path / ".sync"
    act_dir = sync_dir / "work-orders" / "ACTIVE"
    comp_dir = sync_dir / "work-orders" / "COMPLETED"
    contracts_dir = sync_dir / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # WO-008 is ALREADY COMPLETED in COMPLETED/
    (comp_dir / "WO-008.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-008",
            "milestone_id": "Milestone 1",
            "title": "Core math engine",
            "status": "COMPLETED",
            "assigned_agents": ["codex"],
            "deliverable": {"type": "code", "path": "src/engine.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # WO-014 is ACTIVE in ACTIVE/ claiming the same deliverable src/engine.js
    (act_dir / "WO-014.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-014",
            "milestone_id": "Milestone 2",
            "title": "Core math engine duplicate",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/engine.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # WO-015 depends on WO-014
    (act_dir / "WO-015.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-015",
            "milestone_id": "Milestone 3",
            "title": "Web UI",
            "status": "ACTIVE",
            "assigned_agents": ["gemini"],
            "dependencies": ["WO-014"],
            "deliverable": {"type": "code", "path": "src/ui.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # Pass 1: Reconcile
    reconciled = reconcile_active_work_orders_with_plan(tmp_path)

    # WO-014 must be CANCELLED and superseded by WO-008
    wo14 = yaml.safe_load((act_dir / "WO-014.yaml").read_text(encoding="utf-8"))
    assert wo14["status"] == "CANCELLED"
    assert "WO-008" in str(wo14.get("cancellation_reason") or wo14.get("error"))

    # WO-015 must have its dependency rewired to WO-008
    wo15 = yaml.safe_load((act_dir / "WO-015.yaml").read_text(encoding="utf-8"))
    assert wo15["status"] == "ACTIVE"
    assert "WO-008" in wo15["dependencies"]
    assert "WO-014" not in wo15["dependencies"]

    # Pass 2: Second reconciliation must NOT revive WO-014
    reconcile_active_work_orders_with_plan(tmp_path)
    wo14_after = yaml.safe_load((act_dir / "WO-014.yaml").read_text(encoding="utf-8"))
    assert wo14_after["status"] == "CANCELLED"


def test_cross_milestone_duplicate_worker_deliverable_deduplication(tmp_path: Path):
    """When multiple active worker work orders claim the same code deliverable across milestones,

    the canonical one survives and duplicates are cancelled with dependents rewired.
    """
    plan_content = """# Project Plan: Calculator

## Current Architecture
Node.js web application.

## Milestones & Roadmap
- [ ] Milestone 1: Math engine (Agent: codex)
  - [ ] Task 1.1: Implement src/calc.js
- [ ] Milestone 2: Extended math engine (Agent: codex)
  - [ ] Task 2.1: Implement src/calc.js
- [ ] Milestone 3: Web UI (Agent: gemini)
  - [ ] Task 3.1: In src/ui.js, import calc
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # WO-002 (Milestone 1, codex, deliverable src/calc.js)
    (act_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "milestone_id": "Milestone 1",
            "title": "Math engine",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/calc.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # WO-004 (Milestone 2, codex, duplicate deliverable src/calc.js)
    (act_dir / "WO-004.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-004",
            "milestone_id": "Milestone 2",
            "title": "Extended math engine",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/calc.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # WO-005 (Milestone 3, depends on WO-004)
    (act_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-005",
            "milestone_id": "Milestone 3",
            "title": "Web UI",
            "status": "ACTIVE",
            "assigned_agents": ["gemini"],
            "dependencies": ["WO-004"],
            "deliverable": {"type": "code", "path": "src/ui.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    reconciled = reconcile_active_work_orders_with_plan(tmp_path)

    # WO-004 is duplicate of WO-002; WO-004 must be cancelled
    wo4 = yaml.safe_load((act_dir / "WO-004.yaml").read_text(encoding="utf-8"))
    assert wo4["status"] == "CANCELLED"
    assert "WO-002" in str(wo4.get("cancellation_reason") or wo4.get("error"))

    # WO-002 remains ACTIVE
    wo2 = yaml.safe_load((act_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert wo2["status"] == "ACTIVE"

    # WO-005 rewired to WO-002
    wo5 = yaml.safe_load((act_dir / "WO-005.yaml").read_text(encoding="utf-8"))
    assert wo5["status"] == "ACTIVE"
    assert "WO-002" in wo5["dependencies"]
    assert "WO-004" not in wo5["dependencies"]


def test_product_git_commit_on_feature_completion(tmp_path: Path):
    """Archiving a completed work order must commit its deliverable file to the product git repo."""
    # 1. Initialize git repo in tmp_path
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@stackmind.dev"], cwd=str(tmp_path), check=True)

    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# Test Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=str(tmp_path), check=True)

    # 2. Worker authors deliverable
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    engine_file = src_dir / "engine.js"
    engine_file.write_text("export const add = (a, b) => a + b;\n", encoding="utf-8")

    # 3. Setup active work order
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    act_dir.mkdir(parents=True, exist_ok=True)
    wo_data = {
        "id": "WO-008",
        "title": "Core math engine",
        "status": "ACTIVE",
        "assigned_agents": ["codex"],
        "deliverable": {"type": "code", "path": "src/engine.js"},
        "revision": 1,
    }
    (act_dir / "WO-008.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

    # 4. Archive completed work order
    archived = archive_completed_work_orders(tmp_path, ["WO-008"])
    assert "WO-008" in archived

    # 5. Verify product git commit was created
    log_proc = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    commit_msg = log_proc.stdout
    assert "feat(WO-008): Core math engine" in commit_msg
    assert "Work-Order: WO-008" in commit_msg
    assert "Delivered-By: codex" in commit_msg

    # 6. Verify completed work order record has product_commit_sha
    comp_file = tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-008.yaml"
    assert comp_file.is_file()
    comp_data = yaml.safe_load(comp_file.read_text(encoding="utf-8"))
    assert comp_data.get("completion", {}).get("product_commit_sha")


def test_waiting_for_human_notification_and_pruning(tmp_path: Path):
    """Transitioning to WAITING_FOR_HUMAN must write alert files and prune completed IDs from state."""
    sync_dir = tmp_path / ".sync"
    comp_dir = sync_dir / "work-orders" / "COMPLETED"
    comp_dir.mkdir(parents=True, exist_ok=True)

    # Completed work order on disk
    (comp_dir / "WO-008.yaml").write_text(
        yaml.safe_dump({"id": "WO-008", "status": "COMPLETED"}), encoding="utf-8"
    )

    class DummyManager:
        pass

    supervisor = LifecycleSupervisor(DummyManager())
    state = RunState(
        run_id="run-test-alert",
        session_id="test-session",
        workspace=str(tmp_path),
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-008", "WO-014"],
        blocked_wo_ids=["WO-008", "WO-015"],
        completed_wo_ids=["WO-008"],
        product_goal="Test alert goal",
    )

    # Transition to WAITING_FOR_HUMAN
    supervisor._transition(state, Phase.WAITING_FOR_HUMAN, authorized_override=True)

    # 1. State must be pruned of completed WO-008
    assert "WO-008" not in state.worker_wo_ids
    assert state.worker_wo_ids == ["WO-014"]
    assert "WO-008" not in state.blocked_wo_ids
    assert state.blocked_wo_ids == ["WO-015"]

    # 2. Both inbox ALERT.md files must exist
    human_alert = sync_dir / "inbox" / "human" / "ALERT.md"
    ceo_alert = sync_dir / "inbox" / "CEO" / "ALERT.md"
    assert human_alert.is_file()
    assert ceo_alert.is_file()

    alert_text = human_alert.read_text(encoding="utf-8")
    assert "SUPERVISOR HALTED: WAITING FOR HUMAN" in alert_text
    assert "run-test-alert" in alert_text
    assert "stackmind resume" in alert_text
    assert ":resume" in alert_text

# =========================================================================
# Tests for Postmortem run-8b5c360c Architectural Fixes:
# 1. node --test runner recognition
# 2. VALIDATION and RESEARCH deliverable exclusions
# 3. plan_criteria fallback to work orders
# 4. GITOPS bounded retries and verdict invalidation
# 5. Zero-turn phase oscillation loop breaker
# =========================================================================

from validators.harness.integration_gate import IntegrationGate
from validators.harness.delivery_integrity import (
    evaluate_planning_completeness,
    evaluate_release_readiness,
    plan_criteria,
)
from validators.kernel.daemon.supervisor.models import AdvanceResult


def test_node_test_runner_recognition(tmp_path: Path):
    """IntegrationGate._is_test_command must recognize modern Node.js test runners."""
    gate = IntegrationGate(tmp_path)
    # Test with string commands
    assert gate._is_test_command("node --test")
    assert gate._is_test_command("node --test tests/timing-engine.test.js")
    assert gate._is_test_command("node --test-name-pattern=timer tests/suite.js")
    assert gate._is_test_command("node tests/engine.test.js")
    assert gate._is_test_command("node tests/engine.spec.mjs")
    assert not gate._is_test_command("node src/main.js")
    assert not gate._is_test_command("node server.js")

    # Test with parsed token lists
    assert gate._is_test_command(["node", "--test", "tests/timing-engine.test.js"])
    assert gate._is_test_command(["node", "tests/timing-engine.test.js"])
    assert not gate._is_test_command(["node", "src/timing-engine.js"])


def test_validation_and_research_deliverable_exclusions(tmp_path: Path):
    """Work orders of type VALIDATION and RESEARCH should not be flagged for missing deliverables or contracts."""
    ws = tmp_path
    sync_dir = ws / ".sync"
    act_dir = sync_dir / "work-orders" / "ACTIVE"
    contracts_dir = sync_dir / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    (ws / "PLAN.md").write_text("# Plan\n\n# Acceptance Criteria\n- Criterion 1", encoding="utf-8")

    (act_dir / "WO-007.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-007",
            "type": "VALIDATION",
            "status": "COMPLETED",
            "title": "Integration Review",
            "acceptance_criteria": ["Verify integration"],
        }),
        encoding="utf-8",
    )
    (act_dir / "WO-008.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-008",
            "type": "RESEARCH",
            "status": "COMPLETED",
            "title": "Spike",
            "acceptance_criteria": ["Evaluate library"],
        }),
        encoding="utf-8",
    )

    report = evaluate_planning_completeness(ws)
    for check in report.checks:
        if check.name in ("meaningful_deliverables", "criteria_ownership"):
            assert "WO-007" not in str(check.evidence)
            assert "WO-008" not in str(check.evidence)
            assert check.passed

    release_report = evaluate_release_readiness(ws)
    for check in release_report.checks:
        if check.name == "governance_contracts":
            assert "WO-007" not in str(check.evidence)
            assert "WO-008" not in str(check.evidence)


def test_plan_criteria_fallback_to_work_orders(tmp_path: Path):
    """When PLAN.md has no explicit '# Acceptance Criteria' section, fall back to work orders' criteria."""
    ws = tmp_path
    sync_dir = ws / ".sync"
    act_dir = sync_dir / "work-orders" / "ACTIVE"
    act_dir.mkdir(parents=True, exist_ok=True)

    (ws / "PLAN.md").write_text("# Project Plan\n\n## Tasks\n- Task 1", encoding="utf-8")

    (act_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Task 1",
            "acceptance_criteria": ["Criteria from WO-001: millisecond precision", "Criteria from WO-001: dark theme"],
        }),
        encoding="utf-8",
    )

    criteria = plan_criteria(ws)
    assert "Criteria from WO-001: millisecond precision" in criteria
    assert "Criteria from WO-001: dark theme" in criteria


def test_gitops_bounded_retries_and_verdict_invalidation(tmp_path: Path):
    """When release readiness fails repeatedly in GITOPS, retries are bounded and verdict is invalidated."""
    ws = tmp_path
    sync_dir = ws / ".sync"
    reviews_dir = sync_dir / "reviews" / "architect"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    final_verdict = reviews_dir / "run-test-bounce.json"
    final_verdict.write_text('{"verdict": "PASS"}', encoding="utf-8")

    class FakeGitopsManager:
        def __init__(self):
            self.operations = {
                "op-gitops-1": {
                    "operation_id": "op-gitops-1",
                    "status": "COMPLETED",
                    "result": {"status": "completed"},
                }
            }
        def get_operation(self, op_id):
            return self.operations.get(op_id)

    manager = FakeGitopsManager()
    supervisor = LifecycleSupervisor(manager)
    state = RunState(
        run_id="run-test-bounce",
        session_id="test-session",
        workspace=str(ws),
        phase=Phase.GITOPS,
        product_goal="Test bounce breaker",
        gitops_retries=0,
        max_gitops_retries=2,
        gitops_operation_id="op-gitops-1",
    )

    result = supervisor._advance_gitops(state)
    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert state.gitops_retries == 1
    assert not final_verdict.is_file()

    final_verdict.write_text('{"verdict": "PASS"}', encoding="utf-8")
    state.phase = Phase.GITOPS
    state.gitops_operation_id = "op-gitops-1"
    result = supervisor._advance_gitops(state)
    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert state.gitops_retries == 2
    assert not final_verdict.is_file()

    state.phase = Phase.GITOPS
    state.gitops_operation_id = "op-gitops-1"
    result = supervisor._advance_gitops(state)
    assert result == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert "retries exhausted" in str(state.error)


def test_zero_turn_phase_oscillation_breaker(tmp_path: Path):
    """Supervisor advance loop must detect and halt zero-turn phase oscillations (>=12 transitions) at WAITING_FOR_HUMAN."""
    class OscillatingSupervisor(LifecycleSupervisor):
        def _transition(self, state, new_phase, authorized_override=False, **kwargs):
            state.phase = new_phase

        def _advance_dispatching(self, state):
            self._transition(state, Phase.FINAL_REVIEW)
            return AdvanceResult.TRANSITIONED

        def _advance_final_review(self, state):
            self._transition(state, Phase.DISPATCHING)
            return AdvanceResult.TRANSITIONED

    class FakeManager:
        pass

    sup = OscillatingSupervisor(FakeManager())
    state = RunState(
        run_id="run-test-oscillation",
        session_id="test-session",
        workspace=str(tmp_path),
        phase=Phase.DISPATCHING,
        product_goal="Test oscillation breaker",
        global_turn_budget=100,
    )

    for _ in range(11):
        res = sup.advance(state)
        assert res == AdvanceResult.TRANSITIONED

    res = sup.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert "PHASE_OSCILLATION_LOOP" in str(state.error)
