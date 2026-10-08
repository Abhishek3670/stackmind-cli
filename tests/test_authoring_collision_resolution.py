"""Tests for multi-layer Work Order anti-collision resolution on continuation goals.

Verifies:
1. Reconciling authored work orders remaps colliding active IDs (e.g. WO-001) to
   allocated non-colliding IDs (e.g. WO-008) across work orders, contracts, and decisions.
2. Dependencies between newly authored work orders are renumbered consistently.
3. Staged work order collision cleaner removes duplicate state directory files.
4. INDEX.yaml status sync protects completed entries from being flipped to ACTIVE.
5. compile_authoring_artifacts normalizes colliding active work orders.
6. Daemon manager authoring prompt injects allocated IDs dynamically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

import pytest

from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.runner import AgentRunner
from validators.kernel.daemon.manager import SessionManager


@dataclass
class DummyDecision:
    status: str = "completed"
    modified_files: tuple[str, ...] = ()
    commands: list[str] = None


def test_reconcile_authored_work_orders_remaps_colliding_ids(tmp_path: Path):
    project_dir = tmp_path / "project"
    workspace_root = tmp_path / "scratch"

    # Setup project with COMPLETED WO-001 through WO-007
    comp_dir = project_dir / ".sync" / "work-orders" / "COMPLETED"
    comp_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 8):
        (comp_dir / f"WO-{i:03d}.yaml").write_text(
            yaml.safe_dump({"id": f"WO-{i:03d}", "status": "COMPLETED"}), encoding="utf-8"
        )

    # Setup scratch workspace where model authored WO-001 and WO-002 (with dependency)
    ws_active = workspace_root / ".sync" / "work-orders" / "ACTIVE"
    ws_contracts = workspace_root / ".sync" / "contracts"
    ws_active.mkdir(parents=True, exist_ok=True)
    ws_contracts.mkdir(parents=True, exist_ok=True)

    wo1 = {"id": "WO-001", "title": "Audit", "dependencies": []}
    wo2 = {"id": "WO-002", "title": "Fix", "dependencies": ["WO-001"]}
    (ws_active / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")
    (ws_active / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

    c1 = {"work_order": "WO-001", "agent_id": "gemma"}
    c2 = {"work_order": "WO-002", "agent_id": "codex"}
    (ws_contracts / "WO-001.yaml").write_text(yaml.safe_dump(c1), encoding="utf-8")
    (ws_contracts / "WO-002.yaml").write_text(yaml.safe_dump(c2), encoding="utf-8")

    runner = AgentRunner.__new__(AgentRunner)
    runner.project_path = project_dir
    runner.agent = "claude"

    decision = DummyDecision(
        status="completed",
        modified_files=(
            ".sync/work-orders/ACTIVE/WO-001.yaml",
            ".sync/work-orders/ACTIVE/WO-002.yaml",
            ".sync/contracts/WO-001.yaml",
            ".sync/contracts/WO-002.yaml",
        ),
    )
    auto_added = [
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        ".sync/contracts/WO-001.yaml",
    ]

    new_decision, new_auto_added = runner._reconcile_authored_work_orders(
        workspace_root, decision, auto_added
    )

    # WO-001 and WO-002 should be remapped to WO-008 and WO-009
    assert not (ws_active / "WO-001.yaml").exists()
    assert not (ws_active / "WO-002.yaml").exists()
    assert (ws_active / "WO-008.yaml").exists()
    assert (ws_active / "WO-009.yaml").exists()

    wo8_data = yaml.safe_load((ws_active / "WO-008.yaml").read_text(encoding="utf-8"))
    wo9_data = yaml.safe_load((ws_active / "WO-009.yaml").read_text(encoding="utf-8"))
    assert wo8_data["id"] == "WO-008"
    assert wo9_data["id"] == "WO-009"
    assert wo9_data["dependencies"] == ["WO-008"]

    assert not (ws_contracts / "WO-001.yaml").exists()
    assert not (ws_contracts / "WO-002.yaml").exists()
    assert (ws_contracts / "WO-008.yaml").exists()
    assert (ws_contracts / "WO-009.yaml").exists()

    c8_data = yaml.safe_load((ws_contracts / "WO-008.yaml").read_text(encoding="utf-8"))
    assert c8_data["work_order"] == "WO-008"

    assert ".sync/work-orders/ACTIVE/WO-008.yaml" in new_decision.modified_files
    assert ".sync/work-orders/ACTIVE/WO-009.yaml" in new_decision.modified_files
    assert ".sync/contracts/WO-008.yaml" in new_decision.modified_files
    assert ".sync/contracts/WO-009.yaml" in new_decision.modified_files
    assert ".sync/work-orders/ACTIVE/WO-001.yaml" not in new_decision.modified_files
    assert ".sync/work-orders/ACTIVE/WO-008.yaml" in new_auto_added


def test_reconcile_authored_work_orders_uses_preallocated_active_ids(tmp_path: Path):
    project_dir = tmp_path / "project"
    workspace_root = tmp_path / "scratch"

    # Project has COMPLETED WO-001..WO-005, and pre-allocated ACTIVE WO-006..WO-007
    comp_dir = project_dir / ".sync" / "work-orders" / "COMPLETED"
    proj_act_dir = project_dir / ".sync" / "work-orders" / "ACTIVE"
    comp_dir.mkdir(parents=True, exist_ok=True)
    proj_act_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 6):
        (comp_dir / f"WO-{i:03d}.yaml").write_text(yaml.safe_dump({"id": f"WO-{i:03d}"}))
    for i in range(6, 8):
        (proj_act_dir / f"WO-{i:03d}.yaml").write_text(yaml.safe_dump({"id": f"WO-{i:03d}"}))

    # Model in scratch wrote WO-001 and WO-002
    ws_act = workspace_root / ".sync" / "work-orders" / "ACTIVE"
    ws_act.mkdir(parents=True, exist_ok=True)
    (ws_act / "WO-001.yaml").write_text(yaml.safe_dump({"id": "WO-001", "title": "Step 1"}))
    (ws_act / "WO-002.yaml").write_text(yaml.safe_dump({"id": "WO-002", "title": "Step 2"}))

    runner = AgentRunner.__new__(AgentRunner)
    runner.project_path = project_dir
    runner.agent = "claude"

    decision = DummyDecision(
        status="completed",
        modified_files=(".sync/work-orders/ACTIVE/WO-001.yaml", ".sync/work-orders/ACTIVE/WO-002.yaml"),
    )

    new_decision, _ = runner._reconcile_authored_work_orders(workspace_root, decision, [])
    assert (ws_act / "WO-006.yaml").exists()
    assert (ws_act / "WO-007.yaml").exists()
    assert not (ws_act / "WO-001.yaml").exists()
    assert ".sync/work-orders/ACTIVE/WO-006.yaml" in new_decision.modified_files
    assert ".sync/work-orders/ACTIVE/WO-007.yaml" in new_decision.modified_files


def test_reconcile_staged_work_order_collisions_cleans_duplicates(tmp_path: Path):
    staged_root = tmp_path / "staged"
    act_dir = staged_root / ".sync" / "work-orders" / "ACTIVE"
    comp_dir = staged_root / ".sync" / "work-orders" / "COMPLETED"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)

    (comp_dir / "WO-001.yaml").write_text("status: COMPLETED")
    (act_dir / "WO-001.yaml").write_text("status: ACTIVE")
    (act_dir / "WO-008.yaml").write_text("status: ACTIVE")

    AgentRunner._reconcile_staged_work_order_collisions(staged_root)

    # Colliding active file is pruned, non-colliding is preserved
    assert not (act_dir / "WO-001.yaml").exists()
    assert (comp_dir / "WO-001.yaml").exists()
    assert (act_dir / "WO-008.yaml").exists()


def test_sync_index_preserves_completed_status(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    act_dir = sync_dir / "work-orders" / "ACTIVE"
    comp_dir = sync_dir / "work-orders" / "COMPLETED"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)
    index_file = sync_dir / "work-orders" / "INDEX.yaml"

    initial_index = {
        "orders": [
            {"id": "WO-001", "status": "COMPLETED", "title": "Done"},
            {"id": "WO-002", "status": "COMPLETED", "title": "Done 2"},
        ]
    }
    index_file.write_text(yaml.safe_dump(initial_index))

    # WO-001 is completed in COMPLETED/ -> must preserve COMPLETED
    (comp_dir / "WO-001.yaml").write_text(yaml.safe_dump({"id": "WO-001", "status": "COMPLETED"}))
    # WO-002 is active in ACTIVE/ (e.g. reopened for rework) -> must update to ACTIVE
    (act_dir / "WO-002.yaml").write_text(yaml.safe_dump({"id": "WO-002", "status": "ACTIVE"}))

    AgentRunner._sync_index_and_tree_yaml(tmp_path)

    updated = yaml.safe_load(index_file.read_text())
    wo1 = next(o for o in updated["orders"] if o["id"] == "WO-001")
    wo2 = next(o for o in updated["orders"] if o["id"] == "WO-002")
    assert wo1["status"] == "COMPLETED"
    assert wo2["status"] == "ACTIVE"


def test_compile_authoring_artifacts_anti_collision(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    act_dir = sync_dir / "work-orders" / "ACTIVE"
    comp_dir = sync_dir / "work-orders" / "COMPLETED"
    contracts_dir = sync_dir / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # COMPLETED has WO-001 through WO-004
    for i in range(1, 5):
        (comp_dir / f"WO-{i:03d}.yaml").write_text(yaml.safe_dump({"id": f"WO-{i:03d}"}))

    # ACTIVE mistakenly has WO-001
    (act_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({"id": "WO-001", "title": "New feature", "assigned_agents": ["codex"]})
    )
    (contracts_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({"work_order": "WO-001", "agent_id": "codex"})
    )

    res = compile_authoring_artifacts(tmp_path)

    # WO-001 should be remapped to WO-005
    assert not (act_dir / "WO-001.yaml").exists()
    assert (act_dir / "WO-005.yaml").exists()
    assert (contracts_dir / "WO-005.yaml").exists()
    wo5_data = yaml.safe_load((act_dir / "WO-005.yaml").read_text())
    assert wo5_data["id"] == "WO-005"


def test_synthesize_child_work_orders_preserves_explicit_agent(tmp_path: Path):
    from validators.kernel.daemon.authoring import synthesize_child_work_orders
    (tmp_path / ".sync").mkdir(parents=True, exist_ok=True)
    plan_content = """# Project Plan: Code Validation

