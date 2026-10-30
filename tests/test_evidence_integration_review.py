"""Evidence-Based Integration Review Regression Test Suite.

Verifies deterministic, inspectable evidence-based integration gating:
1. Fabricated LLM claim cannot override failed test command.
2. Missing mandatory test evidence blocks approval when test suite exists on disk.
3. Stale evidence from earlier artifact revision (hash mismatch) blocks approval.
4. Route contract mismatch (frontend calls undeclared endpoint) blocks approval.
5. Valid inspectable evidence allows approval path to transition to FINAL_REVIEW
   (the mandatory Architect final-acceptance gate follows).
6. Inapplicable checks are explicitly reported as skipped with rationale.
7. Retries remain bounded and fail closed to BLOCKED when exhausted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
import pytest
import yaml

from validators.harness.integration_gate import (
    IntegrationGate,
    IntegrationGateDecision,
    IntegrationCheck,
)
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from tests.test_lifecycle_supervisor import MockSessionManager


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _setup_base_workspace(ws: Path) -> None:
    (ws / ".sync" / "work-orders" / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (ws / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)
    idx_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(yaml.safe_dump({"orders": [], "next_id": 10}), encoding="utf-8")


def _create_completed_wo(ws: Path, wo_id: str, title: str, agent: str, deliv_path: str, deliv_type: str = "code") -> None:
    wo_dir = ws / ".sync" / "work-orders" / "COMPLETED"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / f"{wo_id}.yaml").write_text(
        yaml.safe_dump({
            "id": wo_id,
            "title": title,
            "assigned_agents": [agent],
            "status": "COMPLETED",
            "deliverable": {
                "type": deliv_type,
                "path": deliv_path,
                "description": title,
            },
        }),
        encoding="utf-8",
    )


def test_fabricated_llm_claim_cannot_override_failed_test_command(tmp_path: Path) -> None:
    """Requirement 1 & 5: LLM review claims 'completed', but telemetry records returncode != 0.
    Anti-fabrication check flags the discrepancy and blocks release progression."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # Create deliverable and test files
    src_file = tmp_path / "src" / "app.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Backend app", "codex", "src/app.py")

    test_file = tmp_path / "tests" / "test_app.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_add(): assert False\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "QA test suite", "gemma", "tests/test_app.py")

    state = supervisor.start_run("run-fab-1", "Calculator API", tmp_path, "sess-fab-1")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    # Dispatch integration review
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    rev_op_id = state.integration_operation_id
    assert rev_op_id is not None

    # LLM review claims success despite a failed test execution in commands_audit
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Everything passes 100%! All unit tests pass cleanly with zero defects.",
        "blockers": [],
        "commands_audit": [
            {"command": "pytest tests/test_app.py", "returncode": 1},
        ],
    }

    # Supervisor advance must reject approval and route to rework
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING  # routed to rework
    assert state.integration_rework_rounds == 1

    # Deterministic evidence confirms check failure
    ev = state.integration_evidence
    assert ev["passed"] is False
    assert any("Fabricated LLM review claim" in issue for issue in ev["issues"])
    assert any("Deterministic test failure" in issue for issue in ev["issues"])


