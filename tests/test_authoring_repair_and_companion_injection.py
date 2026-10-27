"""Regression tests for AuthoringGate repair overwrite allowance, companion test injection,

and supervisor repair turn schema guidance.
"""

from pathlib import Path
import yaml
import pytest

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.authoring_readiness import validate_authoring_readiness


def test_authoring_gate_allows_repair_of_blocked_work_order(tmp_path: Path):
    """AuthoringGate allows authorized authors to repair a BLOCKED work order

    without raising Overwrite conflict, auto-incrementing revision if needed.
    """
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    existing_wo = wo_dir / "WO-001.yaml"
    existing_data = {
        "id": "WO-001",
        "milestone_id": "Milestone 1",
        "type": "FEATURE",
        "title": "Landing Website",
        "status": "BLOCKED",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "Landing page",
        },
        "description": "Initial failing implementation",
        "revision": 1,
        "synthesized_by": "claude",
        "error": "Authoring readiness gate still failing after repair (TEST_COVERAGE_UNPLANNED)",
    }
    existing_wo.write_text(yaml.safe_dump(existing_data, sort_keys=False), encoding="utf-8")

    repaired_data = dict(existing_data)
    repaired_data["description"] = "Repaired implementation with companion test declared"
    repaired_data["test_plan"] = [{"sources": ["index.html"], "tests": ["tests/test_index.py"]}]
    repaired_data.pop("error", None)

    # Note: fresh gate simulates a new turn / gateway invocation
    gate = AuthoringGate(project_root=tmp_path)
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(repaired_data, sort_keys=False),
        agent="claude",
        project_root=tmp_path,
    )
    assert decision.passed is True, f"Repair was rejected: {decision.errors}"
    assert decision.normalized_content is not None
    loaded_norm = yaml.safe_load(decision.normalized_content)
    # Revision auto-bumped to 2
    assert loaded_norm["revision"] == 2
    assert any(a.get("revision") == 2 for a in loaded_norm.get("amendments", []))


def test_authoring_gate_allows_repair_of_contract_for_blocked_work_order(tmp_path: Path):
    """AuthoringGate allows authorized authors to repair a contract bound to a BLOCKED work order."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "status": "BLOCKED",
            "error": "Readiness gate failure",
        }, sort_keys=False),
        encoding="utf-8",
    )

    contract_dir = tmp_path / ".sync" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    existing_contract = contract_dir / "WO-001.yaml"
    existing_contract.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "gemini",
            "work_order": "WO-001",
            "contract_revision": 1,
            "identity": {"role": "frontend", "reports_to": "claude"},
            "scope": {"allow": [{"module": "index.html"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
            "budget": {"max_files_touched": 5, "max_tokens": 30000},
        }, sort_keys=False),
        encoding="utf-8",
    )

    repaired_contract = {
        "schema_version": 1,
        "agent_id": "gemini",
        "work_order": "WO-001",
        "contract_revision": 1,  # Same revision submitted by repair
        "identity": {"role": "frontend", "reports_to": "claude"},
        "scope": {"allow": [{"module": "index.html"}, {"module": "tests/**"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": 8, "max_tokens": 50000},
    }

    gate = AuthoringGate(project_root=tmp_path)
    decision = gate.validate_artifact_content(
        ".sync/contracts/WO-001.yaml",
        yaml.safe_dump(repaired_contract, sort_keys=False),
        agent="claude",
        project_root=tmp_path,
    )
    assert decision.passed is True, f"Contract repair was rejected: {decision.errors}"
    assert decision.normalized_content is not None
    loaded_norm = yaml.safe_load(decision.normalized_content)
    assert loaded_norm["contract_revision"] == 2


def test_authoring_compiler_injects_companion_test_when_zero_planned_tests(tmp_path: Path):
    """authoring_compiler injects default companion test planning when a code deliverable

    has no planned tests, preventing TEST_COVERAGE_UNPLANNED from blocking single-milestone projects.
    """
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir = tmp_path / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)

    wo_data = {
        "id": "WO-001",
        "milestone_id": "Milestone 1",
        "type": "FEATURE",
        "title": "Local Landing Website",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "path": "index.html",
            "description": "Landing page",
        },
        "description": "Frontend single page landing site",
        "revision": 1,
        "synthesized_by": "claude",
        "implementation_estimate": {
            "expected_files": ["index.html", "assets/css/styles.css"],
            "max_files_touched": 4,
        },
    }
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_data, sort_keys=False), encoding="utf-8")

    contract_data = {
        "schema_version": 1,
        "agent_id": "gemini",
        "work_order": "WO-001",
        "identity": {"role": "frontend", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "index.html"}, {"module": "assets/**"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 4, "max_tokens": 50000},
    }
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(contract_data, sort_keys=False), encoding="utf-8")

    plan_milestones = [{"id": "Milestone 1", "title": "Local Landing Website", "agent": "gemini"}]

    # Run compiler
    compile_result = compile_authoring_artifacts(tmp_path, plan_milestones, planning_wo_id="WO-000")
    assert not compile_result.decision_required

    # Check that work order was compiled with test_plan
    updated_wo = yaml.safe_load((wo_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert "test_plan" in updated_wo
    assert updated_wo["test_plan"] == [{"sources": ["index.html"], "tests": ["tests/test_index.py"]}]
    assert "tests/test_index.py" in updated_wo["implementation_estimate"]["expected_files"]

    # Check that contract scope allows tests/**
    updated_contract = yaml.safe_load((contracts_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    allow_modules = [m.get("module") for m in updated_contract["scope"]["allow"] if isinstance(m, dict)]
    assert "tests/**" in allow_modules

    # Now run readiness gate
    readiness = validate_authoring_readiness(
        tmp_path,
        plan_id="PLAN-001",
        expected_milestones=plan_milestones,
        planning_wo_id="WO-000",
    )
    # Gate must PASS cleanly with zero issues!
    assert readiness.ready is True, f"Readiness issues: {[f'{i.code}: {i.message}' for i in readiness.issues]}"
