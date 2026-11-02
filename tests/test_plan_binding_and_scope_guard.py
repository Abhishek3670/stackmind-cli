"""Tests for Phase 2: Plan Binding & Scope Drift Guard (Checklist C.2).

Verifies:
1. When a work order specifies `plan_binding_hash`, it must match the SHA-256 hash
   of the approved PLAN.md on disk.
2. A mismatch between `plan_binding_hash` and active `PLAN.md` causes authoring gate rejection.
3. Follow-up work orders whose declared deliverables are referenced in PLAN.md pass scope check.
4. Follow-up work orders whose declared deliverables fall outside the scope of PLAN.md
   are rejected with a Scope drift violation.
5. Standard infrastructure, test suites, and documentation files are exempt from scope drift rejection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import pytest
import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.authoring_semantic import _validate_work_order
from validators.harness.review_gate import plan_hash


@pytest.fixture
def plan_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)

    # Approved PLAN.md with explicit roadmap and deliverables
    plan_content = """# Approved Project Plan: Web Calculator

## Goal
Build a clean, responsive web calculator.

## Milestones & Deliverables
- Milestone 1: Core Calculation Logic
  - Deliverable: src/calculator.js
- Milestone 2: UI Components
  - Deliverable: src/ui.js
- Milestone 3: Documentation
  - Deliverable: docs/user_guide.md
"""
    (ws / "PLAN.md").write_text(plan_content, encoding="utf-8")
    return ws


def test_matching_plan_binding_hash_passes(plan_workspace: Path):
    expected_hash = plan_hash(plan_workspace)
    assert expected_hash is not None

    wo_data = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Core calculation engine",
        "plan_binding_hash": expected_hash,
        "deliverable": {
            "type": "code",
            "path": "src/calculator.js",
            "description": "Arithmetic calculation functions",
        },
    }

    errors: list[str] = []
    _validate_work_order("WO-001.yaml", wo_data, errors, project_root=plan_workspace)
    assert not errors, f"Expected no errors but got: {errors}"


def test_mismatched_plan_binding_hash_fails(plan_workspace: Path):
    stale_hash = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    wo_data = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Core calculation engine",
        "plan_binding_hash": stale_hash,
        "deliverable": {
            "type": "code",
            "path": "src/calculator.js",
            "description": "Arithmetic calculation functions",
        },
    }

    errors: list[str] = []
    _validate_work_order("WO-001.yaml", wo_data, errors, project_root=plan_workspace)
    assert any("plan_binding_hash" in err and "does not match approved PLAN.md hash" in err for err in errors)


def test_deliverable_within_plan_scope_passes(plan_workspace: Path):
    wo_data = {
        "id": "WO-002",
        "type": "FEATURE",
        "title": "Calculator UI Layout",
        "plan_binding_hash": plan_hash(plan_workspace),
        "deliverable": {
            "type": "code",
            "path": "src/ui.js",
            "description": "Keypad and display layout components",
        },
    }

    errors: list[str] = []
    _validate_work_order("WO-002.yaml", wo_data, errors, project_root=plan_workspace)
    assert not errors, f"Expected no errors for in-scope deliverable, got: {errors}"


def test_scope_drift_unapproved_deliverable_rejected(plan_workspace: Path):
    wo_data = {
        "id": "WO-003",
        "type": "FEATURE",
        "title": "Cryptocurrency Miner Plugin",
        "plan_binding_hash": plan_hash(plan_workspace),
        "deliverable": {
            "type": "code",
            "path": "src/crypto_miner.py",
            "description": "Background proof of work miner",
        },
    }

    errors: list[str] = []
    _validate_work_order("WO-003.yaml", wo_data, errors, project_root=plan_workspace)
    assert any("Scope drift violation" in err and "src/crypto_miner.py" in err for err in errors)


def test_exempt_deliverables_bypass_scope_drift(plan_workspace: Path):
    # Tests, documentation, and metadata files must be allowed without false positives
    exempt_cases = [
        {"path": "tests/test_calculator.py", "type": "code", "desc": "Unit tests for calculator"},
        {"path": "README.md", "type": "doc", "desc": "Project overview"},
        {"path": "package.json", "type": "config", "desc": "Project dependencies"},
    ]

    for case in exempt_cases:
        wo_data = {
            "id": "WO-004",
            "type": "FEATURE",
            "title": f"Update {case['path']}",
            "plan_binding_hash": plan_hash(plan_workspace),
            "deliverable": {
                "type": case["type"],
                "path": case["path"],
                "description": case["desc"],
            },
        }
        errors: list[str] = []
        _validate_work_order("WO-004.yaml", wo_data, errors, project_root=plan_workspace)
        assert not errors, f"Exempt file {case['path']} should not trigger scope drift: {errors}"


def test_authoring_gate_end_to_end_validation(plan_workspace: Path):
    # Verify through the AuthoringGate validate pipeline
    gate = AuthoringGate(schemas_dir=Path(__file__).resolve().parent.parent / "schemas", project_root=plan_workspace)

    valid_hash = plan_hash(plan_workspace)
    valid_wo = yaml.safe_dump({
        "id": "WO-001",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "title": "Calculation engine",
        "plan_binding_hash": valid_hash,
        "deliverable": {
            "type": "code",
            "path": "src/calculator.js",
            "description": "Calculation logic",
        },
    })
    decision_valid = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", valid_wo, agent="claude")
    assert decision_valid.passed is True, f"Expected pass, got errors: {decision_valid.errors}"

    # Rogue work order with scope drift
    rogue_wo = yaml.safe_dump({
        "id": "WO-002",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "title": "Unapproved Service",
        "plan_binding_hash": valid_hash,
        "deliverable": {
            "type": "code",
            "path": "src/unapproved_service.js",
            "description": "Service not in PLAN.md",
        },
    })
    decision_rogue = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", rogue_wo, agent="claude")
    assert decision_rogue.passed is False
    assert any("Scope drift violation" in e for e in decision_rogue.errors)
