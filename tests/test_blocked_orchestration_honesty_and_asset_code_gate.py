"""Regression tests for blocked orchestration honesty, asset deliverable classification,
and authoring repair unblocking.
"""
from pathlib import Path
import yaml
import pytest

from validators.harness.authoring_readiness import _is_code_deliverable, validate_authoring_readiness
from validators.harness.task_contract import render_team_status
from validators.harness.authoring_gate import AuthoringGate
from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan


def test_is_code_deliverable_excludes_assets_and_directories():
    """Static assets, SVGs, images, styling, and directories are not code deliverables."""
    assert _is_code_deliverable("code", "assets/img/") is False
    assert _is_code_deliverable("code", "assets/img") is False
    assert _is_code_deliverable("code", "assets/img/accents.svg") is False
    assert _is_code_deliverable("code", "assets/img/app_icon.png") is False
    assert _is_code_deliverable("code", "assets/css/styles.css") is False
    assert _is_code_deliverable("doc", "README.md") is False

    # True code deliverables
    assert _is_code_deliverable("code", "index.html") is True
    assert _is_code_deliverable("code", "src/backend.py") is True
    assert _is_code_deliverable("code", "assets/js/main.js") is True
    assert _is_code_deliverable("feature", "app.py") is True


def test_render_team_status_halts_specialists_and_instructs_honesty_when_blocked(tmp_path: Path):
    """When supervisor run is BLOCKED, render_team_status must report specialists as HALTED

    and instruct the Architect to be honest rather than hallucinating that 'orchestration is working'.
    """
    sync_dir = tmp_path / ".sync"
    wo_dir = sync_dir / "work-orders" / "ACTIVE"
    sup_dir = sync_dir / "runtime" / "supervisor"
    wo_dir.mkdir(parents=True, exist_ok=True)
    sup_dir.mkdir(parents=True, exist_ok=True)

    # WO-001 active
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Build Frontend",
            "status": "ACTIVE",
            "assigned_agents": ["gemini"],
            "deliverable": {"type": "code", "path": "index.html"},
        }),
        encoding="utf-8",
    )

    # Supervisor run in BLOCKED phase
    (sup_dir / "run-test.yaml").write_text(
        yaml.safe_dump({
            "run_id": "run-test",
            "phase": "BLOCKED",
            "error": "Authoring readiness gate still failing after repair",
            "readiness_issues": [{"code": "TEST_COVERAGE_UNPLANNED", "message": "No test artifact is planned"}],
        }),
        encoding="utf-8",
    )

    status_text = render_team_status(tmp_path)

    # Must show supervisor BLOCKED
    assert "Phase: BLOCKED" in status_text
    assert "Supervisor [run-test] (BLOCKED)" in status_text

    # Must NOT report Frontend as ACTIVE executing
    assert "Frontend (gemini): ACTIVE executing" not in status_text
    assert "Frontend (gemini): HALTED / WAITING (Supervisor is BLOCKED" in status_text

    # Must instruct Architect honesty
    assert "CRITICAL CURRENT STATE: Supervisor orchestration is BLOCKED" in status_text
    assert "NEVER claim 'the orchestration is working', 'nothing broke'" in status_text


def test_authoring_gate_permits_architect_repair_during_supervisor_run(tmp_path: Path):
    """AuthoringGate allows authorized author (claude) to repair active work orders

    when a supervisor run is active or blocked.
    """
    sync_dir = tmp_path / ".sync"
    wo_dir = sync_dir / "work-orders" / "ACTIVE"
    sup_dir = sync_dir / "runtime" / "supervisor"
    wo_dir.mkdir(parents=True, exist_ok=True)
    sup_dir.mkdir(parents=True, exist_ok=True)

    # Existing WO-001
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "milestone_id": "Milestone 1",
            "type": "FEATURE",
            "priority": "P1",
            "title": "Initial Task",
            "status": "ACTIVE",
            "revision": 1,
            "description": "Initial task description for frontend build.",
            "assigned_agents": ["gemini"],
            "dependencies": [],
            "acceptance_criteria": ["Initial criteria met"],
            "deliverable": {
                "type": "code",
                "path": "index.html",
                "description": "Initial landing page HTML deliverable.",
            },
        }),
        encoding="utf-8",
    )

    # Supervisor is in ARCHITECT_REPAIR
    (sup_dir / "run-test.yaml").write_text(
        yaml.safe_dump({
            "run_id": "run-test",
            "phase": "ARCHITECT_REPAIR",
            "readiness_issues": [{"code": "TEST_COVERAGE_UNPLANNED", "message": "Repair needed"}],
        }),
        encoding="utf-8",
    )

    gate = AuthoringGate(project_root=tmp_path)
    updated_yaml = yaml.safe_dump({
        "id": "WO-001",
        "milestone_id": "Milestone 1",
        "type": "FEATURE",
        "priority": "P1",
        "title": "Repaired Task with Companion Test",
        "status": "ACTIVE",
        "description": "Repaired task description with companion test declared.",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "acceptance_criteria": ["Repaired criteria met"],
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "Repaired landing page HTML deliverable.",
        },
        "test_plan": [{"sources": ["index.html"], "tests": ["tests/test_index.py"]}],
    })

    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        updated_yaml,
        agent="claude",
    )

    assert decision.passed is True
    # Auto-bumped revision
    data = yaml.safe_load(decision.normalized_content)
    assert data["revision"] >= 2
