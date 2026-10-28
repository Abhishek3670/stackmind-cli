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


def test_work_order_schema_allows_cancellation_reason(tmp_path: Path):
    """Work order schema must allow cancellation_reason property without validation errors."""
    gate = AuthoringGate(project_root=tmp_path)
    wo_yaml = yaml.safe_dump({
        "id": "WO-015",
        "milestone_id": "Milestone 2",
        "type": "FEATURE",
        "priority": "P1",
        "title": "Cancelled Task",
        "status": "CANCELLED",
        "description": "Cancelled task description.",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "acceptance_criteria": ["Cancelled criteria"],
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "Landing page deliverable",
        },
        "error": "Superseded by WO-007",
        "cancellation_reason": "Superseded by WO-007 for milestone Milestone 2",
    })

    decision = gate.validate_artifact_content(
        ".sync/work-orders/BLOCKED/WO-015.yaml",
        wo_yaml,
        agent="claude",
    )
    assert decision.passed is True
    assert not any("cancellation_reason" in e for e in decision.errors)


def test_cli_validate_permits_compatible_statuses_in_blocked_dir(tmp_path: Path):
    """cli/validate.py accepts CANCELLED and BLOCKED as compatible non-executable states."""
    from cli.validate import _validate_work_order_state_files, ValidationResult

    sync_dir = tmp_path / ".sync"
    blocked_dir = sync_dir / "work-orders" / "BLOCKED"
    blocked_dir.mkdir(parents=True, exist_ok=True)

    wo_file = blocked_dir / "WO-001.yaml"
    wo_file.write_text(yaml.safe_dump({
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Cancelled Order",
        "status": "CANCELLED",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "app.py", "description": "app"},
        "description": "desc",
    }), encoding="utf-8")

    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_file.write_text(yaml.safe_dump({
        "schema_version": 1,
        "orders": [{
            "id": "WO-001",
            "status": "BLOCKED",
            "file": "work-orders/BLOCKED/WO-001.yaml",
        }]
    }), encoding="utf-8")

    result = ValidationResult()
    _validate_work_order_state_files(sync_dir, result)
    protocol_errors = [i for i in result.issues if i.layer == "Protocol" and "status 'CANCELLED' does not match" in i.message]
    assert len(protocol_errors) == 0


def test_authoring_readiness_ignores_contracts_for_blocked_work_orders(tmp_path: Path):
    """Contracts whose corresponding work orders are archived in BLOCKED are not orphan contracts."""
    sync_dir = tmp_path / ".sync"
    blocked_dir = sync_dir / "work-orders" / "BLOCKED"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    contracts_dir = sync_dir / "contracts"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Active work order and contract
    (active_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002",
        "milestone_id": "Milestone 1",
        "type": "FEATURE",
        "title": "Active Task",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "index.html", "description": "desc"},
        "description": "Active task description",
        "test_plan": [{"sources": ["index.html"], "tests": ["tests/test_index.py"]}],
    }), encoding="utf-8")
    (contracts_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "work_order": "WO-002",
        "agent_id": "gemini",
        "scope": {"allow": [{"module": "index.html"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 5, "max_tokens": 10000},
    }), encoding="utf-8")

    # Archived/cancelled work order and its historical contract
    (blocked_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Cancelled Task",
        "status": "CANCELLED",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "pkg.json", "description": "desc"},
        "description": "Cancelled task",
    }), encoding="utf-8")
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "work_order": "WO-001",
        "agent_id": "codex",
        "scope": {"allow": [{"module": "pkg.json"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 2, "max_tokens": 5000},
    }), encoding="utf-8")

    result = validate_authoring_readiness(
        tmp_path,
        plan_id="PLAN-001",
        expected_milestones=[{"id": "Milestone 1", "title": "Active Task", "agent": "gemini"}],
    )
    assert not any(issue.code == "ORPHAN_CONTRACT" and "WO-001" in issue.message for issue in result.issues)
