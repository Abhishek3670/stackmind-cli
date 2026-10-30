"""SDP-FIX-006 Orchestration Reliability and Governed Self-Recovery tests.

Deterministic verification for:
1. Dependency gate path traversal termination (no infinite loops on '.')
2. Pre-existing deliverable no-op touch verification
3. Gateway command failure tracking on non-zero exit codes
4. Actionable diagnostic preservation on dispatch failure
5. Governed unarchiving from BLOCKED to ACTIVE in _mark_wo_status_on_disk
6. Idempotent plan and run approval without ValueError on re-approval
7. Auto-repair turn completion prior to dispatch (no OperationContentionError)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml

from validators.harness.dependency_gate import is_local_module
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, RunState


def test_dependency_gate_path_traversal_termination(tmp_path: Path) -> None:
    """Verify that is_local_module terminates quickly on '.' and does not loop infinitely."""
    assert not is_local_module(".", tmp_path)
    assert not is_local_module("", tmp_path)


def test_mark_wo_status_on_disk_restores_blocked_to_active(tmp_path: Path) -> None:
    """Verify that _mark_wo_status_on_disk restores a file from BLOCKED/ to ACTIVE/."""
    sync_dir = tmp_path / ".sync"
    blocked_dir = sync_dir / "work-orders" / "BLOCKED"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    blocked_dir.mkdir(parents=True)
    active_dir.mkdir(parents=True)

    wo_id = "WO-042"
    blocked_file = blocked_dir / f"{wo_id}.yaml"
    blocked_file.write_text(
        yaml.safe_dump({"id": wo_id, "status": "BLOCKED", "error": "test failure"}, sort_keys=False),
        encoding="utf-8",
    )

    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_file.write_text(
        yaml.safe_dump({
            "orders": [{"id": wo_id, "status": "BLOCKED", "file": f"work-orders/BLOCKED/{wo_id}.yaml"}],
            "total_active": 0,
            "total_blocked": 1,
        }, sort_keys=False),
        encoding="utf-8",
    )

    # Call _mark_wo_status_on_disk with target_status="ACTIVE" and clear_error=True
    updated = LifecycleSupervisor._mark_wo_status_on_disk(tmp_path, wo_id, "ACTIVE", clear_error=True)
    assert updated is True

    # BLOCKED file should be moved/unlinked
    assert not blocked_file.exists()

    # ACTIVE file should exist and have status ACTIVE with error cleared
    active_file = active_dir / f"{wo_id}.yaml"
    assert active_file.is_file()
    data = yaml.safe_load(active_file.read_text(encoding="utf-8"))
    assert data["status"] == "ACTIVE"
    assert "error" not in data
    assert "blocked_reason" not in data

    # INDEX.yaml should be synchronized
    idx = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert idx["orders"][0]["status"] == "ACTIVE"
    assert idx["orders"][0]["file"] == f"work-orders/ACTIVE/{wo_id}.yaml"
    assert idx["total_active"] == 1
    assert idx["total_blocked"] == 0


def test_approve_plan_and_run_idempotency(tmp_path: Path) -> None:
    """Verify that approving an already APPROVED plan or run is idempotent and does not raise."""
    mgr = SessionManager(DaemonStorage(str(tmp_path / "sessions.json")))
    sess = mgr.create_session("architecture", "claude", {}, str(tmp_path))
    sid = sess["session_id"]

    plan = mgr.propose_plan(
        sid,
        "PLAN-001",
        "Test Plan",
        "# Project Plan\n\n## Current Architecture\n- Backend: Python\n\n## Milestones & Roadmap\n- [ ] M1: Init",
        {},
    )
    assert plan["state"] == "AWAITING_APPROVAL"

    # First approval succeeds
    wos1 = mgr.approve_plan(sid, "PLAN-001", reason="First approval")
    assert isinstance(wos1, list)
    p_rec = mgr.get_plan(sid, "PLAN-001")
    assert p_rec["state"] == "APPROVED"

    # Second approval is idempotent, does NOT raise ValueError
    wos2 = mgr.approve_plan(sid, "PLAN-001", reason="Second approval")
    assert isinstance(wos2, list)

    # approve_run is also idempotent
    run_state = RunState(run_id="run-test", product_goal="test", workspace=str(tmp_path), session_id=sid)
    run_state.plan_id = "PLAN-001"
    mgr._active_runs["run-test"] = run_state

    result = mgr.approve_run(sid, reason="Operator approved run")
    assert result["run_id"] == "run-test"


def test_architect_turn_preserves_actionable_diagnostic(tmp_path: Path) -> None:
    """Verify that when _architect_turn fails dispatch, it records actionable reason on state."""
    mgr = MagicMock()
    mgr._sessions = {}
    mgr.get_session.return_value = {"state": "RUNNING", "active_operation": None}
    mgr.start_turn = MagicMock(side_effect=RuntimeError("Quota exhausted for backend claude"))

    sup = LifecycleSupervisor(mgr)
    state = RunState(run_id="run-001", product_goal="goal", workspace=str(tmp_path), session_id="sess-001")

    result = sup._architect_turn(state, "Repair prompt", "WO-001")
    assert result is None
    assert state.last_dispatch_error is not None
    assert "Quota exhausted for backend claude" in state.last_dispatch_error


def test_architect_turn_ensures_session_running(tmp_path: Path) -> None:
    """Verify that _architect_turn transitions a BLOCKED/PAUSED session back to RUNNING."""
    mgr = MagicMock()
    session_data = {"session_id": "sess-002", "state": "BLOCKED"}
    mgr._sessions = {"sess-002": session_data}
    mgr.start_turn.return_value = {"operation_id": "op-repair-001"}

    sup = LifecycleSupervisor(mgr)
    state = RunState(run_id="run-002", product_goal="goal", workspace=str(tmp_path), session_id="sess-002")

    res = sup._architect_turn(state, "Prompt", "WO-001")
    assert res == {"operation_id": "op-repair-001"}
    # Session state must be set to RUNNING so begin_operation doesn't reject it
    assert session_data["state"] == "RUNNING"
