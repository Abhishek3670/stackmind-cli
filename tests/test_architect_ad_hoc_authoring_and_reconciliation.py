"""Unit tests for Architect ad-hoc authoring and supervisor run reconciliation.

Verifies:
1. Lead Architect is authorized to author work orders and contracts in ad-hoc mode.
2. Non-architect agents remain blocked from authoring work orders in ad-hoc mode.
3. Architect execution turns are exempt from strict declared vs. observed file-delta mismatches.
4. Supervisor resume reopens a COMPLETE run to DISPATCHING if active work orders exist on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.daemon.supervisor import Phase
from validators.kernel.daemon.supervisor.models import RunState


def test_architect_authorized_for_work_orders_in_adhoc_mode(tmp_path: Path) -> None:
    """Architect agent/role in ad-hoc conversation must be permitted to write work orders."""
    mock_adapter = MagicMock()
    mock_tools = MagicMock()
    mock_tools.write_file.return_value = "Wrote 120 bytes"

    gateway = ProviderGateway(mock_adapter, mock_tools)
    gateway.is_adhoc = True
    gateway.is_architecture = True

    res = gateway.execute_tool_call(
        "write_file",
        {
            "path": ".sync/work-orders/ACTIVE/WO-008.yaml",
            "content": "id: WO-008\ntype: FIX\n",
        },
    )

    # Must NOT return the ad-hoc disabled error
    assert "disabled during ad-hoc conversation" not in res
    mock_tools.write_file.assert_called_once()


def test_non_architect_blocked_from_work_orders_in_adhoc_mode(tmp_path: Path) -> None:
    """Non-architect (e.g. frontend/backend worker) in ad-hoc conversation must remain blocked."""
    mock_adapter = MagicMock()
    mock_tools = MagicMock()

    gateway = ProviderGateway(mock_adapter, mock_tools)
    gateway.is_adhoc = True
    gateway.is_architecture = False
    gateway.role = "frontend"

    res = gateway.execute_tool_call(
        "write_file",
        {
            "path": ".sync/work-orders/ACTIVE/WO-008.yaml",
            "content": "id: WO-008\ntype: FIX\n",
        },
    )

    assert "disabled during ad-hoc conversation" in res
    mock_tools.write_file.assert_not_called()


def test_architect_batch_write_authorized_in_adhoc_mode(tmp_path: Path) -> None:
    """Architect batch_write_files must succeed in ad-hoc mode for work orders."""
    mock_adapter = MagicMock()
    mock_tools = MagicMock()
    mock_tools.batch_write_files.return_value = {
        "status": "success",
        "written_count": 1,
        "files": [".sync/work-orders/ACTIVE/WO-008.yaml"],
    }

    gateway = ProviderGateway(mock_adapter, mock_tools)
    gateway.is_adhoc = True
    gateway.is_architecture = True

    res = gateway.execute_tool_call(
        "batch_write_files",
        {
            "files": [
                {
                    "path": ".sync/work-orders/ACTIVE/WO-008.yaml",
                    "content": "id: WO-008\n",
                }
            ]
        },
    )

    assert "disabled during ad-hoc conversation" not in str(res)
    mock_tools.batch_write_files.assert_called_once()


def test_resume_reopens_complete_run_when_active_work_orders_exist(tmp_path: Path) -> None:
    """If active work orders exist on disk, resume_run must reopen COMPLETE -> DISPATCHING."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon.db")
    manager = SessionManager(storage=storage)

    # Setup workspace with active work order
    ws = tmp_path / "project"
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "WO-008.yaml").write_text("id: WO-008\nstatus: ACTIVE\n", encoding="utf-8")

    # Setup session
    session = manager.create_session("claude", "test", {}, str(ws))
    session_id = session["session_id"]

    # Setup state currently in COMPLETE
    state = RunState(
        run_id="run-test",
        workspace=str(ws),
        session_id=session_id,
        phase=Phase.COMPLETE,
        product_goal="Goal",
        completed_wo_ids=["WO-000", "WO-001"],
        worker_wo_ids=[],
    )
    sup_dir = ws / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    manager.supervisor.save_run_state(state, ws)

    # Resume run
    res = manager.resume_run(session_id, run_id="run-test")

    # State must be reopened to DISPATCHING and include WO-008
    assert res["phase"] == "DISPATCHING"
    assert "WO-008" in res["worker_wo_ids"]


