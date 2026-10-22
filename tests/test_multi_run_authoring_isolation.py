from pathlib import Path
import yaml
import pytest

from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.authoring_readiness import validate_authoring_readiness, load_plan_milestones
from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState, Phase, archive_blocked_work_orders


def _setup_multi_run_workspace(root: Path) -> Path:
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sync_dir = ws / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    blocked_dir = sync_dir / "work-orders" / "BLOCKED"
    completed_dir = sync_dir / "work-orders" / "COMPLETED"
    contracts_dir = sync_dir / "contracts"
    runtime_dir = sync_dir / "runtime"

    for d in (active_dir, blocked_dir, completed_dir, contracts_dir, runtime_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Completed planning WO from run 1
    (completed_dir / "WO-000.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-000",
            "title": "Initial Architecture Plan",
            "status": "COMPLETED",
            "type": "RESEARCH",
            "assigned_agents": ["claude"],
        }),
        encoding="utf-8",
    )

    # Lingering blocked work orders from run 1 in ACTIVE
    (active_dir / "WO-006.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-006",
            "title": "Release & GitOps",
            "status": "BLOCKED",
            "milestone_id": "Milestone 6",
            "type": "FEATURE",
            "assigned_agents": ["local-llm"],
            "dependencies": ["WO-005"],
            "deliverable": {"type": "doc", "path": "CHANGELOG.md"},
        }),
        encoding="utf-8",
    )
    (contracts_dir / "WO-006.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "work_order": "WO-006",
            "agent_id": "local-llm",
            "budget": {"max_files_touched": 3},
            "allow": [{"module": "workspace/write", "path": "CHANGELOG.md"}],
        }),
        encoding="utf-8",
    )

    (active_dir / "WO-007.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-007",
            "title": "Integration Review",
            "status": "BLOCKED",
            "type": "VALIDATION",
            "assigned_agents": ["claude"],
            "dependencies": ["WO-006"],
            "deliverable": {"type": "doc"},
        }),
        encoding="utf-8",
    )

    # New PLAN.md for Run 2 with only 2 milestones
    (ws / "PLAN.md").write_text(
        """# Project Plan

## Milestone 1: Core Setup
- **Agent**: codex
- **Tasks**: Setup basic files

## Milestone 2: Build Pipeline
- **Agent**: gemma
- **Tasks**: Setup and run build tests
""",
        encoding="utf-8",
    )

    # Authored work orders for Run 2 (Planning WO was WO-008)
    (completed_dir / "WO-008.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-008",
            "title": "Planning Turn 2",
            "status": "COMPLETED",
            "type": "RESEARCH",
            "assigned_agents": ["claude"],
        }),
        encoding="utf-8",
    )

    (active_dir / "WO-009.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-009",
            "title": "Core Setup",
            "status": "ACTIVE",
            "priority": "P1",
            "milestone_id": "Milestone 1",
            "type": "FEATURE",
            "assigned_agents": ["codex"],
            "dependencies": ["WO-008"],
            "deliverable": {"type": "code", "path": "src/main.py", "description": "Core setup main file"},
        }),
        encoding="utf-8",
    )
    (contracts_dir / "WO-009.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "work_order": "WO-009",
            "agent_id": "codex",
            "identity": {"role": "backend", "reports_to": "claude"},
            "budget": {"max_files_touched": 3, "max_tokens": 20000},
            "scope": {
                "allow": [{"module": "src/**"}],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
        }),
        encoding="utf-8",
    )

    (active_dir / "WO-010.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-010",
            "title": "Build Pipeline",
            "status": "ACTIVE",
            "priority": "P1",
            "milestone_id": "Milestone 2",
            "type": "VALIDATION",
            "assigned_agents": ["gemma"],
            "dependencies": ["WO-009"],
            "deliverable": {"type": "code", "path": "tests/test_build.py", "description": "Build tests"},
            "test_plan": [
                {"tests": ["tests/test_build.py"], "source": "src/main.py"}
            ],
        }),
        encoding="utf-8",
    )
    (contracts_dir / "WO-010.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "work_order": "WO-010",
            "agent_id": "gemma",
            "identity": {"role": "qa", "reports_to": "claude"},
            "budget": {"max_files_touched": 3, "max_tokens": 20000},
            "scope": {
                "allow": [
                    {"module": "tests/**"},
                    {"module": "workspace/command"},
                    {"module": ".sync/inbox/claude/**"},
                ],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
        }),
        encoding="utf-8",
    )

    return ws


def test_compiler_ignores_blocked_and_prior_run_work_orders(tmp_path: Path):
    ws = _setup_multi_run_workspace(tmp_path)
    milestones = load_plan_milestones(ws)

    # Calling compile_authoring_artifacts with planning_wo_id='WO-008'
    # must ignore WO-006 (which declares Milestone 6, unknown in PLAN.md)
    res = compile_authoring_artifacts(ws, milestones, planning_wo_id="WO-008")
    assert not res.decision_required, f"Unexpected decisions required: {res.decision_required}"


def test_readiness_gate_ignores_blocked_and_prior_run_work_orders(tmp_path: Path):
    ws = _setup_multi_run_workspace(tmp_path)
    milestones = load_plan_milestones(ws)

    # Validate readiness with planning_wo_id='WO-008'
    res = validate_authoring_readiness(
        ws,
        plan_id="PLAN-002",
        expected_milestones=milestones,
        planning_wo_id="WO-008",
        completed_wo_ids=["WO-000", "WO-008"],
    )
    assert res.ready is True, f"Readiness failed with issues: {[i.to_dict() for i in res.issues]}"


def test_supervisor_worker_discovery_ignores_blocked_prior_wos(tmp_path: Path):
    ws = _setup_multi_run_workspace(tmp_path)
    supervisor = LifecycleSupervisor(None)

    state = RunState(
        run_id="run-multi-2",
        product_goal="Run second goal",
        workspace=str(ws),
        session_id="sess-2",
        planning_wo_id="WO-008",
    )

    supervisor._discover_worker_wos(state)
    # Must only discover WO-009 and WO-010, NOT WO-006 or WO-007
    assert "WO-006" not in state.worker_wo_ids
    assert "WO-007" not in state.worker_wo_ids
    assert "WO-009" in state.worker_wo_ids
    assert "WO-010" in state.worker_wo_ids


def test_archive_blocked_work_orders_moves_files(tmp_path: Path):
    ws = _setup_multi_run_workspace(tmp_path)
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    blocked_dir = ws / ".sync" / "work-orders" / "BLOCKED"

    # Prior to archiving, WO-006 and WO-007 are in ACTIVE
    assert (active_dir / "WO-006.yaml").is_file()
    assert (active_dir / "WO-007.yaml").is_file()

    archived = archive_blocked_work_orders(ws)
    assert "WO-006" in archived
    assert "WO-007" in archived

    # After archiving, they must be unlinked from ACTIVE and present in BLOCKED
    assert not (active_dir / "WO-006.yaml").exists()
    assert not (active_dir / "WO-007.yaml").exists()
    assert (blocked_dir / "WO-006.yaml").is_file()
    assert (blocked_dir / "WO-007.yaml").is_file()

    # Active work orders for Run 2 must remain in ACTIVE
    assert (active_dir / "WO-009.yaml").is_file()
    assert (active_dir / "WO-010.yaml").is_file()
