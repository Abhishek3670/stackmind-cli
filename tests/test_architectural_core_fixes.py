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