def test_missing_mandatory_test_evidence_blocks_approval(tmp_path: Path) -> None:
    """Requirement 2: Test suites exist on disk, but no test execution evidence is recorded.
    Integration review must not approve when evidence is missing."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    src_file = tmp_path / "src" / "math_util.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def square(x): return x * x\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Math util", "codex", "src/math_util.py")

    test_file = tmp_path / "tests" / "test_math_util.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_square(): pass\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "QA test", "gemma", "tests/test_math_util.py")

    state = supervisor.start_run("run-missing-ev", "Math library", tmp_path, "sess-mev")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Looks good.",
        "blockers": [],
        "commands_audit": [],  # No test execution!
    }

    res = supervisor.advance(state)
    assert state.phase != Phase.PRODUCT_READY
    assert state.phase == Phase.EXECUTING  # routed to rework
    ev = state.integration_evidence
    assert ev["passed"] is False
    assert any("Missing mandatory test execution evidence" in issue for issue in ev["issues"])


def test_stale_evidence_from_earlier_artifact_revision_blocks_approval(tmp_path: Path) -> None:
    """Requirement 3: Deliverable modified after verification evidence was captured (hash mismatch).
    Must fail closed to prevent approvals based on stale runs."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    src_file = tmp_path / "src" / "service.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    initial_content = "def serve(): return 'v1'\n"
    src_file.write_text(initial_content, encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Service", "codex", "src/service.py")

    old_hash = _file_hash(src_file)

    # Now mutate the deliverable behind the scenes to simulate stale evidence
    src_file.write_text("def serve(): return 'v2-untested'\n", encoding="utf-8")

    state = supervisor.start_run("run-stale-ev", "Service", tmp_path, "sess-stale")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Verified against old revision.",
        "blockers": [],
        "verification_evidence": {
            "results": [
                {"relative_path": "src/service.py", "content_hash": old_hash, "passed": True}
            ]
        },
    }

    res = supervisor.advance(state)
    assert state.phase != Phase.PRODUCT_READY
    ev = state.integration_evidence
    assert ev["passed"] is False
    assert any("Stale evidence" in issue for issue in ev["issues"])


