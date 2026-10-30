"""Regression tests for sequential milestone deliverable deduplication,
cycle prevention, and GitOps deliverable extraction.
"""

from pathlib import Path
import yaml
import pytest

from validators.kernel.daemon.authoring import (
    reconcile_active_work_orders_with_plan,
    extract_deliverable_spec,
    extract_candidate_paths,
)
from validators.harness.authoring_readiness import validate_authoring_readiness


def test_gitops_deliverable_extracts_version_doc_over_package_json():
    """GitOps role must extract version/release doc rather than claiming package.json config."""
    m_title = "GitOps — reconcile version/coverage documentation"
    tasks = [
        "Task 5.1 (BUG-007): Align `VERSION`, `package.json` `version`, and README claimed version.",
        "Task 5.2: Update `CHANGELOG.md` if present.",
        "Task 5.3: Commit release artifacts.",
    ]
    spec = extract_deliverable_spec(
        milestone_title=m_title,
        tasks=tasks,
        role="local-llm",
    )
    assert spec["type"] == "doc"
    assert spec["path"] in ("VERSION", "VERSION.md", "CHANGELOG.md")
    assert spec["path"] != "package.json"


def test_sequential_milestones_sharing_deliverable_do_not_collide(tmp_path: Path):
    """Sequential milestones sharing a deliverable path (e.g. package.json)

    must NOT trigger Rule 1c collision cancellation or circular dependencies.
    """
    plan_content = """# Project Plan: Bug-Fix

## Current Architecture
Node.js authentication app.

## Milestones & Roadmap
- [ ] Milestone 1: Scaffold backend dependencies — add cookie-parser (Agent: codex)
  - [ ] Task 1.1: Update package.json to add cookie-parser
- [ ] Milestone 2: Backend — issue JWT as httpOnly cookie (Agent: codex)
  - [ ] Task 2.1: In src/controllers/authController.js, set cookie
- [ ] Milestone 3: Frontend — eliminate localStorage (Agent: gemini)
  - [ ] Task 3.1: In app.js, clear storage
- [ ] Milestone 4: QA — extend tests (Agent: gemma)
  - [ ] Task 4.1: Extend tests/auth.test.js
- [ ] Milestone 5: GitOps — reconcile version/coverage documentation (Agent: local-llm)
  - [ ] Task 5.1: Align VERSION, package.json version, and README
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # WO-001 (Milestone 1, codex, deliverable package.json)
    (act_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "milestone_id": "Milestone 1",
            "title": "Scaffold backend dependencies",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "config", "path": "package.json"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )
    # WO-002 (Milestone 2, codex, depends on WO-001)
    (act_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "milestone_id": "Milestone 2",
            "title": "Backend API",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": ["WO-001"],
            "deliverable": {"type": "code", "path": "src/controllers/authController.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )
    # WO-003 (Milestone 3, gemini, depends on WO-002)
    (act_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003",
            "milestone_id": "Milestone 3",
            "title": "Frontend UI",
            "status": "ACTIVE",
            "assigned_agents": ["gemini"],
            "dependencies": ["WO-002"],
            "deliverable": {"type": "code", "path": "app.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )
    # WO-004 (Milestone 4, gemma, depends on WO-003)
    (act_dir / "WO-004.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-004",
            "milestone_id": "Milestone 4",
            "title": "QA Tests",
            "status": "ACTIVE",
            "assigned_agents": ["gemma"],
            "dependencies": ["WO-003"],
            "deliverable": {"type": "code", "path": "tests/auth.test.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )
    # WO-005 (Milestone 5, local-llm, also touches package.json, depends on WO-004)
    (act_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-005",
            "milestone_id": "Milestone 5",
            "title": "GitOps Release",
            "status": "ACTIVE",
            "assigned_agents": ["local-llm"],
            "dependencies": ["WO-004"],
            "deliverable": {"type": "config", "path": "package.json"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    # Reconcile work orders
    reconciled = reconcile_active_work_orders_with_plan(tmp_path)

    # WO-001 must NOT be cancelled
    wo1 = yaml.safe_load((act_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert wo1["status"] == "ACTIVE"

    # WO-002 must still depend on WO-001, NOT WO-005
    wo2 = yaml.safe_load((act_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert wo2["dependencies"] == ["WO-001"]

    # WO-005 must still depend on WO-004
    wo5 = yaml.safe_load((act_dir / "WO-005.yaml").read_text(encoding="utf-8"))
    assert wo5["dependencies"] == ["WO-004"]


def test_reconcile_un_cancels_erroneously_deduplicated_work_order(tmp_path: Path):
    """When a work order was erroneously cancelled due to duplicate deliverable,

    reconciliation must un-cancel it and restore the correct dependency chain.
    """
    plan_content = """# Project Plan: Bug-Fix

## Current Architecture
Node.js authentication app.

## Milestones & Roadmap
- [ ] Milestone 1: Scaffold backend dependencies — add cookie-parser (Agent: codex)
  - [ ] Task 1.1: Update package.json to add cookie-parser
- [ ] Milestone 2: Backend — issue JWT as httpOnly cookie (Agent: codex)
  - [ ] Task 2.1: In src/controllers/authController.js, set cookie
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # WO-001 erroneously CANCELLED by prior duplicate deliverable bug
    (act_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "milestone_id": "Milestone 1",
            "title": "Scaffold backend dependencies — add cookie-parser",
            "status": "CANCELLED",
            "cancellation_reason": "Superseded by WO-005 (duplicate deliverable 'package.json')",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "config", "path": "package.json"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )
    # WO-002 pointing to WO-001
    (act_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "milestone_id": "Milestone 2",
            "title": "Backend — issue JWT as httpOnly cookie",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "dependencies": ["WO-001"],
            "deliverable": {"type": "code", "path": "src/controllers/authController.js"},
            "synthesized_by": "bootstrap",
        }),
        encoding="utf-8",
    )

    reconciled = reconcile_active_work_orders_with_plan(tmp_path)

    # WO-001 must be revived to ACTIVE
    wo1 = yaml.safe_load((act_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert wo1["status"] == "ACTIVE"
    assert "error" not in wo1
    assert "cancellation_reason" not in wo1

    # WO-002 dependencies must remain valid
    wo2 = yaml.safe_load((act_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert wo2["dependencies"] == ["WO-001"]
