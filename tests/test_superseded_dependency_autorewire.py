"""Tests for autonomous superseded dependency resolution and recovery decision isolation.

Verifies:
1. _successor_for detects replacement work orders from replacement fields, log rebase hints,
   and matching completed deliverable paths.
2. _handle_invalid_dependency rewires superseded dependencies on active work orders automatically.
3. Leftover recovery decision files are archived on dispatch to prevent stale decision loops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.daemon.supervisor.dependencies import (
    DependencyStatus,
    _successor_for,
    load_all_work_orders,
)
from validators.kernel.daemon.supervisor.release import completion_record_for


def _write_wo(root: Path, wo_id: str, data: dict[str, Any], bucket: str = "ACTIVE") -> Path:
    folder = root / ".sync" / "work-orders" / bucket
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / f"{wo_id}.yaml"
    file_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return file_path


def test_successor_for_log_fallback(tmp_path: Path) -> None:
    # WO-001 is completed
    _write_wo(
        tmp_path,
        "WO-001",
        {
            "id": "WO-001",
            "revision": 1,
            "dependencies": [],
            "status": "COMPLETED",
            "deliverable": {"path": "package.json"},
        },
        bucket="COMPLETED",
    )
    # WO-013 is cancelled with log hint pointing to WO-001
    _write_wo(
        tmp_path,
        "WO-013",
        {
            "id": "WO-013",
            "status": "CANCELLED",
            "dependencies": [],
            "deliverable": {"path": "package.json"},
            "log": ["Rebased onto the completed scaffolding WO-001."],
        },
        bucket="ACTIVE",
    )
    records = load_all_work_orders(tmp_path)
    successor = _successor_for(records, "WO-013")
    assert successor == "WO-001"


def test_successor_for_deliverable_path_matching(tmp_path: Path) -> None:
    # WO-001 is completed with package.json deliverable
    _write_wo(
        tmp_path,
        "WO-001",
        {
            "id": "WO-001",
            "revision": 1,
            "dependencies": [],
            "status": "COMPLETED",
            "deliverable": {"path": "package.json"},
        },
        bucket="COMPLETED",
    )
    # WO-013 is cancelled with identical deliverable, without explicit log pointer
    _write_wo(
        tmp_path,
        "WO-013",
        {
            "id": "WO-013",
            "status": "CANCELLED",
            "dependencies": [],
            "deliverable": {"path": "package.json"},
        },
        bucket="ACTIVE",
    )
    records = load_all_work_orders(tmp_path)
    successor = _successor_for(records, "WO-013")
    assert successor == "WO-001"


def test_auto_rewires_superseded_dependency_on_dispatch(tmp_path: Path) -> None:
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-rewire", "Test rewiring", tmp_path, "sess-rewire")
    state.phase = Phase.EXECUTING

    # Completed predecessor with valid completion record and file on disk
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    wo_001_data = {
        "id": "WO-001",
        "revision": 1,
        "dependencies": [],
        "status": "COMPLETED",
        "deliverable": {"path": "package.json"},
    }
    wo_001_data["completion"] = completion_record_for(tmp_path, "WO-001", wo_001_data, "2026-11-06T00:00:00Z")
    _write_wo(tmp_path, "WO-001", wo_001_data, bucket="COMPLETED")

    # Cancelled duplicate
    _write_wo(
        tmp_path,
        "WO-013",
        {
            "id": "WO-013",
            "status": "CANCELLED",
            "dependencies": [],
            "deliverable": {"path": "package.json"},
        },
        bucket="ACTIVE",
    )

    # Active work order depending on cancelled duplicate WO-013
    wo_008_path = _write_wo(
        tmp_path,
        "WO-008",
        {
            "id": "WO-008",
            "status": "ACTIVE",
            "dependencies": ["WO-013"],
            "deliverable": {"path": "src/engine.js"},
        },
        bucket="ACTIVE",
    )
    # Ensure contract exists for WO-008
    contract_dir = tmp_path / ".sync" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    (contract_dir / "WO-008.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "codex",
            "work_order": "WO-008",
            "identity": {"role": "backend", "reports_to": "lead"},
            "scope": {"allow": [{"module": "src/engine.js"}], "deny": []},
            "budget": {"max_files_touched": 5, "max_turns": 3},
        }),
        encoding="utf-8",
    )

    state.worker_wo_ids = ["WO-008"]
    state.completed_wo_ids = ["WO-001"]

    # First advance will hit INVALID dependency on WO-013, auto-rewire to WO-001,
    # and re-evaluate dependencies!
    dep_res = supervisor._dependency_result(state, "WO-008", tmp_path)
    assert dep_res.status == DependencyStatus.INVALID
    res = supervisor._handle_invalid_dependency(
        state,
        tmp_path,
        "WO-008",
        dep_res,
    )
    assert res == AdvanceResult.TRANSITIONED

    # Verify that WO-008 was rewired on disk to depend on WO-001
    updated_data = yaml.safe_load(wo_008_path.read_text(encoding="utf-8"))
    assert updated_data["dependencies"] == ["WO-001"]

    # And evaluating dependencies now reports SATISFIED (WO-001 is completed)
    eval_res = supervisor._dependency_result(state, "WO-008", tmp_path)
    assert eval_res.status == DependencyStatus.SATISFIED


def test_stale_recovery_decision_archived_on_dispatch(tmp_path: Path) -> None:
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-dec", "Test decision isolation", tmp_path, "sess-dec")

    dec_dir = tmp_path / ".sync" / "decisions" / "recovery"
    dec_dir.mkdir(parents=True, exist_ok=True)
    stale_dec_path = dec_dir / "WO-008.decision.json"
    stale_dec_path.write_text(
        json.dumps({
            "work_order": "WO-008",
            "action": "architect_fix",
            "failure_code": "OLD_FAILURE",
        }),
        encoding="utf-8",
    )

    state.worker_blockers["WO-008"] = {"failure_code": "INVALID_DEPENDENCY"}
    supervisor._dispatch_recovery_decision(state, tmp_path, "WO-008")

    # Stale decision file must have been archived away from the active decisions folder
    assert not stale_dec_path.exists()
