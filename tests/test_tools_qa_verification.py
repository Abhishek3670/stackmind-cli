"""Tests for Phase 7 QA Verification and Verdict Engine Tools in ToolGateway."""

from pathlib import Path
import pytest
import yaml

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "gemma", allow_patterns=None, deny_patterns=None, write_mode="read-write"):
    if allow_patterns is None:
        allow_patterns = ["**"]
    if deny_patterns is None:
        deny_patterns = []

    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": allow_patterns,
            "denied": deny_patterns,
            "write": write_mode,
        },
    })
    session = AgentSession(agent, "test-provider", str(tmp_path), session_id="s1")
    attempt = session.create_attempt(contract, attempt_id="a1")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = get_role_policy(agent)
    workspace = ScratchWorkspace(authoritative_root=tmp_path, root=tmp_path, attempt_id="a1")
    gateway = ToolGateway(
        boundary=boundary,
        policy=policy,
        contract=attempt.contract,
        workspace=workspace,
        sandbox=None,
        session_id="s1",
        attempt_id="a1",
        actor_id=agent,
        provider_id="test-provider",
    )
    return gateway, journal


def test_verify_deliverables(tmp_path):
    # Setup work order with deliverables
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_file = wo_dir / "WO-010.yaml"
    wo_data = {
        "id": "WO-010",
        "type": "FEATURE",
        "title": "Auth Feature",
        "status": "PENDING",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverables": [
            {"path": "src/auth.py", "type": "code"},
            {"path": "tests/test_auth.py", "type": "test"},
        ],
    }
    wo_file.write_text(yaml.dump(wo_data), encoding="utf-8")

    gateway, _ = _setup_gateway(tmp_path, "gemma")

    # Missing deliverables first
    res1 = gateway.verify_deliverable("WO-010")
    assert res1["passed"] is False
    assert "src/auth.py" in res1["deliverables_missing"]

    # Create files
    src_file = tmp_path / "src" / "auth.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def auth(): pass\n", encoding="utf-8")

    test_file = tmp_path / "tests" / "test_auth.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_auth(): assert True\n", encoding="utf-8")

    res2 = gateway.verify_deliverable("WO-010")
    assert res2["passed"] is True
    assert "src/auth.py" in res2["deliverables_found"]
    assert "tests/test_auth.py" in res2["deliverables_found"]


def test_verdict_role_enforcement(tmp_path):
    # Codex cannot approve work orders or submit verdicts
    codex_gw, _ = _setup_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.approve_work_order("WO-001")

    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.submit_verdict("WO-001", "APPROVED", "report")

    # Claude cannot submit verdicts
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    with pytest.raises(PermissionError, match="policy denies operation"):
        claude_gw.approve_work_order("WO-001")


def test_gemma_verdict_submission_lifecycle(tmp_path):
    gemma_gw, journal = _setup_gateway(tmp_path, "gemma")

    # Request changes writes to codex inbox
    msg1 = gemma_gw.request_changes("WO-001", ["Missing unit tests for edge cases"])
    assert "NEEDS_CHANGES" in msg1
    codex_inbox = tmp_path / ".sync" / "inbox" / "codex" / "verdict_WO-001.md"
    assert codex_inbox.is_file()
    assert "Missing unit tests" in codex_inbox.read_text(encoding="utf-8")

    # Approve writes to claude inbox
    msg2 = gemma_gw.approve_work_order("WO-001", signature="gemma-sig-123")
    assert "APPROVED" in msg2
    claude_inbox = tmp_path / ".sync" / "inbox" / "claude" / "verdict_WO-001.md"
    assert claude_inbox.is_file()
    assert "gemma-sig-123" in claude_inbox.read_text(encoding="utf-8")


def test_skill_verification_and_release_metadata(tmp_path):
    gemma_gw, _ = _setup_gateway(tmp_path, "gemma")

    # Skill test
    s_test = gemma_gw.skill_test("auth_pattern")
    assert s_test["status"] == "verified"
    assert s_test["canary_simulation"] == "passed"

    # Skill audit
    s_audit = gemma_gw.skill_audit()
    assert s_audit["audited"] is True

    # Validate release metadata
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    meta = gemma_gw.validate_release_metadata()
    assert meta["valid"] is True