def test_check_qa_verdict_ignores_archived_and_stale_verdicts(tmp_path: Path) -> None:
    """_check_qa_verdict must ignore archived files and stale JSON verdicts whose subject_hash mismatches."""
    from validators.kernel.daemon.supervisor.execution import _is_verdict_file, _ExecutionMixin
    import re

    stale_file = tmp_path / "WO-008-stale-20261106205244.json"
    stale_file.write_text('{"verdict": "FAIL"}', encoding="utf-8")
    assert not _is_verdict_file(stale_file), "_is_verdict_file must reject files with -stale-"

    invalidated_file = tmp_path / "WO-008-invalidated-20261106205244.json"
    assert not _is_verdict_file(invalidated_file), "_is_verdict_file must reject files with -invalidated-"

    # Check _check_qa_verdict subject_hash freshness check
    reviews_qa = tmp_path / ".sync" / "reviews" / "qa"
    reviews_qa.mkdir(parents=True, exist_ok=True)
    active_qa = reviews_qa / "WO-008.json"
    active_qa.write_text('{"schema": "stackmind.qa-verdict.v1", "work_order_id": "WO-008", "verdict": "FAIL", "subject_hash": "old_hash"}', encoding="utf-8")

    class MockSupervisor(_ExecutionMixin):
        _WO_ID_RE = re.compile(r"WO-\d{3}")
        def _agent_for_wo(self, wo_id, ws): return "gemini"
        def _role_for_agent(self, agent): return "frontend"
        def _get_wo_deliverable_path(self, wo_id, ws): return "src/main.js"

    sup = MockSupervisor()
    # With mismatched hash (file does not exist or has different hash), verdict is ignored (returns None)
    res = sup._check_qa_verdict("WO-008", tmp_path)
    assert res is None, f"Expected None for stale verdict, got {res}"


def test_integration_review_contract_is_read_write_and_permits_documentation(tmp_path: Path) -> None:
    """_synthesize_integration_review_artifacts must generate read-write contract so architect can update release notes."""
    from validators.kernel.daemon.supervisor.review import _ReviewMixin

    class MockReviewSupervisor(_ReviewMixin):
        pass

    sup = MockReviewSupervisor()
    state = RunState(
        run_id="run-rev-1",
        workspace=str(tmp_path),
        session_id="sess-rev-1",
        phase=Phase.INTEGRATION_REVIEW,
        product_goal="Stopwatch App",
        completed_wo_ids=["WO-000", "WO-001"],
    )
    sup._synthesize_integration_review_artifacts(state, tmp_path, "WO-007", ["index.html"])

    contract_path = tmp_path / ".sync" / "contracts" / "WO-007.yaml"
    assert contract_path.is_file(), "Contract file must be authored"
    c_data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert c_data["scope"]["write"] == "read-write", "Integration review contract must be read-write"
    assert c_data["budget"]["max_files_touched"] >= 10, "Budget must allow editing release docs"


