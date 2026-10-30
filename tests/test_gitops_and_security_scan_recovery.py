"""Tests for GitOps and Security Scan recovery and authorization hardening.

Verifies:
1. ContractEvaluator authorizes run_security_scan on workspace root ('.', 'workspace')
   and workspace directories unless explicitly denied.
2. Kernel tools run_security_scan executes without PermissionError even if target
   directory does not exist or differs from allow globs.
3. LifecycleSupervisor _advance_gitops performs bounded retry on verification gate failures
   (e.g. security_verified credential leak) with actionable feedback in the retry prompt.
4. Database defaults ('postgres', 'root', 'localhost') in containers are recognized as allowlisted.
"""

from __future__ import annotations
from pathlib import Path
import pytest
import yaml

from validators.kernel.contract import AgentContract, ContractEvaluator
from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, AdvanceResult
from validators.kernel.security import is_allowlisted_token, scan_for_credential_leaks
from tests.test_lifecycle_supervisor import MockSessionManager


def test_contract_evaluator_authorizes_security_scan_workspace_root():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="gemma",
        work_order="WO-004",
        allow=("tests/**", "client/**", "server/**"),
        deny=(".git/**",),
        write_mode="read-write",
    )

    # 1. Scanning '.' or workspace root is authorized
    ok, msg = evaluator.authorize(contract, "run_security_scan", ".")
    assert ok is True
    assert msg == "authorized"

    ok, msg = evaluator.authorize(contract, "run_security_scan", "workspace/.")
    assert ok is True
    assert msg == "authorized"

    ok, msg = evaluator.authorize(contract, "run_security_scan", "")
    assert ok is True
    assert msg == "authorized"


def test_contract_evaluator_authorizes_security_scan_directories():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="gemma",
        work_order="WO-004",
        allow=("tests/**", "client/**", "server/**"),
        deny=(".git/**",),
        write_mode="read-write",
    )

    # 2. Scanning 'src/' is authorized even if not explicitly in contract.allow
    ok, msg = evaluator.authorize(contract, "run_security_scan", "workspace/src/")
    assert ok is True
    assert msg == "authorized"

    ok, msg = evaluator.authorize(contract, "run_security_scan", "workspace/server/")
    assert ok is True
    assert msg == "authorized"


def test_contract_evaluator_denies_security_scan_on_denied_target():
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="gemma",
        work_order="WO-004",
        allow=("tests/**", "client/**"),
        deny=(".git/**", ".env"),
        write_mode="read-write",
    )

    # 3. Scanning explicitly denied path is rejected
    ok, msg = evaluator.authorize(contract, "run_security_scan", "workspace/.git")
    assert ok is False
    assert "denied" in msg

    ok, msg = evaluator.authorize(contract, "run_security_scan", "workspace/.env")
    assert ok is False
    assert "denied" in msg


def test_security_scan_allowlist_container_defaults():
    # Database defaults in container manifests
    assert is_allowlisted_token("postgres") is True
    assert is_allowlisted_token("postgres_password") is True
    assert is_allowlisted_token("root_password") is True
    assert is_allowlisted_token("localhost") is True


