"""Phase 3 Regression Tests: Artifact-Based Completion Verification (SDP-FIX-001).

10 Required Scenarios:
1. An uncreated deliverable is rejected even if claimed by worker.
2. A 0-byte or whitespace-only deliverable is rejected.
3. A directory matching the deliverable path is rejected.
4. A malformed deliverable (e.g. invalid JSON, syntax-broken code) is rejected when format verification applies.
5. In multi-file deliverables, missing one file causes verification to fail.
6. A valid deliverable passes verification.
7. Verification failures increment retry / rework counters according to existing policies without infinite loops.
8. Unverified predecessor prevents downstream dependent work orders from executing.
9. Out-of-scope writes performed alongside a valid deliverable are rejected by scope enforcement.
10. The verification decision includes inspectable evidence (checks run, outcomes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest
import yaml

from validators.harness.deliverable_verifier import (
    DeliverableVerifier,
    check_deliverable_path_containment,
    verify_work_order_deliverables,
)
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    RunState,
    Phase,
)
from tests import review_helpers
from tests.test_lifecycle_supervisor import MockSessionManager


def _write_contract(ws: Path, record: dict[str, Any]) -> Path:
    p = ws / ".sync" / "contracts" / f"{record['work_order']}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return p


def _make_contract(wo_id: str, agent: str = "codex") -> dict[str, Any]:
    return {
        "work_order": wo_id,
        "agent": agent,
        "scope": {"allow": [{"path": "src/**"}, {"path": "tests/**"}], "deny": []},
        "budget": {"max_files_touched": 5, "max_tokens": 10000},
    }


def _write_wo(ws: Path, record: dict[str, Any], sub: str = "ACTIVE") -> Path:
    p = ws / ".sync" / "work-orders" / sub / f"{record['id']}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return p


def _make_wo(
    wo_id: str,
    agent: str = "codex",
    deliv_path: str = "src/app.py",
    deliv_type: str = "code",
    extra_deliverables: list[dict[str, Any]] | None = None,
    deps: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": f"Implement {wo_id}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": list(deps or []),
        "deliverable": {
            "type": deliv_type,
            "description": f"{wo_id} deliverable",
            "path": deliv_path,
        },
        "description": f"Work order {wo_id}",
    }
    if extra_deliverables:
        record["deliverables"] = list(extra_deliverables)
    return record


# ─── 1. Uncreated deliverable rejected ────────────────────────────────────────

def test_uncreated_deliverable_rejected_even_if_claimed_by_worker(tmp_path: Path):
    """An uncreated deliverable is rejected even if claimed by worker."""
    wo = _make_wo("WO-001", deliv_path="src/missing.py")
    _write_wo(tmp_path, wo)

    # Worker claims done, but src/missing.py does not exist on disk
    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert evidence["verified"] is False
    assert evidence["failed_count"] == 1
    assert any("does not exist" in err.lower() for err in errors)


# ─── 2. 0-byte or whitespace-only deliverable rejected ────────────────────────

def test_zero_byte_deliverable_rejected(tmp_path: Path):
    """A 0-byte deliverable is rejected."""
    wo = _make_wo("WO-001", deliv_path="src/empty.json", deliv_type="code")
    _write_wo(tmp_path, wo)

    target = tmp_path / "src" / "empty.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert evidence["verified"] is False
    assert any("empty or contains only whitespace" in err.lower() for err in errors)


def test_whitespace_only_deliverable_rejected(tmp_path: Path):
    """A whitespace-only deliverable is rejected."""
    wo = _make_wo("WO-001", deliv_path="src/whitespace.py", deliv_type="code")
    _write_wo(tmp_path, wo)

    target = tmp_path / "src" / "whitespace.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("   \n\t  \n  ", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert evidence["verified"] is False
    assert any("empty or contains only whitespace" in err.lower() for err in errors)


# ─── 3. Directory matching deliverable path rejected ─────────────────────────

def test_directory_matching_deliverable_path_rejected(tmp_path: Path):
    """A directory matching the deliverable path is rejected."""
    wo = _make_wo("WO-001", deliv_path="src/component.py", deliv_type="code")
    _write_wo(tmp_path, wo)

    # Create a directory instead of a regular file
    target_dir = tmp_path / "src" / "component.py"
    target_dir.mkdir(parents=True, exist_ok=True)

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert evidence["verified"] is False
    assert any("directory" in err.lower() for err in errors)


# ─── 4. Malformed deliverable rejected ────────────────────────────────────────

def test_malformed_json_deliverable_rejected(tmp_path: Path):
    """Malformed JSON deliverable fails verification."""
    wo = _make_wo("WO-001", deliv_path="config/settings.json")
    _write_wo(tmp_path, wo)

    target = tmp_path / "config" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ invalid json: [ missing close }", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert any("invalid json" in err.lower() for err in errors)


def test_malformed_python_deliverable_rejected(tmp_path: Path):
    """Malformed Python code deliverable fails syntax verification."""
    wo = _make_wo("WO-001", deliv_path="src/module.py")
    _write_wo(tmp_path, wo)

    target = tmp_path / "src" / "module.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def broken_syntax(:\n    pass\n", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert any("python syntax error" in err.lower() for err in errors)


def test_malformed_yaml_deliverable_rejected(tmp_path: Path):
    """Malformed YAML deliverable fails verification."""
    wo = _make_wo("WO-001", deliv_path="config/manifest.yaml")
    _write_wo(tmp_path, wo)

    target = tmp_path / "config" / "manifest.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("key: [unclosed list\n", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert any("yaml" in err.lower() for err in errors)


def test_unresolved_placeholder_rejected(tmp_path: Path):
    """Unresolved placeholder like TODO or FIXME in code is rejected."""
    wo = _make_wo("WO-001", deliv_path="src/logic.py")
    _write_wo(tmp_path, wo)

    target = tmp_path / "src" / "logic.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def compute():\n    # TODO: implement compute\n    pass\n", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert any("placeholder" in err.lower() for err in errors)


# ─── 5. Multi-file completeness ───────────────────────────────────────────────

def test_multi_file_deliverables_missing_one_fails(tmp_path: Path):
    """In multi-file deliverables, missing even one file causes verification to fail."""
    wo = _make_wo(
        "WO-001",
        deliv_path="src/index.html",
        extra_deliverables=[
            {"path": "src/style.css"},
            {"path": "src/app.js"},
        ],
    )
    _write_wo(tmp_path, wo)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Author index.html and style.css, but NOT app.js
    (src_dir / "index.html").write_text("<!DOCTYPE html><html><body><h1>App</h1></body></html>", encoding="utf-8")
    (src_dir / "style.css").write_text("body { margin: 0; }", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert evidence["deliverables_count"] == 3
    assert evidence["passed_count"] == 2
    assert evidence["failed_count"] == 1
    assert any("app.js" in err for err in errors)

    # Now author app.js with valid code
    (src_dir / "app.js").write_text("console.log('App started');\n", encoding="utf-8")

    passed2, errors2, evidence2 = verify_work_order_deliverables(tmp_path, wo)

    assert passed2 is True
    assert evidence2["deliverables_count"] == 3
    assert evidence2["passed_count"] == 3
    assert evidence2["failed_count"] == 0
    assert len(errors2) == 0


# ─── 6. Valid deliverable passes ──────────────────────────────────────────────

def test_valid_deliverable_passes_verification(tmp_path: Path):
    """A valid, non-empty, syntactically correct deliverable passes verification."""
    wo = _make_wo("WO-001", deliv_path="src/math_utils.py")
    _write_wo(tmp_path, wo)

    target = tmp_path / "src" / "math_utils.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is True
    assert len(errors) == 0
    assert evidence["verified"] is True
    assert evidence["passed_count"] == 1
    assert evidence["failed_count"] == 0


# ─── 7. Verification failures increment retry counters ────────────────────────

def test_verification_failures_increment_retry_counters_without_infinite_loops(tmp_path: Path):
    """Verification failures increment retry / rework counters according to existing policies without infinite loops."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-test-retry", "Test product", tmp_path, "sess-test")
    state.phase = Phase.EXECUTING
    state.max_attempts = 2
    state.max_retries = 2

    wo_id = "WO-001"
    wo = _make_wo(wo_id, deliv_path="src/unwritten.py")
    _write_wo(tmp_path, wo)

    # Initially: attempts = 0, retries = 0
    assert state.attempt_counts.get(wo_id, 0) == 0
    assert state.retry_counts.get(wo_id, 0) == 0

    # Deliverable does not exist on disk
    deliv_passed, deliv_err, _ = supervisor._verify_work_order_deliverables(wo_id, tmp_path)
    assert deliv_passed is False

    # Attempt 1
    dispatched1 = supervisor._retry_worker(state, tmp_path, wo_id, lambda n, a: f"Retry attempt {n}")
    assert dispatched1 is True
    assert state.attempt_counts[wo_id] == 1
    assert state.retry_counts.get(wo_id, 0) == 0

    # Attempt 2 (reaches max_attempts, increments retries to 1)
    dispatched2 = supervisor._retry_worker(state, tmp_path, wo_id, lambda n, a: f"Retry attempt {n}")
    assert dispatched2 is True
    assert state.attempt_counts[wo_id] == 2
    assert state.retry_counts[wo_id] == 1

    # Attempt 3 (attempt resets to 1, retries increments to 2 >= max_retries: exhausted!)
    dispatched3 = supervisor._retry_worker(state, tmp_path, wo_id, lambda n, a: f"Retry attempt {n}")
    assert dispatched3 is False  # Budget exhausted!
    assert wo_id in state.failed_wo_ids