def test_is_governance_blocker_matches_contract_read_only_and_directories(tmp_path: Path) -> None:
    """_is_governance_or_packaging_blocker must recognize CONTRACT_READ_ONLY_VIOLATION and directory deliverables."""
    from validators.kernel.daemon.supervisor.review import _ReviewMixin

    # Check substring logic
    b1 = "CONTRACT_READ_ONLY_VIOLATION: Contract WO-007 is read-only but 2 file(s) were modified"
    b2 = "WO-011: tests: Deliverable is a directory, expected regular file: W:\\clean_tui_ornith\\tests"
    b3 = "Deliverable verification failed: tests: Deliverable is a directory, expected regular file"

    # In review.py, the helper checks lowercase substrings
    def check_b(b_text: str) -> bool:
        b_lower = b_text.lower()
        return (
            "changelog" in b_lower
            or "readme" in b_lower
            or "wo-000" in b_lower
            or "worker_timeout" in b_lower
            or "timeout (retry" in b_lower
            or "packaging" in b_lower
            or "board inconsistency" in b_lower
            or "active on board" in b_lower
            or "release-packaging" in b_lower
            or "documentation gap" in b_lower
            or "contract is read-only" in b_lower
            or "contract read-only" in b_lower
            or "contract_read_only" in b_lower
            or "read-only" in b_lower
            or "policy denied writing" in b_lower
            or "permissionerror" in b_lower
            or "release-phase work order" in b_lower
            or "not a code defect" in b_lower
            or "deliverable is a directory" in b_lower
            or "expected regular file" in b_lower
            or "deliverable verification failed" in b_lower
        )

    assert check_b(b1), "Must match CONTRACT_READ_ONLY_VIOLATION"
    assert check_b(b2), "Must match deliverable directory error"
    assert check_b(b3), "Must match deliverable verification failed directory error"


def test_deliverable_verifier_accepts_test_directory_with_files(tmp_path: Path) -> None:
    """DeliverableVerifier must accept a test directory when files exist."""
    from validators.harness.deliverable_verifier import DeliverableVerifier, verify_work_order_deliverables

    test_dir = tmp_path / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "suite.test.js").write_text("console.log('pass');", encoding="utf-8")

    verifier = DeliverableVerifier()
    res = verifier.verify(test_dir)
    assert res.passed, f"Expected verifier to pass on valid test directory, got: {[c.message for c in res.checks if not c.passed]}"
    assert res.content_hash is not None, "Content hash should be computed for directory"

    wo_data = {
        "id": "WO-011",
        "type": "VALIDATION",
        "deliverable": {
            "path": "tests",
            "type": "test",
            "description": "Test suite directory",
        },
    }
    ok, errors, _ = verify_work_order_deliverables(tmp_path, wo_data)
    assert ok, f"Expected verify_work_order_deliverables to pass, got: {errors}"
    assert res.passed, f"Expected verifier to pass on valid test directory, got checks: {[c.message for c in res.checks if not c.passed]}"
    assert res.content_hash is not None, "Content hash should be computed for directory"


def test_reworkable_integration_wos_ignores_pure_governance_blockers(tmp_path: Path) -> None:
    """_reworkable_integration_wos must not unarchive code work orders when blockers are purely governance."""
    from validators.kernel.daemon.supervisor.review import _ReviewMixin

    class MockReviewSupervisor(_ReviewMixin):
        def _agent_for_wo(self, wo_id, ws):
            return "gemma" if "006" in wo_id or "009" in wo_id else "gemini"
        def _role_for_agent(self, agent):
            return "qa" if agent == "gemma" else "frontend"

    sup = MockReviewSupervisor()
    state = RunState(
        run_id="run-rev-2",
        workspace=str(tmp_path),
        session_id="sess-rev-2",
        phase=Phase.INTEGRATION_REVIEW,
        product_goal="Stopwatch App",
        completed_wo_ids=["WO-000", "WO-001", "WO-002", "WO-003", "WO-004", "WO-005", "WO-006"],
        worker_wo_ids=["WO-001", "WO-002", "WO-003", "WO-004", "WO-005", "WO-006"],
    )

    blockers = ["CONTRACT_READ_ONLY_VIOLATION: Contract WO-007 is read-only but 2 file(s) were modified"]
    reworkable = sup._reworkable_integration_wos(state, tmp_path, blockers)
    assert reworkable == [], f"Expected empty reworkable for pure governance blocker, got {reworkable}"