## Current Architecture
Baseline environment.

## Milestones & Roadmap
- [ ] Milestone 1: Codebase Audit (Agent: gemma)
  - [ ] Task 1.1: Audit files.
- [ ] Milestone 2: Gap Analysis & Correction (Agent: codex)
  - [ ] Task 2.1: Fix missing elements identified in the audit.
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    wos = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(wos) == 2
    # Milestone 2 mentions 'audit' in task text, but Agent is codex -> must remain codex
    assert wos[1]["assigned_agents"] == ["codex"]
    act_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / f"{wos[1]['id']}.yaml"
    data = yaml.safe_load(act_file.read_text(encoding="utf-8"))
    assert data["assigned_agents"] == ["codex"]


def test_reconcile_active_work_orders_with_plan_fixes_agent_mismatch(tmp_path: Path):
    from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan
    plan_content = """# Plan

## Current Architecture
Baseline environment.

## Milestones & Roadmap
- [ ] Milestone 1: Gap Analysis & Correction (Agent: codex)
  - [ ] Task 1.1: Fix issues in the audit.
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Active WO mistakenly assigned to gemma with src/backend.py deliverable
    wo_data = {
        "id": "WO-009",
        "title": "Gap Analysis & Correction",
        "assigned_agents": ["gemma"],
        "deliverable": {"type": "code", "path": "src/backend.py"},
    }
    (act_dir / "WO-009.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")
    contract_data = {
        "agent_id": "gemma",
        "work_order": "WO-009",
        "identity": {"role": "qa"},
    }
    (contracts_dir / "WO-009.yaml").write_text(yaml.safe_dump(contract_data), encoding="utf-8")

    reconciled = reconcile_active_work_orders_with_plan(tmp_path)
    assert "WO-009" in reconciled

    new_wo = yaml.safe_load((act_dir / "WO-009.yaml").read_text(encoding="utf-8"))
    assert new_wo["assigned_agents"] == ["codex"]
    new_contract = yaml.safe_load((contracts_dir / "WO-009.yaml").read_text(encoding="utf-8"))
    assert new_contract["agent_id"] == "codex"
    assert new_contract["identity"]["role"] == "backend"


def test_plan_rejection_allocates_next_available_work_order_without_collision(tmp_path: Path):
    """When a plan is rejected, re-planning allocates the next unused work order ID (e.g. WO-008)
    rather than colliding with the completed planning work order (e.g. WO-007)."""
    from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, AdvanceResult
    from validators.kernel.daemon.storage import DaemonStorage
    from cli.validate import validate

    # 1. Setup workspace with completed work orders WO-000 through WO-006
    comp_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    comp_dir.mkdir(parents=True, exist_ok=True)
    for i in range(0, 7):
        (comp_dir / f"WO-{i:03d}.yaml").write_text(
            yaml.safe_dump({"id": f"WO-{i:03d}", "status": "COMPLETED"}), encoding="utf-8"
        )

    storage = DaemonStorage(tmp_path / ".daemon_storage")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    supervisor = LifecycleSupervisor(manager)
    state = supervisor.start_run("run-reject-test", "Design auth system", tmp_path, sid)
    manager._active_runs["run-reject-test"] = state

    # 2. First advance -> dispatches planning turn and synthesizes WO-007
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.planning_wo_id == "WO-007"
    assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-007.yaml").is_file()

    # 3. Simulate turn completion & propose plan
    manager.propose_plan(sid, "plan-1", "Auth System Plan", metadata={"operation_id": state.planning_operation_id})
    manager.complete_operation(operation_id=state.planning_operation_id, status="COMPLETED", result={"summary": "Plan ready"})

    # Advance supervisor -> moves to AWAITING_APPROVAL and archives WO-007
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL
    assert "WO-007" in state.completed_wo_ids
    assert (comp_dir / "WO-007.yaml").is_file()
    assert not (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-007.yaml").exists()

    # 4. Reject the plan with feedback
    manager.reject_plan(sid, "plan-1", reason="Need rate limiting added")

    # Advance supervisor -> transitions to PLANNING
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PLANNING
    assert state.planning_wo_id is None
    assert state.planning_operation_id is None

    # 5. Advance again -> dispatches re-planning turn under WO-008 (next available ID)
    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.planning_wo_id == "WO-008"
    assert (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-008.yaml").is_file()

    # Invariants: WO-007 remains solely in COMPLETED, WO-008 solely in ACTIVE
    assert (comp_dir / "WO-007.yaml").is_file()
    assert not (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-007.yaml").exists()
    assert not (comp_dir / "WO-008.yaml").exists()

    # Validate that runtime passes with zero multi-directory issues
    val_res = validate(tmp_path)
    state_dir_errors = [e.message for e in val_res.errors if "exists in multiple state directories" in e.message]
    assert state_dir_errors == []


def test_prepare_run_for_resume_cleans_up_dual_state_collision(tmp_path: Path):
    """_prepare_run_for_resume heals dual-state active/completed work order collisions on disk."""
    from validators.kernel.daemon.supervisor import Phase
    from validators.kernel.daemon.storage import DaemonStorage

    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    comp_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    act_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)

    # Collision on disk: WO-007 exists in BOTH ACTIVE and COMPLETED
    (comp_dir / "WO-007.yaml").write_text("id: WO-007\nstatus: COMPLETED\n", encoding="utf-8")
    (act_dir / "WO-007.yaml").write_text("id: WO-007\nstatus: ACTIVE\n", encoding="utf-8")

    storage = DaemonStorage(tmp_path / ".daemon_storage")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    run_state = manager.supervisor.start_run("run-heal", "Goal", tmp_path, sid)
    run_state.phase = Phase.FAILED
    run_state.planning_wo_id = "WO-007"
    run_state.completed_wo_ids = ["WO-007"]
    manager._active_runs["run-heal"] = run_state

    # Resume run -> calls _prepare_run_for_resume
    manager._prepare_run_for_resume(run_state, session, tmp_path)

    # The stale active duplicate was removed
    assert not (act_dir / "WO-007.yaml").exists()
    assert (comp_dir / "WO-007.yaml").is_file()
    # planning_wo_id was cleared so next advance will allocate fresh ID
    assert run_state.planning_wo_id is None