def test_gitops_security_verified_block_retries_with_actionable_feedback(tmp_path: Path):
    """When a GitOps turn is blocked by verification gate (e.g. security_verified),
    the supervisor performs a bounded retry and passes the blocker feedback to the prompt."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-gitops-sec", "Build App", tmp_path, "sess-sec")
    state.phase = Phase.GITOPS
    state.gitops_wo_id = "WO-005"
    state.max_retries = 2

    contracts_dir = tmp_path / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "local-llm", "work_order": "WO-005",
            "scope": {"allow": [{"module": "docker-compose.yml"}, {"module": "VERSION.md"}, {"module": "CHANGELOG.md"}],
                      "deny": [], "write": "read-write"},
            "budget": {"max_files_touched": 5, "max_tokens": 0},
        }),
        encoding="utf-8",
    )

    # 1. First dispatch
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    gitops_op1 = state.gitops_operation_id
    assert gitops_op1 is not None

    # Verify initial prompt includes deployment security instruction
    initial_prompt = mock_mgr.operations[gitops_op1]["prompt"]
    assert "Ensure deployment manifests" in initial_prompt
    assert "NO hardcoded passwords" in initial_prompt

    # 2. GitOps turn finishes with security_verified verification gate blockage
    err_msg = "verification gate failed: security_verified (credential leak detected in docker-compose.yml: 'gacc***')"
    mock_mgr.operations[gitops_op1]["status"] = "BLOCKED"
    mock_mgr.operations[gitops_op1]["result"] = {
        "status": "blocked",
        "error": err_msg,
        "blockers": [err_msg],
    }

    # Advance -> supervisor detects blockage and retries with feedback
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.GITOPS
    assert state.retry_counts.get("WO-005") == 1

    # 3. Verify retry prompt contains the blocker feedback and fix guidance
    gitops_op2 = state.gitops_operation_id
    assert gitops_op2 != gitops_op1
    retry_prompt = mock_mgr.operations[gitops_op2]["prompt"]
    assert "CRITICAL FIX REQUIRED (Attempt 2)" in retry_prompt
    assert "credential leak detected in docker-compose.yml" in retry_prompt

    # 4. GitOps succeeds on retry. Completion now requires the mandatory
    # Architect final-acceptance evidence against the approved plan.
    from tests.review_helpers import final_pass
    (tmp_path / "PLAN.md").write_text("# Approved Plan\n\nBuild App.\n", encoding="utf-8")
    state.plan_id = "PLAN-001"
    state.integration_evidence = {"passed": True}
    final_pass(tmp_path, "run-gitops-sec", "PLAN-001", [])
    mock_mgr.operations[gitops_op2]["status"] = "COMPLETED"
    mock_mgr.operations[gitops_op2]["result"] = {"status": "completed"}

    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.COMPLETE
    assert state.phase == Phase.COMPLETE


def test_gitops_exhausted_retries_transition_to_blocked(tmp_path: Path):
    """When retries are exhausted on GitOps verification blocker, transitions to Phase.BLOCKED."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-gitops-exhaust", "Build App", tmp_path, "sess-ex")
    state.phase = Phase.GITOPS
    state.gitops_wo_id = "WO-005"
    state.max_retries = 1

    contracts_dir = tmp_path / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "local-llm", "work_order": "WO-005",
            "scope": {"allow": [{"module": "VERSION.md"}, {"module": "CHANGELOG.md"}],
                      "deny": [], "write": "read-write"},
            "budget": {"max_files_touched": 5, "max_tokens": 0},
        }),
        encoding="utf-8",
    )

    # Initial dispatch
    supervisor.advance(state)
    op1 = state.gitops_operation_id
    mock_mgr.operations[op1]["status"] = "BLOCKED"
    mock_mgr.operations[op1]["result"] = {
        "status": "blocked",
        "error": "verification gate failed: security_verified",
        "blockers": ["verification gate failed: security_verified"],
    }

    # Attempt 1 retry (max_retries = 1)
    supervisor.advance(state)
    assert state.phase == Phase.GITOPS
    assert state.retry_counts.get("WO-005") == 1

    op2 = state.gitops_operation_id
    mock_mgr.operations[op2]["status"] = "BLOCKED"
    mock_mgr.operations[op2]["result"] = {
        "status": "blocked",
        "error": "verification gate failed: security_verified",
        "blockers": ["verification gate failed: security_verified"],
    }

    # Retries exhausted -> transitions to BLOCKED
    res = supervisor.advance(state)
    assert res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "retries exhausted" in (state.error or "")


def test_tool_gateway_run_security_scan_handles_restricted_scope_and_missing_dir(tmp_path: Path):
    """ToolGateway run_security_scan executes cleanly without PermissionError when called on
    '.' or non-existent 'src/' under a restricted contract, and flags credential leaks."""
    from tests.test_tools_process_lifecycle import _setup_gateway
    from validators.kernel import ContractNormalizer

    # Restricted contract that only allows client/**
    restricted_contract = ContractNormalizer.normalize({
        "agent": "gemma",
        "wo": "WO-004",
        "contract_version": 1,
        "scope": {
            "allowed": ["client/**", "tests/**"],
            "denied": [".git/**"],
            "write": "read-write",
        },
    })

    gateway, _ = _setup_gateway(tmp_path, "gemma")
    gateway.contract = restricted_contract

    # 1. Scanning non-existent src/ does not raise PermissionError and returns empty findings
    res_src = gateway.run_security_scan("src/")
    assert res_src["success"] is True
    assert res_src["files_scanned"] == 0
    assert res_src["findings"] == []

    # 2. Scanning root '.' is authorized
    res_root = gateway.run_security_scan(".")
    assert res_root["success"] is True

    # 3. In-turn leak detection: create client/app.py with a hardcoded secret
    (tmp_path / "client").mkdir(parents=True, exist_ok=True)
    (tmp_path / "client" / "app.py").write_text("API_SECRET_KEY = 'sk-123456789012345678901234567890'\n", encoding="utf-8")

    res_client = gateway.run_security_scan("client/")
    assert res_client["success"] is False
    assert any("CREDENTIAL_LEAK" in f["code"] or "HARDCODED_SECRET" in f["code"] for f in res_client["findings"])

