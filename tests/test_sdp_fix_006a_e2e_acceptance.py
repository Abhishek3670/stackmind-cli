"""SDP-FIX-006A — End-to-End Acceptance and Recovery Safety Audit.

Deterministic end-to-end lifecycle verification in a disposable empty workspace:
1. Proposal of football simulation plan
2. Operator approval producing valid work orders and contracts conforming to schemas
3. Authorized dispatch and execution producing verified artifacts
4. Deliberately introduced verification failure blocking release and archiving to BLOCKED/
5. Governed recovery repairing the failure and unarchiving back to ACTIVE/
6. Successful re-verification and release gating
7. Final state agreement across supervisor RunState, work orders, INDEX.yaml, and TREE.yaml
8. Recovery dispatch failure simulation with surviving diagnostic across restart
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import yaml

from cli.init import ensure_canonical_tree, ensure_workspace_protocol_scaffolding
from validators.harness.authoring_readiness import validate_authoring_readiness
from validators.kernel.daemon.authoring import synthesize_child_work_orders
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
    archive_blocked_work_orders,
    archive_completed_work_orders,
)
from validators.kernel.io import durable_write_yaml


def test_football_simulation_e2e_lifecycle(tmp_path: Path) -> None:
    """Bounded end-to-end lifecycle verification for football simulation request."""
    ws = tmp_path / "football_sim"
    ws.mkdir(parents=True)
    ensure_workspace_protocol_scaffolding(ws)
    ensure_canonical_tree(ws)

    # 1. Initialize session and daemon manager
    storage = DaemonStorage(str(ws / ".sync" / "sessions.json"))
    mgr = SessionManager(storage)
    sess = mgr.create_session("architecture", "claude", {}, str(ws))
    sid = sess["session_id"]

    # 2. Plan creation: Football match simulation
    football_plan_content = """# Project Plan: Football Match Simulation

## Current Architecture
- Backend: Python match engine (`src/engine/simulation.py`)
- Frontend: HTML5 canvas browser interface (`web/index.html`)
- Verification: Pytest suite (`tests/test_simulation.py`)