# ─── 8. Unverified predecessor blocks downstream dependents ───────────────────

def test_unverified_predecessor_prevents_downstream_execution(tmp_path: Path):
    """Unverified predecessor prevents downstream dependent work orders from executing."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-test-deps", "Test product", tmp_path, "sess-deps")
    state.phase = Phase.EXECUTING

    wo1 = _make_wo("WO-001", deliv_path="src/core.py")
    wo2 = _make_wo("WO-002", deliv_path="src/feature.py", deps=["WO-001"])
    _write_wo(tmp_path, wo1)
    _write_wo(tmp_path, wo2)

    # Deliverable for WO-001 is missing or invalid
    assert supervisor._check_deliverable_exists("WO-001", tmp_path) is False

    # Because WO-001 has not completed verification, it is NOT in completed_wo_ids
    assert "WO-001" not in state.completed_wo_ids

    # WO-002 dependency check must fail closed
    assert supervisor._dependencies_met(state, "WO-002", tmp_path) is False

    # Now verify WO-001 succeeds once its deliverable is authored properly
    core_file = tmp_path / "src" / "core.py"
    core_file.parent.mkdir(parents=True, exist_ok=True)
    core_file.write_text("def init_core(): return True\n", encoding="utf-8")

    assert supervisor._check_deliverable_exists("WO-001", tmp_path) is True

    # When supervisor marks WO-001 completed
    state.completed_wo_ids.append("WO-001")

    # Now WO-002 dependencies are satisfied
    assert supervisor._dependencies_met(state, "WO-002", tmp_path) is True


# ─── 9. Out-of-scope writes rejected alongside valid deliverable ──────────────

def test_out_of_scope_writes_alongside_valid_deliverable_rejected(tmp_path: Path):
    """Out-of-scope writes performed alongside a valid deliverable are rejected by scope enforcement."""
    from validators.harness.contract_gate import verify_post_execution
    from validators.harness.runner import HarnessTask, HarnessDecision
    from validators.kernel.daemon.authoring import build_child_contract
    from validators.knowledge.contract import ContractAccessDenied

    task = HarnessTask(
        kind="work_order",
        identifier="WO-001",
        path=tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml",
        title="Scoped task",
        body="Implement scoped task",
        query="Scoped task",
        work_order_id="WO-001",
        deliverable_path="src/valid.py",
    )

    # Valid deliverable written, but worker also wrote to unauthorized path (.env is denied)
    allowed_file = "src/valid.py"
    unauthorized_file = ".env"

    decision = HarnessDecision(
        status="completed",
        summary="Done with work",
        report_markdown="Completed scoped task",
        blockers=(),
        release_target="src/valid.py",
        retrieval_queries=(),
        uncertainty=(),
        modified_files=(allowed_file, unauthorized_file),
    )

    # Build schema-valid contract allowing src/valid.py and denying .env
    contract_data = build_child_contract("WO-001", "codex", "developer", deliverable_path="src/valid.py")
    contract_p = tmp_path / ".sync" / "contracts" / "WO-001.yaml"
    contract_p.parent.mkdir(parents=True, exist_ok=True)
    contract_p.write_text(yaml.safe_dump(contract_data), encoding="utf-8")

    # Verify that verify_post_execution rejects the out-of-scope write
    with pytest.raises(ContractAccessDenied) as exc_info:
        verify_post_execution(
            tmp_path,
            "codex",
            task,
            decision,
            observed_files=(allowed_file, unauthorized_file),
        )

    assert ".env" in str(exc_info.value) or "denied" in str(exc_info.value).lower()


# ─── 10. Verification decision includes inspectable evidence ──────────────────

def test_verification_decision_includes_inspectable_evidence(tmp_path: Path):
    """The verification decision includes inspectable evidence (checks run, outcomes)."""
    wo = _make_wo(
        "WO-001",
        deliv_path="src/service.py",
        extra_deliverables=[{"path": "config/schema.json"}],
    )
    _write_wo(tmp_path, wo)

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "service.py").write_text("class Service:\n    def start(self): pass\n", encoding="utf-8")

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # Write invalid JSON to check failure evidence reporting
    (cfg_dir / "schema.json").write_text("{ malformed: json }", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)

    assert passed is False
    assert "verified" in evidence
    assert evidence["verified"] is False
    assert "declared_paths" in evidence
    assert evidence["declared_paths"] == ["src/service.py", "config/schema.json"]
    assert evidence["deliverables_count"] == 2
    assert evidence["passed_count"] == 1
    assert evidence["failed_count"] == 1
    assert "results" in evidence
    assert len(evidence["results"]) == 2

    # First result (service.py) passed
    r0 = evidence["results"][0]
    assert r0["relative_path"] == "src/service.py"
    assert r0["passed"] is True
    assert r0["content_hash"] is not None
    assert any(c["name"] == "python_syntax" and c["passed"] for c in r0["checks"])

    # Second result (schema.json) failed
    r1 = evidence["results"][1]
    assert r1["relative_path"] == "config/schema.json"
    assert r1["passed"] is False
    assert any(c["name"] == "json_syntax" and not c["passed"] for c in r1["checks"])

    # Timestamp present
    assert "timestamp" in evidence


# ─── 11. Path Containment Enforcements ────────────────────────────────────────

def test_traversal_paths_rejected(tmp_path: Path):
    """Paths containing '..' or nested traversal are rejected."""
    wo1 = _make_wo("WO-001", deliv_path="../secret.py")
    passed1, errors1, evidence1 = verify_work_order_deliverables(tmp_path, wo1)
    assert passed1 is False
    assert any("traversal" in err.lower() for err in errors1)

    wo2 = _make_wo("WO-002", deliv_path="src/../../outside.py")
    passed2, errors2, evidence2 = verify_work_order_deliverables(tmp_path, wo2)
    assert passed2 is False
    assert any("traversal" in err.lower() for err in errors2)


def test_absolute_posix_paths_rejected(tmp_path: Path):
    """Absolute POSIX paths are rejected."""
    wo = _make_wo("WO-001", deliv_path="/etc/passwd")
    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)
    assert passed is False
    assert any("absolute" in err.lower() for err in errors)


def test_windows_drive_paths_rejected(tmp_path: Path):
    """Windows drive paths are rejected cross-platform."""
    wo = _make_wo("WO-001", deliv_path="C:/Windows/System32/drivers/etc/hosts")
    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)
    assert passed is False
    assert any("windows drive" in err.lower() for err in errors)


def test_unc_paths_rejected(tmp_path: Path):
    """UNC paths are rejected."""
    wo1 = _make_wo("WO-001", deliv_path="//server/share/file.py")
    passed1, errors1, _ = verify_work_order_deliverables(tmp_path, wo1)
    assert passed1 is False
    assert any("unc" in err.lower() for err in errors1)

    wo2 = _make_wo("WO-002", deliv_path=r"\\server\share\file.py")
    passed2, errors2, _ = verify_work_order_deliverables(tmp_path, wo2)
    assert passed2 is False
    assert any("unc" in err.lower() for err in errors2)


def test_windows_separators_normalized_and_contained(tmp_path: Path):
    """Valid relative paths with Windows backslashes are normalized and verified."""
    wo = _make_wo("WO-001", deliv_path=r"src\nested\app.py")
    src_dir = tmp_path / "src" / "nested"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "app.py").write_text("def run(): pass\n", encoding="utf-8")

    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)
    assert passed is True
    assert errors == []
    assert evidence["declared_paths"] == ["src/nested/app.py"]


def test_symlink_escaping_root_rejected(tmp_path: Path):
    """Symlinks escaping the permitted root are rejected if symlinks are supported."""
    outside_dir = tmp_path.parent / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("SECRET = 42\n", encoding="utf-8")

    link_path = tmp_path / "link_outside.py"
    try:
        link_path.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this environment")

    wo = _make_wo("WO-001", deliv_path="link_outside.py")
    passed, errors, evidence = verify_work_order_deliverables(tmp_path, wo)
    assert passed is False
    assert any("escapes root" in err.lower() or "containment" in err.lower() for err in errors)


def test_staged_artifact_missing_from_staging_fails_without_fallback(tmp_path: Path):
    """Staged verification strictly checks staged_root and never falls back to live workspace."""
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (live_dir / "src").mkdir()
    (live_dir / "src" / "app.py").write_text("def live(): pass\n", encoding="utf-8")

    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    # staged_dir has NO src/app.py

    wo = _make_wo("WO-001", deliv_path="src/app.py")
    passed, errors, evidence = verify_work_order_deliverables(live_dir, wo, staged_root=staged_dir)
    assert passed is False
    assert any("does not exist" in err.lower() for err in errors)


# ─── 12. Blocker Cleanup on Successful Recovery ───────────────────────────────

def test_successful_retry_clears_stale_worker_blocker(tmp_path: Path):
    """A failed deliverable attempt followed by a successful retry clears the stale worker_blocker."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-clean-blocker", "Build app", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.max_retries = 2
    state.max_attempts = 1

    # Write initial work order with deliverable
    _write_wo(tmp_path, _make_wo("WO-001", deliv_path="src/app.py"))
    _write_contract(tmp_path, _make_contract("WO-001"))

    # Turn 1 completes, but src/app.py was NOT written to disk
    op1 = mock_mgr.start_turn("sess-001", "Execute WO-001", work_order_id="WO-001", agent_id="codex")
    mock_mgr.complete_operation(op1["operation_id"], "COMPLETED")

    # Supervisor advances: deliverable verification fails, worker_blocker is set, retry dispatched
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" in state.worker_blockers
    assert state.worker_blockers["WO-001"]["failure_code"] == "DELIVERABLE_VERIFICATION_FAILED"

    # Set an unrelated blocker on WO-999 to verify it is NOT cleared
    state.worker_blockers["WO-999"] = {"failure_code": "UNRELATED_BLOCKER"}

    # Turn 2 (retry): worker now writes the valid deliverable to disk
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("def app():\n    return 'clean'\n", encoding="utf-8")
    # WO-001 produces code, so completion also requires the mandatory QA gate;
    # record the independent QA verdict for this revision (as the QA role does).
    review_helpers.qa_pass(tmp_path, "WO-001")

    retries = [o for o in mock_mgr.list_operations() if o.get("work_order_id") == "WO-001" and o != op1]
    assert len(retries) == 1
    mock_mgr.complete_operation(retries[0]["operation_id"], "COMPLETED")

    # Supervisor advances: deliverable verification succeeds, WO-001 completes
    res2 = supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids
    # Stale blocker for WO-001 is cleared
    assert "WO-001" not in state.worker_blockers
    # Unrelated blocker for WO-999 is preserved
    assert "WO-999" in state.worker_blockers
    # Serialized state contains no blocker for WO-001
    assert "WO-001" not in state.to_dict()["worker_blockers"]
    assert "WO-999" in state.to_dict()["worker_blockers"]