def test_route_contract_mismatch_blocks_approval(tmp_path: Path) -> None:
    """Requirement 4: Frontend calls an API endpoint not declared by backend routes.
    Must block approval and target the frontend work order for rework."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # Backend declares /api/users
    backend_file = tmp_path / "src" / "server.py"
    backend_file.parent.mkdir(parents=True, exist_ok=True)
    backend_file.write_text(
        "from flask import Flask\napp = Flask(__name__)\n\n@app.route('/api/users')\ndef users(): return []\n",
        encoding="utf-8",
    )
    _create_completed_wo(tmp_path, "WO-001", "Backend server", "codex", "src/server.py")

    # Frontend calls /api/orders (NOT declared in backend!)
    frontend_file = tmp_path / "src" / "client.js"
    frontend_file.write_text(
        "async function fetchOrders() {\n  const res = await fetch('/api/orders');\n  return res.json();\n}\n",
        encoding="utf-8",
    )
    _create_completed_wo(tmp_path, "WO-002", "Frontend client", "gemini", "src/client.js")

    state = supervisor.start_run("run-route-mismatch", "Web app", tmp_path, "sess-route")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Integration review looks complete.",
        "blockers": [],
    }

    res = supervisor.advance(state)
    assert state.phase != Phase.PRODUCT_READY
    assert state.phase == Phase.EXECUTING
    ev = state.integration_evidence
    assert ev["passed"] is False
    assert any("Route contract mismatch" in issue for issue in ev["issues"])


def test_valid_evidence_allows_approval_to_product_ready(tmp_path: Path) -> None:
    """Requirement 5: Valid deliverables, matching routes, successful test runs, and fresh hashes
    allow integration review to transition cleanly to Phase.PRODUCT_READY."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # Backend with /api/items
    backend_file = tmp_path / "src" / "api.py"
    backend_file.parent.mkdir(parents=True, exist_ok=True)
    b_code = "from flask import Flask\napp = Flask(__name__)\n\n@app.route('/api/items')\ndef items(): return []\n"
    backend_file.write_text(b_code, encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Backend API", "codex", "src/api.py")

    # Frontend calling /api/items
    frontend_file = tmp_path / "src" / "index.js"
    f_code = "fetch('/api/items').then(r => r.json());\n"
    frontend_file.write_text(f_code, encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "Frontend UI", "gemini", "src/index.js")

    # Tests for backend
    test_file = tmp_path / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    t_code = "def test_api(): assert True\n"
    test_file.write_text(t_code, encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-003", "QA tests", "gemma", "tests/test_api.py")

    b_hash = _file_hash(backend_file)
    f_hash = _file_hash(frontend_file)
    t_hash = _file_hash(test_file)

    state = supervisor.start_run("run-clean-approval", "Clean app", tmp_path, "sess-clean")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Integration review verified all deliverables.",
        "blockers": [],
        "commands_audit": [
            {"command": "pytest tests/test_api.py", "returncode": 0},
        ],
        "verification_evidence": {
            "results": [
                {"relative_path": "src/api.py", "content_hash": b_hash, "passed": True},
                {"relative_path": "src/index.js", "content_hash": f_hash, "passed": True},
                {"relative_path": "tests/test_api.py", "content_hash": t_hash, "passed": True},
            ]
        },
    }

    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    ev = state.integration_evidence
    assert ev["passed"] is True
    assert len(ev["issues"]) == 0


def test_inapplicable_checks_reported_as_skipped_with_rationale(tmp_path: Path) -> None:
    """Requirement 6: Inapplicable checks (e.g. no build manifest, no tests, no routes)
    must be explicitly recorded as 'skipped' with a reason, not falsely marked as passing."""
    _setup_base_workspace(tmp_path)

    # Workspace with only a standalone Python module (no build script, no test suite, no web API routes)
    mod_file = tmp_path / "util.py"
    mod_file.write_text("def helper(): return 42\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Utility", "codex", "util.py")

    gate = IntegrationGate(tmp_path)
    decision = gate.evaluate()

    assert decision.passed is True
    checks_by_name = {c.name: c for c in decision.checks}

    # Verify build_execution is skipped
    assert "build_execution" in checks_by_name
    assert checks_by_name["build_execution"].status == "skipped"
    assert checks_by_name["build_execution"].reason == "No build manifest or build script declared"

    # Verify test_execution is skipped
    assert "test_execution" in checks_by_name
    assert checks_by_name["test_execution"].status == "skipped"
    assert checks_by_name["test_execution"].reason == "No test files found"

    # Verify route_consistency is skipped
    assert "route_consistency" in checks_by_name
    assert checks_by_name["route_consistency"].status == "skipped"
    assert checks_by_name["route_consistency"].reason == "No route pair detected in workspace"

    # Verify evidence captures the skipped checks list
    skipped = decision.evidence.get("skipped_checks", [])
    skipped_names = {s["name"] for s in skipped}
    assert "build_execution" in skipped_names
    assert "test_execution" in skipped_names
    assert "route_consistency" in skipped_names


def test_exhausted_integration_retries_fail_closed_to_blocked(tmp_path: Path) -> None:
    """Requirement 7: When integration rework rounds are exhausted, the supervisor
    fails closed to Phase.BLOCKED rather than approving by default."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    src_file = tmp_path / "src" / "broken.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def broken(): return 0\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Broken component", "codex", "src/broken.py")

    test_file = tmp_path / "tests" / "test_broken.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_broken(): assert False\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "QA test", "gemma", "tests/test_broken.py")

    state = supervisor.start_run("run-exhausted", "Exhausted retries", tmp_path, "sess-exh")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002"]
    state.max_retries = 2
    state.integration_rework_rounds = 2  # Already exhausted!

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Completed claim",
        "blockers": [],
        "commands_audit": [
            {"command": "pytest tests/test_broken.py", "returncode": 1},
        ],
    }

    res = supervisor.advance(state)
    assert res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "Integration review blocked" in (state.error or "")


def test_telemetry_scoped_to_valid_operations_and_supersedes_historical_failures(tmp_path: Path) -> None:
    """Finding 1: Telemetry is scoped to valid operations, excluding ignored_operation_ids.
    Historical failed operations do not poison successful rework, but current failures block."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    src_file = tmp_path / "src" / "calc.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def mul(a, b): return a * b\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Calc", "codex", "src/calc.py")

    test_unit = tmp_path / "tests" / "test_unit.py"
    test_unit.parent.mkdir(parents=True, exist_ok=True)
    test_unit.write_text("def test_mul(): assert True\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "Unit test", "gemma", "tests/test_unit.py")

    test_integ = tmp_path / "tests" / "test_integ.py"
    test_integ.write_text("def test_all(): assert True\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-003", "Integration test", "gemma", "tests/test_integ.py")

    state = supervisor.start_run("run-scope-1", "Calc App", tmp_path, "sess-scope-1")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    # Simulate historical failed operation that was superseded and marked ignored
    hist_op_id = "op-hist-failed-1"
    mock_mgr.operations[hist_op_id] = {
        "operation_id": hist_op_id,
        "session_id": state.session_id,
        "status": "COMPLETED",
        "result": {
            "commands_audit": [
                {"command": "pytest tests/test_unit.py", "returncode": 1},
            ]
        },
    }
    state.ignored_operation_ids = [hist_op_id]

    # Current successful operations for unit and integration suites
    curr_op_1 = "op-curr-unit-1"
    mock_mgr.operations[curr_op_1] = {
        "operation_id": curr_op_1,
        "session_id": state.session_id,
        "status": "COMPLETED",
        "result": {
            "commands_audit": [
                {"command": "pytest tests/test_unit.py", "returncode": 0},
            ]
        },
    }
    curr_op_2 = "op-curr-integ-1"
    mock_mgr.operations[curr_op_2] = {
        "operation_id": curr_op_2,
        "session_id": state.session_id,
        "status": "COMPLETED",
        "result": {
            "commands_audit": [
                {"command": "pytest tests/test_integ.py", "returncode": 0},
            ]
        },
    }

    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Integration review verified all deliverables and tests.",
        "blockers": [],
    }

    # Historical failure does not poison; distinct suites pass -> FINAL_REVIEW
    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW

    # Now verify that if one current suite fails, it remains blocking
    state.phase = Phase.INTEGRATION_REVIEW
    state.integration_operation_id = None
    mock_mgr.operations[curr_op_2]["result"] = {
        "commands_audit": [
            {"command": "pytest tests/test_integ.py", "returncode": 1},
        ]
    }
    supervisor.advance(state)
    rev_op_id_2 = state.integration_operation_id
    mock_mgr.operations[rev_op_id_2]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id_2]["result"] = {
        "status": "completed",
        "summary": "Attempt with current failure.",
        "blockers": [],
    }
    res_fail = supervisor.advance(state)
    assert res_fail == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING  # routed to rework because of current failure