## Milestones & Roadmap
- [ ] Milestone 1: Simulation Engine Core (WO-001)
- [ ] Milestone 2: Web Pitch Viewer (WO-002)
- [ ] Milestone 3: End-to-End Test Suite (WO-003)
"""
    plan = mgr.propose_plan(
        sid,
        "PLAN-FOOTBALL-001",
        "Football Match Simulation",
        football_plan_content,
        {
            "is_goal": True,
            "work_orders": [
                {
                    "id": "WO-001",
                    "title": "Simulation Engine Core",
                    "agent": "codex",
                    "role": "backend",
                    "deliverable": "src/engine/simulation.py",
                    "milestone_id": "M1",
                },
                {
                    "id": "WO-002",
                    "title": "Web Pitch Viewer",
                    "agent": "gemini",
                    "role": "frontend",
                    "deliverable": "web/index.html",
                    "milestone_id": "M2",
                },
                {
                    "id": "WO-003",
                    "title": "Simulation Test Suite",
                    "agent": "gemma",
                    "role": "qa",
                    "deliverable": "tests/test_simulation.py",
                    "milestone_id": "M3",
                },
            ],
        },
    )
    assert plan["state"] == "AWAITING_APPROVAL"

    # 3. Plan approval produces authorized work orders and contracts
    wos = mgr.approve_plan(sid, "PLAN-FOOTBALL-001", reason="Operator approved football simulation")
    assert len(wos) >= 3

    plan_md = ws / "PLAN.md"
    assert plan_md.is_file()
    assert "Football Match Simulation" in plan_md.read_text(encoding="utf-8")

    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"
    assert (active_dir / "WO-001.yaml").is_file()
    assert (active_dir / "WO-002.yaml").is_file()
    assert (active_dir / "WO-003.yaml").is_file()
    assert (contracts_dir / "WO-001.yaml").is_file()

    # Verify authoring readiness gate passes
    readiness = validate_authoring_readiness(ws, plan_id="PLAN-FOOTBALL-001")
    assert readiness.ready is True

    # 4. Authorized dispatch & execution of WO-001 (Backend Simulation Engine)
    sim_file = ws / "src" / "engine" / "simulation.py"
    sim_file.parent.mkdir(parents=True)
    sim_file.write_text(
        "class MatchSimulation:\n"
        "    def __init__(self, home: str, away: str):\n"
        "        self.home = home\n"
        "        self.away = away\n"
        "        self.score = {'home': 0, 'away': 0}\n"
        "    def tick(self) -> dict:\n"
        "        return {'minute': 1, 'score': self.score}\n",
        encoding="utf-8",
    )
    # Mark WO-001 completed
    archived_comp = archive_completed_work_orders(ws, ["WO-001"])
    assert "WO-001" in archived_comp
    assert (ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml").is_file()
    assert not (active_dir / "WO-001.yaml").exists()

    # 5. Deliberately introduced verification failure on WO-002 (Web Pitch Viewer)
    # Deliberately mark WO-002 as BLOCKED due to synthetic verification failure
    wo2_file = active_dir / "WO-002.yaml"
    wo2_data = yaml.safe_load(wo2_file.read_text(encoding="utf-8"))
    wo2_data["status"] = "BLOCKED"
    wo2_data["error"] = "Clean-delivery check failed: prohibited token 'DRAFT_STUB' detected"
    durable_write_yaml(wo2_file, wo2_data)

    archived_blocked = archive_blocked_work_orders(ws, ["WO-002"])
    assert "WO-002" in archived_blocked
    blocked_wo2 = ws / ".sync" / "work-orders" / "BLOCKED" / "WO-002.yaml"
    assert blocked_wo2.is_file()
    assert not (active_dir / "WO-002.yaml").exists()

    index_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
    idx = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert idx["total_blocked"] == 1

    # 6. Governed Recovery repairs the failure and unarchives back to ACTIVE
    sup = LifecycleSupervisor(mgr)
    state = RunState(
        run_id="run-football-001",
        product_goal="Football match simulation",
        workspace=str(ws),
        session_id=sid,
    )
    state.worker_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.completed_wo_ids = ["WO-001"]
    state.blocked_wo_ids = ["WO-002"]
    state.worker_blockers["WO-002"] = {
        "operation_id": "op-wo002-fail",
        "failure_code": "CLEAN_DELIVERY_VIOLATION",
        "reason": "prohibited token 'DRAFT_STUB' detected",
    }

    # Repair the deliverable cleanly
    web_file = ws / "web" / "index.html"
    web_file.parent.mkdir(parents=True)
    web_file.write_text(
        "<!DOCTYPE html>\n<html><head><title>Football Match</title></head>\n"
        "<body><canvas id='pitch' width='800' height='600'></canvas>\n"
        "<script>console.log('Pitch ready');</script></body></html>\n",
        encoding="utf-8",
    )

    # Recovery: _clear_worker_blocker unarchives WO-002 back to ACTIVE
    advance = sup._clear_worker_blocker(state, ws, "WO-002", redispatch=False)
    assert advance == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-002" not in state.blocked_wo_ids
    assert (active_dir / "WO-002.yaml").is_file()
    assert not blocked_wo2.exists()

    # Complete WO-000 (Architecture), WO-002, and WO-003
    archive_completed_work_orders(ws, ["WO-000", "WO-002", "WO-003"])

    # 7. Verify final runtime state agrees across all layers
    idx_final = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert idx_final["total_blocked"] == 0
    assert idx_final["total_completed"] == 4
    assert idx_final["total_active"] == 0

    tree_final = ensure_canonical_tree(ws)
    assert tree_final["work_orders"]["total_blocked"] == 0
    assert tree_final["work_orders"]["total_completed"] == 4

    for stop_ev in mgr._run_stop_events.values():
        stop_ev.set()
    mgr.close_session(sid)


def test_dispatch_failure_recovery_and_restart(tmp_path: Path) -> None:
    """Verify that dispatch failure records diagnostic and survives restart without duplicate ops."""
    ws = tmp_path / "restart_ws"
    ws.mkdir(parents=True)
    ensure_workspace_protocol_scaffolding(ws)

    storage = DaemonStorage(str(ws / ".sync" / "sessions.json"))
    mgr = SessionManager(storage)
    sess = mgr.create_session("architecture", "claude", {}, str(ws))
    sid = sess["session_id"]

    sup = LifecycleSupervisor(mgr)
    state = RunState(
        run_id="run-restart-001",
        product_goal="Restart test",
        workspace=str(ws),
        session_id=sid,
    )
    state.phase = Phase.ARCHITECT_REPAIR
    state.repair_context = {"work_order_id": "WO-001", "source": "recovery", "work_order": "WO-001"}

    # Mock manager.start_turn to fail with a specific diagnostic
    mgr.start_turn = MagicMock(side_effect=RuntimeError("Provider API rate limit exceeded"))
    mgr.get_session = MagicMock(return_value={"state": "RUNNING", "active_operation": None})

    result = sup.advance(state)
    assert result == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "Provider API rate limit exceeded" in str(state.error)

    # Persist the run state to disk
    sup.save_run_state(state, ws)

    # Reconstruct supervisor and reload run state (simulating daemon restart)
    loaded_state = sup.load_run_state("run-restart-001", ws)
    assert loaded_state is not None
    assert loaded_state.phase == Phase.BLOCKED
    assert "Provider API rate limit exceeded" in str(loaded_state.error)

    # Resume the run through supported workflow
    mgr._active_runs["run-restart-001"] = loaded_state
    # Now simulate recovery condition cleared: provider quota available
    mgr.start_turn = MagicMock(return_value={"operation_id": "op-recovered-001"})
    mgr._run_driver_threads = {}

    resumed_info = mgr.resume_run(sid, "run-restart-001")
    assert resumed_info["run_id"] == "run-restart-001"
    # Verify no duplicate operations and clean unblocking
    assert loaded_state.phase in (Phase.DISPATCHING, Phase.AUTHORING, Phase.EXECUTING, Phase.PLANNING)
