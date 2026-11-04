"""Tests for SDP 6-item architectural roadmap:
1. QA_NO_TEST_EVIDENCE and stack-appropriate static verifiers.
2. Full blocker report retention and typed failure codes (never UNKNOWN).
3. Single deliverable ownership in delegate_task (preventing duplicate work orders).
4. Strict architect_fix dependency checks and hash verification.
5. Authoritative harness synthesis of qa_verdict.json.
6. Ephemeral test log filtering under max_files_touched budget.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pytest

from validators.harness import review_gate
from validators.kernel.daemon.supervisor import execution as sup_exec
from validators.kernel.daemon.supervisor import recovery as sup_rec
from validators.kernel.daemon.supervisor.core import RunState, Phase
from validators.kernel.daemon.supervisor import controls as sup_ctrl
from validators.kernel import tools
from validators.harness.contract_validation import verify_post_execution
from validators.knowledge.contract import AgentContract


def test_stack_appropriate_verifier_python(tmp_path: Path) -> None:
    # Valid Python
    valid_py = tmp_path / "app.py"
    valid_py.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    passed, msg, details = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-001", {"deliverable": {"path": "app.py"}}
    )
    assert passed is True
    assert "Stack-appropriate verification passed" in msg

    # Invalid Python syntax
    invalid_py = tmp_path / "broken.py"
    invalid_py.write_text("def hello(\n", encoding="utf-8")
    passed, msg, details = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-001", {"deliverable": {"path": "broken.py"}}
    )
    assert passed is False
    assert "SyntaxError" in msg


def test_stack_appropriate_verifier_javascript(tmp_path: Path) -> None:
    # Valid JS
    valid_js = tmp_path / "index.js"
    valid_js.write_text("function add(a, b) { return a + b; }\nmodule.exports = { add };\n", encoding="utf-8")
    passed, msg, details = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-002", {"deliverable": {"path": "index.js"}}
    )
    assert passed is True
    assert "Stack-appropriate verification passed" in msg

    # Invalid JS (unbalanced braces)
    invalid_js = tmp_path / "bad.js"
    invalid_js.write_text("function bad( { return 1;\n", encoding="utf-8")
    passed, msg, details = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-002", {"deliverable": {"path": "bad.js"}}
    )
    assert passed is False


def test_stack_appropriate_verifier_html_and_css(tmp_path: Path) -> None:
    html_file = tmp_path / "index.html"
    html_file.write_text("<!DOCTYPE html><html><head><title>Test</title></head><body><h1>Hello</h1></body></html>", encoding="utf-8")
    passed, msg, _ = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-003", {"deliverable": {"path": "index.html"}}
    )
    assert passed is True

    css_file = tmp_path / "style.css"
    css_file.write_text("body { margin: 0; padding: 0; }", encoding="utf-8")
    passed, msg, _ = review_gate.run_stack_appropriate_verifier(
        tmp_path, "WO-003", {"deliverable": {"path": "style.css"}}
    )
    assert passed is True


def test_validate_qa_verdict_converts_missing_test_to_no_test_evidence(tmp_path: Path) -> None:
    # Set up work order and deliverable
    sync_wo = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    sync_wo.mkdir(parents=True, exist_ok=True)
    wo_file = sync_wo / "WO-001.yaml"
    wo_file.write_text(
        "id: WO-001\ndeliverable:\n  type: code\n  path: calculator.js\n", encoding="utf-8"
    )

    app_js = tmp_path / "calculator.js"
    app_js.write_text("function calc(x) { return x * 2; }\nmodule.exports = { calc };\n", encoding="utf-8")

    sub_hash = review_gate.subject_hash(tmp_path, "WO-001")

    # Raw QA response from LLM that rejected because pytest found no tests
    raw_verdict = {
        "schema": review_gate.QA_VERDICT_SCHEMA,
        "work_order_id": "WO-001",
        "verdict": "FAIL",
        "subject_hash": sub_hash,
        "criteria": [
            {"id": "functional", "description": "Functional acceptance criteria", "outcome": "PASS"}
        ],
        "evidence": [
            {"command": "pytest", "passed": False}
        ],
        "findings": ["Pytest exit code 5: no tests were collected. No tests directory found."],
        "required_actions": ["Author tests in tests/ folder"],
    }

    # validate_qa_verdict should convert false-negative missing test failure into NO_TEST_EVIDENCE
    passed, reason = review_gate.validate_qa_verdict(raw_verdict, tmp_path, "WO-001")
    assert passed is True
    assert raw_verdict.get("verdict") == review_gate.QA_NO_TEST_EVIDENCE
    assert "converted to NO_TEST_EVIDENCE" in reason


def test_synthesize_authoritative_qa_verdict(tmp_path: Path) -> None:
    # Set up work order
    sync_wo = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    sync_wo.mkdir(parents=True, exist_ok=True)
    wo_file = sync_wo / "WO-002.yaml"
    wo_file.write_text(
        "id: WO-002\nrevision: 3\ndeliverable:\n  type: code\n  path: model.js\n", encoding="utf-8"
    )

    target_file = tmp_path / "model.js"
    content = "class ModelLoader { constructor() {} }\nmodule.exports = { ModelLoader };\n"
    target_file.write_text(content, encoding="utf-8")

    raw_llm_verdict = {
        "verdict": "PASS",
        "reason": "All checks passed",
        "timestamp": "2025-05-22T00:00:00Z",  # Hallucinated date from LLM!
        "subject_hash": "hallucinated_hash",
        "revision": 999,
        "criteria": [{"id": "acceptance", "description": "Meets functional criteria", "outcome": "PASS"}],
        "evidence": [{"kind": "static_check", "passed": True}],
    }

    state = RunState(run_id="run-test", product_goal="Test", workspace=str(tmp_path), session_id="sess-test")
    state.qa_review_attempts["WO-002"] = 2  # next is 3

    exec_mixin = sup_exec._ExecutionMixin()
    exec_mixin._synthesize_authoritative_qa_verdict(
        state, tmp_path, "WO-002", {"agent_id": "gemma", "result": {"verdict": raw_llm_verdict}}
    )

    persisted = review_gate.load_qa_verdict(tmp_path, "WO-002")
    assert persisted is not None

    # Must overwrite hallucinated timestamp with authoritative ISO 8601 UTC timestamp
    assert persisted["timestamp"] != "2025-05-22T00:00:00Z"
    assert datetime.fromisoformat(persisted["timestamp"]).year >= 2026

    # Must overwrite hallucinated subject_hash with true SHA256 of deliverable
    expected_hash = review_gate.subject_hash(tmp_path, "WO-002")
    assert persisted["subject_hash"] == expected_hash

    # Must overwrite revision with authoritative revision
    assert persisted["revision"] == 3
    assert persisted["verdict"] == "PASS"


def test_classify_failure_code_typed_blockers() -> None:
    mixin = sup_ctrl._ControlsMixin()
    assert mixin._classify_failure_code("pytest returned code 5: no tests found") == "NO_TEST_EVIDENCE"
    assert mixin._classify_failure_code("CONTRACT_VALIDATION_ERROR: peer collision") == "CONTRACT_SCOPE_VIOLATION"
    assert mixin._classify_failure_code("SyntaxError: unexpected token") == "CODE_VERIFICATION_FAILED"
    assert mixin._classify_failure_code("Circular dependency deadlock between WO-001 and WO-002") == "DEPENDENCY_DEADLOCK"
    assert mixin._classify_failure_code("Timeout while waiting for lock") == "TIMEOUT"


def test_single_deliverable_ownership_find_existing(tmp_path: Path) -> None:
    sync_dir = tmp_path / ".sync" / "work-orders"
    active_dir = sync_dir / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo_file = active_dir / "WO-001.yaml"
    wo_file.write_text(
        "id: WO-001\nassigned_agents:\n  - frontend\ndeliverable:\n  path: assets/js/model-loader.js\n",
        encoding="utf-8",
    )

    owner = tools.ToolGateway._find_existing_deliverable_owner(None, tmp_path, ["assets/js/model-loader.js"])
    assert owner is not None
    wo_id, agent, role, status, matched = owner
    assert wo_id == "WO-001"
    assert agent == "frontend"
    assert matched == "assets/js/model-loader.js"

    non_owner = tools.ToolGateway._find_existing_deliverable_owner(None, tmp_path, ["assets/js/non-existent.js"])
    assert non_owner is None


def test_ephemeral_test_logs_do_not_exceed_max_files_touched(tmp_path: Path) -> None:
    # Contract permits 1 file touched
    contract = AgentContract({
        "work_order": "WO-012",
        "agent_id": "backend",
        "budget": {"max_files_touched": 1},
        "scope": {
            "write": "read-write",
            "allow": [{"path": "calculator-core.js"}, {"path": "tests/*"}],
            "deny": [],
        },
    })

    class DummyTask:
        work_order_id = "WO-012"

    class DummyDecision:
        status = "completed"
        modified_files = ["calculator-core.js", "tests/wo012_smoke_log.txt"]

    # verify_post_execution should filter out ephemeral tests/wo012_smoke_log.txt
    # and verify that only 1 project file was touched (calculator-core.js)
    verify_post_execution(
        project_path=tmp_path,
        agent="backend",
        task=DummyTask(),
        decision=DummyDecision(),
        observed_files=["calculator-core.js", "tests/wo012_smoke_log.txt"],
        contract=contract,
    )


def test_copy_workspace_for_staging_ignores_runtime_and_transient_files(tmp_path: Path) -> None:
    from validators.harness.runner.staging import copy_workspace_for_staging

    src_dir = tmp_path / "src_project"
    src_dir.mkdir()

    # Governed files that SHOULD be copied
    (src_dir / "index.html").write_text("<h1>App</h1>", encoding="utf-8")
    js_dir = src_dir / "assets" / "js"
    js_dir.mkdir(parents=True)
    (js_dir / "main.js").write_text("console.log('hi');", encoding="utf-8")
    sync_wo = src_dir / ".sync" / "work-orders"
    sync_wo.mkdir(parents=True)
    (sync_wo / "WO-001.yaml").write_text("id: WO-001\n", encoding="utf-8")

    # Ephemeral / daemon runtime files that MUST NOT be copied
    sync_runtime = src_dir / ".sync" / "runtime" / "daemon"
    sync_runtime.mkdir(parents=True)
    (sync_runtime / "daemon-state.json").write_text("{}", encoding="utf-8")
    boot_dir = src_dir / ".sync" / "runtime" / "boot"
    boot_dir.mkdir(parents=True)
    (boot_dir / "claude.boot.yaml").write_text("agent: claude\n", encoding="utf-8")
    (sync_runtime / "daemon-state.tmp.11900.1793781148820045800").write_text("tmp", encoding="utf-8")
    (src_dir / "tests").mkdir(parents=True)
    (src_dir / "tests" / "wo002_smoke_log.txt").write_text("log", encoding="utf-8")
    (src_dir / "scratch.tmp").write_text("scratch", encoding="utf-8")

    staged_dir = tmp_path / "staged_project"
    copy_workspace_for_staging(src_dir, staged_dir)

    # Verify governed files exist in staged copy
    assert (staged_dir / "index.html").is_file()
    assert (staged_dir / "assets" / "js" / "main.js").is_file()
    assert (staged_dir / ".sync" / "work-orders" / "WO-001.yaml").is_file()

    # Verify ephemeral / daemon runtime files were excluded
    assert not (staged_dir / ".sync" / "runtime" / "daemon").exists()
    # Boot snapshots must survive: `stackmind validate` runs on the staged copy.
    assert (staged_dir / ".sync" / "runtime" / "boot" / "claude.boot.yaml").is_file()
    assert not (staged_dir / "scratch.tmp").exists()
    assert not (staged_dir / "tests" / "wo002_smoke_log.txt").exists()


def test_restore_worker_contract_after_recovery(tmp_path: Path) -> None:
    """Test that supervisor restores worker contract permissions when redispatching after recovery."""
    import yaml
    from validators.kernel.daemon.supervisor.recovery import _RecoveryMixin
    from validators.kernel.daemon.supervisor.execution import _ExecutionMixin

    class DummySupervisor(_RecoveryMixin, _ExecutionMixin):
        def __init__(self):
            pass

    sup = DummySupervisor()
    ws = tmp_path

    # Work order assigned to codex
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    wo_data = {
        "id": "WO-001",
        "assigned_agents": ["codex"],
        "deliverable": {"path": "requirements.txt"},
        "implementation_estimate": {"expected_files": ["requirements.txt", "index.html"]},
    }
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

    # Contract left by architect repair with claude
    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True)
    repair_contract = {
        "schema_version": 1,
        "agent_id": "claude",
        "work_order": "WO-001",
        "identity": {"role": "architecture", "reports_to": "ceo"},
        "scope": {"allow": [{"module": "schemas/**"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 25},
        "synthesized_by": "recovery-repair",
    }
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(repair_contract), encoding="utf-8")

    # Run contract restoration
    sup._restore_worker_contract_after_recovery(ws, "WO-001", None)

    restored = yaml.safe_load((contracts_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert restored["agent_id"] == "codex"
    assert restored["identity"]["role"] == "backend"
    assert "synthesized_by" not in restored

    allowed = [r["module"] for r in restored["scope"]["allow"]]
    assert "requirements.txt" in allowed
    assert "index.html" in allowed


def test_contract_loader_adapts_recovery_contract_for_redispatched_worker(tmp_path: Path) -> None:
    """Test that load_harness_contract adapts a recovery-repair contract for the executing worker."""
    import yaml
    from validators.harness.contract_loader import load_harness_contract

    ws = tmp_path
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    wo_data = {
        "id": "WO-001",
        "assigned_agents": ["codex"],
        "deliverable": {"path": "requirements.txt"},
    }
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_data), encoding="utf-8")

    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True)
    contract_data = {
        "schema_version": 1,
        "agent_id": "claude",
        "work_order": "WO-001",
        "identity": {"role": "architecture", "reports_to": "ceo"},
        "scope": {"allow": [{"module": "requirements.txt"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": 10},
        "synthesized_by": "recovery-repair",
    }
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(contract_data), encoding="utf-8")

    contract = load_harness_contract(ws, agent="codex", work_order_id="WO-001")
    assert contract is not None
    assert contract.agent_id == "codex"
    allowed = [r["module"] for r in contract.allow_rules]
    assert "requirements.txt" in allowed


def test_handle_blocked_wo_classifies_scope_permission_denial(tmp_path: Path) -> None:
    """Test that tool loop halt due to scope permission denial is classified as CONTRACT_SCOPE_DENIED."""
    from validators.kernel.daemon.supervisor.execution import _ExecutionMixin
    from validators.kernel.daemon.supervisor.core import RunState, Phase

    class DummySupervisor(_ExecutionMixin):
        def _sha256_file(self, p):
            return "hash"
        def _agent_for_wo(self, wo_id, ws):
            return "codex"
        def _dispatch_recovery_decision(self, state, ws, wo_id):
            return {"operation_id": "op-rec-001"}
        def _transition(self, state, phase):
            state.phase = phase

    sup = DummySupervisor()
    state = RunState(session_id="s1", workspace=str(tmp_path), product_goal="goal", run_id="r1")
    op = {"operation_id": "op-worker-001"}
    result = {
        "status": "blocked",
        "reason": "Governed tool loop halted: Pathological loop: call 'write_file' with identical arguments repeated 3 times without progress",
        "error": "Write(requirements.txt) - Permission denied (outside assigned scope)",
        "blockers": ["Write(requirements.txt) - Permission denied (outside assigned scope)"],
    }

    res = sup._handle_blocked_wo(state, tmp_path, "WO-001", op, result)
    assert "WO-001" in state.worker_blockers
    evidence = state.worker_blockers["WO-001"]
    assert evidence["failure_code"] == "CONTRACT_SCOPE_DENIED"