def test_anchored_test_and_build_command_parser_positive_and_negative() -> None:
    """Finding 2: Anchored command token parser accurately differentiates real test/build
    runners from non-execution commands containing runner substrings."""
    gate = IntegrationGate(Path("."))

    # Positive test commands
    positive_tests = [
        "pytest",
        "pytest tests/test_app.py",
        "CI=1 pytest -v",
        "python -m pytest",
        "python3 -m unittest discover",
        "npm test",
        "pnpm run test:unit",
        "yarn test",
        "bun test",
        "npx jest --coverage",
        "npx vitest run",
        "cargo test",
        "go test ./...",
        "dotnet test",
        "powershell -Command 'pytest tests/'",
        "cmd.exe /c pytest",
    ]
    for cmd in positive_tests:
        tokens = gate._parse_command_tokens(cmd)
        assert gate._is_test_command(tokens), f"Expected '{cmd}' to be recognized as test command"

    # Negative test commands (substrings that must NOT be treated as test execution)
    negative_tests = [
        "cat pytest.ini",
        "echo 'pytest passed successfully'",
        "git log -- jest",
        "touch pytest.log",
        "grep -r 'pytest' src/",
        "vim test_something.py",
        "python script.py",
        "npm install",
        "ls tests/",
    ]
    for cmd in negative_tests:
        tokens = gate._parse_command_tokens(cmd)
        assert not gate._is_test_command(tokens), f"Expected '{cmd}' NOT to be recognized as test command"

    # Positive build commands
    positive_builds = [
        "npm run build",
        "pnpm build",
        "yarn build",
        "bun run build",
        "npx vite build",
        "npx webpack --mode production",
        "tsc -b",
        "make build",
        "make all",
        "cargo build --release",
        "python -m build",
    ]
    for cmd in positive_builds:
        tokens = gate._parse_command_tokens(cmd)
        assert gate._is_build_command(tokens), f"Expected '{cmd}' to be recognized as build command"

    # Negative build commands
    negative_builds = [
        "cat Makefile",
        "echo 'npm run build'",
        "touch build.log",
        "npm test",
        "git status",
    ]
    for cmd in negative_builds:
        tokens = gate._parse_command_tokens(cmd)
        assert not gate._is_build_command(tokens), f"Expected '{cmd}' NOT to be recognized as build command"


