import pytest
import yaml
from pathlib import Path
from unittest.mock import MagicMock
from validators.harness.authoring_yaml import normalize_work_order_data, validate_artifact_content
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.supervisor.models import OperationContentionError, RunState, Phase
from validators.harness.review_gate import validate_qa_verdict, subject_hash
from validators.harness.authoring_readiness import validate_authoring_readiness


def test_work_order_type_and_deliverable_normalization():
    """Verify that common model aliases ('QA', 'report', 'test') are normalized to schema-valid enums."""
    raw_wo = {
        "id": "WO-012",
        "title": "QA Verification",
        "type": "QA",
        "deliverable": {
            "path": ".sync/reviews/qa/WO-006.json",
            "type": "report",
        },
        "deliverables": [
            {
                "path": "tests/test_foo.py",
                "type": "test",
            }
        ],
    }

    normalized = normalize_work_order_data(raw_wo)
    assert normalized["type"] == "VALIDATION"
    assert normalized["deliverable"]["type"] == "doc"
    assert normalized["deliverables"][0]["type"] == "code"


def test_index_artifact_normalization():
    """Verify that work orders embedded inside INDEX.yaml are normalized during validation."""
    index_yaml = """
schema_version: 1
work_orders:
  - id: WO-012
    title: QA Verification
    type: QA
    status: ACTIVE
    priority: P1
    assigned_agents: [gemma]
    deliverable:
      path: .sync/reviews/qa/WO-006.json
      type: report
    deliverables:
      - path: tests/test_foo.py
        type: test
"""
    result = validate_artifact_content(
        rel_path=".sync/work-orders/INDEX.yaml",
        content=index_yaml,
        agent="claude",
    )
    assert result["passed"] is True
    assert "type: doc" in result["normalized_content"]
    assert "type: code" in result["normalized_content"]
    assert "type: VALIDATION" in result["normalized_content"]


def test_begin_operation_does_not_falsely_orphan_starting_thread(tmp_path: Path):
    """Verify that begin_operation does not kill a newly started operation before the grace period."""
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon.json")
    mgr = SessionManager(storage)
    session = mgr.create_session("claude", "echo-agent", {}, str(tmp_path))
    session_id = session["session_id"]

    # Start operation 1 (no thread registered yet, simulating thread instantiation in flight)
    cancel, op_1_id = mgr.begin_operation(session_id, "turn")
    assert mgr._sessions[session_id]["active_operation"] == op_1_id

    # A concurrent operation attempting to start immediately should raise OperationContentionError,
    # NOT mark op_1_id as FAILED/orphaned.
    with pytest.raises(OperationContentionError):
        mgr.begin_operation(session_id, "turn")

    # Op 1 must still be active and RUNNING
    op_record = mgr.get_operation(op_1_id)
    assert op_record["status"] == "RUNNING"
    assert mgr._sessions[session_id]["active_operation"] == op_1_id


def test_qa_verdict_no_test_evidence_with_test_remediation_passes(tmp_path: Path):
    """Verify that QA verdict with NO_TEST_EVIDENCE passes gate even with advisory test-remediation actions."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    readme = tmp_path / "README.md"
    readme.write_text("# Project README\n\nContent here.", encoding="utf-8")

    wo_data = {
        "id": "WO-010",
        "type": "FEATURE",
        "title": "Author README",
        "status": "ACTIVE",
        "assigned_agents": ["gemini"],
        "deliverable": {
            "type": "doc",
            "path": "README.md",
        },
    }
    (wo_dir / "WO-010.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

    cur_hash = subject_hash(tmp_path, "WO-010")
    verdict = {
        "schema": "stackmind.qa-verdict.v1",
        "work_order_id": "WO-010",
        "revision": 1,
        "reviewer": "gemma",
        "reviewer_role": "qa",
        "timestamp": "2026-11-04T12:00:00Z",
        "verdict": "NO_TEST_EVIDENCE",
        "subject_hash": cur_hash,
        "criteria": [
            {"id": "C1", "description": "README is well-formed", "outcome": "PASS"},
        ],
        "evidence": [
            {"command": "read_file README.md", "kind": "inspection", "passed": True, "result": "OK"},
            {"command": "run_tests", "kind": "test", "passed": False, "result": "exit 5, 0 tests collected"},
        ],
        "findings": [
            {"severity": "minor", "detail": "No automated test/build verification exists for README.md"},
        ],
        "required_actions": [
            "Owning worker should add an automated verification step for README.md (e.g. markdownlint).",
        ],
    }

    ok, msg = validate_qa_verdict(verdict, tmp_path, "WO-010")
    assert ok is True
    assert "provisionally" in msg.lower()


def test_authoring_readiness_allows_review_qa_work_orders(tmp_path: Path):
    """Verify that QA work orders reviewing existing deliverables are allowed without tests/ code deliverable."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    c_dir = tmp_path / ".sync" / "contracts"
    wo_dir.mkdir(parents=True, exist_ok=True)
    c_dir.mkdir(parents=True, exist_ok=True)

    wo_data = {
        "id": "WO-012",
        "type": "VALIDATION",
        "title": "Qa: Perform independent QA verification of WO-006",
        "status": "ACTIVE",
        "assigned_agents": ["gemma"],
        "dependencies": [],
        "deliverable": {
            "type": "doc",
            "path": ".sync/reviews/qa/WO-006.json",
        },
    }
    (wo_dir / "WO-012.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

    contract_data = {
        "schema_version": 1,
        "agent_id": "gemma",
        "work_order": "WO-012",
        "identity": {"role": "qa", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": ".sync/reviews/qa/**"}],
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 100000},
    }
    (c_dir / "WO-012.yaml").write_text(yaml.safe_dump(contract_data), encoding="utf-8")

    res = validate_authoring_readiness(tmp_path)
    qa_errors = [iss for iss in res.issues if iss.code == "QA_DELIVERABLE_NOT_EXECUTABLE"]
    assert not qa_errors


def test_archive_rejected_artifacts_cleans_companion_contract(tmp_path: Path):
    """Verify that archiving a rejected work order unlinks its companion contract to avoid orphan contracts."""
    from validators.kernel.daemon.supervisor.review import _ReviewMixin

    class DummySupervisor(_ReviewMixin):
        pass

    sup = DummySupervisor()
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    c_dir = tmp_path / ".sync" / "contracts"
    wo_dir.mkdir(parents=True, exist_ok=True)
    c_dir.mkdir(parents=True, exist_ok=True)

    wo_file = wo_dir / "WO-014.yaml"
    wo_file.write_text("id: WO-014\n", encoding="utf-8")
    c_file = c_dir / "WO-014.yaml"
    c_file.write_text("work_order: WO-014\n", encoding="utf-8")

    state = MagicMock()
    state.run_id = "run-test"
    state.authoring_revision = 1
    state.readiness_issues = [
        {
            "category": "schema",
            "code": "WORK_ORDER_INVALID",
            "artifact_path": ".sync/work-orders/ACTIVE/WO-014.yaml",
        }
    ]

    archived = sup._archive_rejected_artifacts(state, tmp_path)
    assert not wo_file.exists()
    assert not c_file.exists()
    assert ".sync/work-orders/ACTIVE/WO-014.yaml" in archived
    assert ".sync/contracts/WO-014.yaml" in archived
