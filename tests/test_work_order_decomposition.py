"""Phase 2 Regression Tests: Work-Order Decomposition and File Budgets (SDP-FIX-001).

Acceptance Criteria:
- A milestone requiring a frontend page, an application entry point, and a stylesheet
  can generate work orders capable of delivering all required artifacts.
- A multi-file backend feature can declare and satisfy its explicit file budget.
- Independent deliverables can be split into child work orders with correct dependencies.
- Overlapping file ownership is detected or rejected.
- A work order exceeding its declared budget or allowed scope is still rejected.
- Ambiguous deliverables fail closed with a useful diagnostic.
- Existing single-file work orders continue to work.
- Approved-plan constraints remain authoritative and are not bypassed by decomposition.
"""

from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from validators.kernel.daemon.authoring import (
    synthesize_child_work_orders,
    decompose_plan_milestones,
    validate_work_orders_ownership,
    build_child_work_order,
    build_child_contract,
    extract_candidate_paths,
)
from validators.harness.plan import parse_plan, PlanValidationError
from validators.harness.authoring_readiness import validate_authoring_readiness
from validators.harness.contract_gate import verify_post_execution, ContractAccessDenied
from validators.kernel.security import BudgetTracker, BudgetExceededError


def test_frontend_multi_file_milestone_decomposition(tmp_path: Path):
    """A milestone requiring a frontend page, entry point, and stylesheet decomposes
    into 3 work orders capable of delivering all required artifacts, all sharing milestone_id.
    """
    plan_content = """# Project Plan: Multi-File Web Portal

## Current Architecture
Node.js, Express, and HTML5/CSS3 frontend.

## Milestones & Roadmap
- [ ] Milestone 1: Frontend Interface (Agent: gemini) (Skills: frontend-design)
  - [ ] Task 1.1: Create landing page at `src/landing.html`
  - [ ] Task 1.2: Build client logic at `src/app.js`
  - [ ] Task 1.3: Add stylesheets at `src/style.css`
- [ ] Milestone 2: QA Verification (Agent: gemma)
  - [ ] Task 2.1: Run test suite at `tests/test_frontend.test.js`
"""
    wos = synthesize_child_work_orders(tmp_path, plan_content)

    # Milestone 1 decomposed into 3 work orders + 1 QA work order = 4 total
    assert len(wos) == 4

    wo1 = wos[0]
    wo2 = wos[1]
    wo3 = wos[2]
    wo4 = wos[3]

    # Verify WO-001 (Landing page)
    assert wo1["id"] == "WO-001"
    assert wo1["milestone_id"] == "Milestone 1"
    assert wo1["assigned_agents"] == ["gemini"]
    assert wo1["deliverable"]["path"] == "src/landing.html"
    assert wo1["dependencies"] == []
    assert "frontend-design" in wo1.get("skills", [])

    # Verify WO-002 (App JS)
    assert wo2["id"] == "WO-002"
    assert wo2["milestone_id"] == "Milestone 1"
    assert wo2["assigned_agents"] == ["gemini"]
    assert wo2["deliverable"]["path"] == "src/app.js"
    assert wo2["dependencies"] == ["WO-001"]
    assert "frontend-design" in wo2.get("skills", [])

    # Verify WO-003 (Style CSS)
    assert wo3["id"] == "WO-003"
    assert wo3["milestone_id"] == "Milestone 1"
    assert wo3["assigned_agents"] == ["gemini"]
    assert wo3["deliverable"]["path"] == "src/style.css"
    assert wo3["dependencies"] == ["WO-002"]
    assert "frontend-design" in wo3.get("skills", [])

    # Verify WO-004 (QA depends on WO-003)
    assert wo4["id"] == "WO-004"
    assert wo4["milestone_id"] == "Milestone 2"
    assert wo4["assigned_agents"] == ["gemma"]
    assert wo4["dependencies"] == ["WO-003"]

    # Verify all contracts on disk cover the exact deliverable paths
    c1 = yaml.safe_load((tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8"))
    c2 = yaml.safe_load((tmp_path / ".sync" / "contracts" / "WO-002.yaml").read_text(encoding="utf-8"))
    c3 = yaml.safe_load((tmp_path / ".sync" / "contracts" / "WO-003.yaml").read_text(encoding="utf-8"))

    c1_allows = [r["module"] for r in c1["scope"]["allow"]]
    c2_allows = [r["module"] for r in c2["scope"]["allow"]]
    c3_allows = [r["module"] for r in c3["scope"]["allow"]]

    assert "src/landing.html" in c1_allows
    assert "src/app.js" in c2_allows
    assert "src/style.css" in c3_allows


def test_cohesive_multi_file_backend_feature_explicit_budget(tmp_path: Path):
    """A cohesive multi-file backend feature declares and satisfies an explicit file budget
    covering all files and companion test, without being reduced to a budget of 1.
    """
    plan_content = """# Project Plan: User Authentication API

## Current Architecture
Python backend with FastAPI.

## Milestones & Roadmap
- [ ] Milestone 1: User Model and Auth Routes (Agent: codex)
  - [ ] Task 1.1: Implement database model in `src/models/user.py` and API routes in `src/routes/auth.py`
"""
    wos = synthesize_child_work_orders(tmp_path, plan_content)

    assert len(wos) == 1
    wo = wos[0]

    assert wo["id"] == "WO-001"
    assert wo["deliverable"]["path"] == "src/models/user.py"
    assert "deliverables" in wo
    deliv_paths = [d["path"] for d in wo["deliverables"]]
    assert "src/models/user.py" in deliv_paths
    assert "src/routes/auth.py" in deliv_paths

    estimate = wo["implementation_estimate"]
    assert estimate is not None
    # Expected files must contain user.py, auth.py, and companion test
    assert "src/models/user.py" in estimate["expected_files"]
    assert "src/routes/auth.py" in estimate["expected_files"]
    assert any("test" in f for f in estimate["expected_files"])

    # File budget must match expected files count (3), not default to 1
    assert estimate["max_files_touched"] == len(estimate["expected_files"])
    assert estimate["max_files_touched"] >= 3

    contract = yaml.safe_load((tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8"))
    assert contract["budget"]["max_files_touched"] == estimate["max_files_touched"]

    contract_allows = [r["module"] for r in contract["scope"]["allow"]]
    assert "src/models/user.py" in contract_allows
    assert "src/routes/auth.py" in contract_allows


def test_independent_deliverables_split_with_dependencies(tmp_path: Path):
    """Decomposed deliverables establish a sequential dependency DAG and pass AuthoringReadiness."""
    plan_content = """# Project Plan: Core Infrastructure

## Current Architecture
Python backend services.

## Milestones & Roadmap
- [ ] Milestone 1: Services (Agent: codex)
  - [ ] Task 1.1: Build storage service at `src/storage.py`
  - [ ] Task 1.2: Build cache service at `src/cache.py`
- [ ] Milestone 2: QA (Agent: gemma)
  - [ ] Task 2.1: Run tests at `tests/test_services.py`
"""
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
    wos = synthesize_child_work_orders(tmp_path, plan_content)

    assert len(wos) == 3
    assert wos[0]["deliverable"]["path"] == "src/storage.py"
    assert wos[1]["deliverable"]["path"] == "src/cache.py"
    assert wos[1]["dependencies"] == ["WO-001"]
    assert wos[2]["dependencies"] == ["WO-002"]

    # Validate with authoring readiness gate
    readiness = validate_authoring_readiness(tmp_path)
    # Filter issues: should have zero readiness issues for milestone coverage or dependencies
    ownership_or_cov_issues = [
        i for i in readiness.issues
        if i.code in ("MILESTONE_AMBIGUOUS", "MILESTONE_UNCOVERED", "DEPENDENCY_CYCLE", "OVERLAPPING_FILE_OWNERSHIP")
    ]
    assert len(ownership_or_cov_issues) == 0, f"Unexpected readiness issues: {ownership_or_cov_issues}"


def test_overlapping_file_ownership_detected_and_rejected(tmp_path: Path):
    """Parallel work orders claiming the same deliverable without dependencies are rejected."""
    # Case A: validate_work_orders_ownership directly
    wo_parallel_1 = {
        "id": "WO-001",
        "deliverable": {"type": "code", "path": "src/api/auth.py"},
        "dependencies": [],
    }
    wo_parallel_2 = {
        "id": "WO-002",
        "deliverable": {"type": "code", "path": "src/api/auth.py"},
        "dependencies": [],
    }

    is_valid, errors = validate_work_orders_ownership([wo_parallel_1, wo_parallel_2])
    assert not is_valid
    assert any("Overlapping file ownership" in e and "src/api/auth.py" in e for e in errors)

    # Case B: Sequential work orders claiming the same deliverable pass ownership check
    wo_seq_2 = dict(wo_parallel_2)
    wo_seq_2["dependencies"] = ["WO-001"]
    is_valid_seq, errors_seq = validate_work_orders_ownership([wo_parallel_1, wo_seq_2])
    assert is_valid_seq
    assert len(errors_seq) == 0

    # Case C: AuthoringReadiness detects parallel ownership across active work orders
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    w1 = build_child_work_order("WO-001", "Auth 1", "Desc 1", ["codex"], [], "P1", {"type": "code", "path": "src/auth.py"})
    w2 = build_child_work_order("WO-002", "Auth 2", "Desc 2", ["codex"], [], "P1", {"type": "code", "path": "src/auth.py"})
    c1 = build_child_contract("WO-001", "codex", "backend", deliverable_path="src/auth.py", max_files_touched=2)
    c2 = build_child_contract("WO-002", "codex", "backend", deliverable_path="src/auth.py", max_files_touched=2)

    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump(w1), encoding="utf-8")
    (active_dir / "WO-002.yaml").write_text(yaml.safe_dump(w2), encoding="utf-8")
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(c1), encoding="utf-8")
    (contracts_dir / "WO-002.yaml").write_text(yaml.safe_dump(c2), encoding="utf-8")

    result = validate_authoring_readiness(tmp_path)
    ownership_issues = [i for i in result.issues if i.code == "OVERLAPPING_FILE_OWNERSHIP"]
    assert len(ownership_issues) >= 1
    assert "src/auth.py" in ownership_issues[0].message


def test_work_order_exceeding_budget_rejected(tmp_path: Path):
    """A worker attempting to touch more files than permitted by its explicit budget is rejected."""
    tracker = BudgetTracker(max_files_touched=2)

    # Touch 1st file: ok
    tracker.record_file("src/models/user.py")
    ok, err = tracker.check_budget()
    assert ok
    assert err is None

    # Touch 2nd file: ok
    tracker.record_file("src/routes/auth.py")
    ok, err = tracker.check_budget()
    assert ok
    assert err is None

    # Touch 3rd file: exceeds max_files_touched=2!
    tracker.record_file("src/extra.py")
    ok, err = tracker.check_budget()
    assert not ok
    assert "file budget exceeded" in err.lower()
    with pytest.raises(BudgetExceededError):
        tracker.assert_within_budget("worker execution")


def test_ambiguous_deliverables_fail_closed(tmp_path: Path):
    """A plan with conflicting or unresolvable stack/deliverable requirements fails closed."""
    # Stack with conflicting backend technologies
    conflicting_plan = """# Project Plan: Conflicting App

## Architecture
- Backend: Python FastAPI and Node.js Express conflicting
- Frontend: React

## Milestones & Roadmap
- [ ] Milestone 1: Server Logic
  - [ ] Task 1.1: Build backend
"""
    with pytest.raises(PlanValidationError) as exc_info:
        synthesize_child_work_orders(tmp_path, conflicting_plan)

    assert "ambiguous" in str(exc_info.value).lower()


def test_existing_single_file_work_orders_continue_to_work(tmp_path: Path):
    """Existing single-file tasks continue to synthesize cleanly with companion tests."""
    plan_content = """# Project Plan: Simple Utility

## Current Architecture
Python module.

## Milestones & Roadmap
- [ ] Milestone 1: Implement String Utility (Agent: codex)
  - [ ] Task 1.1: Build string helper in `src/string_utils.py`
  - [ ] Task 1.2: Add unit tests in `tests/test_string_utils.py`
"""
    wos = synthesize_child_work_orders(tmp_path, plan_content)

    assert len(wos) == 1
    wo = wos[0]
    assert wo["id"] == "WO-001"
    assert wo["deliverable"]["path"] == "src/string_utils.py"
    assert "tests/test_string_utils.py" in wo["implementation_estimate"]["expected_files"]
    assert wo["implementation_estimate"]["max_files_touched"] == 2


def test_approved_plan_constraints_authoritative_under_decomposition(tmp_path: Path):
    """Decomposition retains the approved stack frameworks and conventions without fallback."""
    node_plan = """# Project Plan: Express API Service

## Architecture
- Backend: Node.js with Express
- Database: PostgreSQL

## Milestones & Roadmap
- [ ] Milestone 1: Authentication & User Service (Agent: codex)
  - [ ] Task 1.1: Implement user router in `src/routes/users.js`
  - [ ] Task 1.2: Implement auth middleware in `src/middleware/auth.js`
"""
    wos = synthesize_child_work_orders(tmp_path, node_plan)

    assert len(wos) == 2
    assert wos[0]["deliverable"]["path"] == "src/routes/users.js"
    assert wos[1]["deliverable"]["path"] == "src/middleware/auth.js"
    assert wos[1]["dependencies"] == ["WO-001"]

    # Verify neither work order fell back to Python or generic files
    for w in wos:
        assert not w["deliverable"]["path"].endswith(".py")