def test_route_method_mismatch_blocks_approval(tmp_path: Path) -> None:
    """Finding 3: Route consistency verifies HTTP method in addition to path.
    A matching path with mismatched HTTP method produces a specific diagnostic and blocks."""
    gate = IntegrationGate(tmp_path)

    # Backend declares POST /api/items only
    be_file = tmp_path / "app.py"
    be_file.write_text(
        "from flask import Flask\napp = Flask(__name__)\n\n"
        "@app.route('/api/items', methods=['POST'])\ndef create_item(): return {}\n",
        encoding="utf-8",
    )

    # Frontend calls DELETE /api/items
    fe_file = tmp_path / "client.js"
    fe_file.write_text("axios.delete('/api/items');\n", encoding="utf-8")

    check, issues, blockers, evidence = gate._check_route_api_consistency()
    assert check.passed is False
    assert check.status == "failed"
    assert any("Route method mismatch" in issue for issue in issues)
    assert any("calls DELETE '/api/items'" in issue for issue in issues)
    assert any("only supports ['POST']" in issue for issue in issues)

    # Fix frontend to call POST /api/items
    fe_file.write_text("axios.post('/api/items', {name: 'item1'});\n", encoding="utf-8")
    check_pass, issues_pass, _, _ = gate._check_route_api_consistency()
    assert check_pass.passed is True
    assert check_pass.status == "passed"
    assert not issues_pass

    # Generic method wildcard in backend (e.g. Flask-RESTful resource) allows any frontend method
    be_file.write_text(
        "from flask import Flask\nfrom flask_restful import Api, Resource\n"
        "app = Flask(__name__)\napi = Api(app)\n"
        "class ItemRes(Resource): pass\n"
        "api.add_resource(ItemRes, '/api/items')\n",
        encoding="utf-8",
    )
    fe_file.write_text("fetch('/api/items', { method: 'DELETE' });\n", encoding="utf-8")
    check_wild, issues_wild, _, _ = gate._check_route_api_consistency()
    assert check_wild.passed is True
    assert check_wild.status == "passed"
    assert not issues_wild


def test_clean_transition_clears_integration_blockers(tmp_path: Path) -> None:
    """Finding 4: Retain blockers while blocked/reworking, and clear state.integration_blockers
    to [] upon successful transition to Phase.FINAL_REVIEW."""
    _setup_base_workspace(tmp_path)
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    src_file = tmp_path / "src" / "service.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def serve(): return True\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-001", "Service", "codex", "src/service.py")

    test_file = tmp_path / "tests" / "test_service.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_service(): assert True\n", encoding="utf-8")
    _create_completed_wo(tmp_path, "WO-002", "Tests", "gemma", "tests/test_service.py")

    state = supervisor.start_run("run-clear-blockers", "Clear Blockers App", tmp_path, "sess-cb-1")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    # 1. First review fails because test execution failed
    supervisor.advance(state)
    rev_op_id = state.integration_operation_id
    mock_mgr.operations[rev_op_id]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id]["result"] = {
        "status": "completed",
        "summary": "Initial attempt",
        "blockers": [],
        "commands_audit": [
            {"command": "pytest tests/test_service.py", "returncode": 1},
        ],
    }

    supervisor.advance(state)
    # Blockers must be populated and retained during rework
    assert len(state.integration_blockers) > 0
    assert any("Deterministic test failure" in b for b in state.integration_blockers)

    # 2. Complete rework with passing test execution
    state.phase = Phase.INTEGRATION_REVIEW
    state.integration_operation_id = None
    supervisor.advance(state)
    rev_op_id_2 = state.integration_operation_id
    mock_mgr.operations[rev_op_id_2]["status"] = "COMPLETED"
    mock_mgr.operations[rev_op_id_2]["result"] = {
        "status": "completed",
        "summary": "Fixed attempt",
        "blockers": [],
        "commands_audit": [
            {"command": "pytest tests/test_service.py", "returncode": 0},
        ],
    }

    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    # Obsolete blockers MUST be cleared
    assert state.integration_blockers == []

