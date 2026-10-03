"""Tests for kernel tool hardening and parameter alias compatibility."""

from pathlib import Path
import pytest
import yaml

from unittest.mock import MagicMock
from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import AuthorizationPolicy, get_role_policy
from validators.kernel.providers.gateway import ProviderGateway, ToolCallRequest
from validators.kernel.providers.adapter import ProviderAdapter


def _setup_test_gateway(tmp_path: Path, agent: str = "codex"):
    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": ["**"],
            "denied": [],
            "write": "read-write",
        },
        "budget": {
            "max_tokens": 10000,
            "max_files": 15,
        }
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
    return gateway, workspace


def test_authorization_policy_name_property():
    policy = AuthorizationPolicy(policy_id="policy-test")
    assert policy.policy_id == "policy-test"
    assert policy.name == "policy-test"


def test_inspect_agent_robustness(tmp_path):
    gateway, _ = _setup_test_gateway(tmp_path, "claude")
    for role in ("codex", "claude", "gemini", "gemma", "local-llm", "unknown-agent"):
        info = gateway.inspect_agent(role)
        assert info["agent"] == role
        assert "policy_name" in info
        assert "permitted_operations_count" in info
        assert isinstance(info.get("permitted_operations"), list)


def test_inspect_work_order_normalization(tmp_path):
    gateway, ws = _setup_test_gateway(tmp_path, "codex")
    active_dir = ws.root / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    wo_file = active_dir / "WO-001.yaml"
    wo_file.write_text(yaml.safe_dump({"id": "WO-001", "status": "ACTIVE"}), encoding="utf-8")

    # Inspect with raw ID
    res1 = gateway.inspect_work_order("WO-001")
    assert res1["found"] is True
    assert res1["work_order"]["id"] == "WO-001"

    # Inspect with .yaml extension
    res2 = gateway.inspect_work_order("WO-001.yaml")
    assert res2["found"] is True
    assert res2["work_order"]["id"] == "WO-001"

    # Inspect with full path
    res3 = gateway.inspect_work_order(".sync/work-orders/ACTIVE/WO-001.yaml")
    assert res3["found"] is True

    # Non-existent work order
    res4 = gateway.inspect_work_order("WO-999")
    assert res4["found"] is False


def test_get_contract_normalization(tmp_path):
    gateway, ws = _setup_test_gateway(tmp_path, "codex")
    contracts_dir = ws.root / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({"agent_id": "codex", "work_order": "WO-002"}), encoding="utf-8"
    )

    # Current contract
    c1 = gateway.get_contract()
    assert c1["found"] is True
    assert c1["work_order"] == "WO-001"

    # Another contract with .yaml
    c2 = gateway.get_contract("WO-002.yaml")
    assert c2["found"] is True
    assert c2["work_order"] == "WO-002"

    # Non-existent contract
    c3 = gateway.get_contract("WO-999")
    assert c3["found"] is False


def test_verify_deliverable_and_diff_normalization(tmp_path):
    gateway, ws = _setup_test_gateway(tmp_path, "gemma")
    active_dir = ws.root / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "deliverables": [{"path": "requirements.txt"}]
        }),
        encoding="utf-8",
    )
    (ws.root / "requirements.txt").write_text("flask\n", encoding="utf-8")

    # verify_deliverable
    deliv = gateway.verify_deliverable("WO-001.yaml")
    assert deliv["passed"] is True
    assert "requirements.txt" in deliv["deliverables_found"]

    # verify_diff does not crash
    diff_res = gateway.verify_diff("WO-001.yaml")
    assert "in_scope" in diff_res


def test_submit_verdict_normalization(tmp_path):
    gateway, ws = _setup_test_gateway(tmp_path, "gemma")
    verdict_msg = gateway.approve_work_order("WO-001.yaml")
    assert "APPROVED" in verdict_msg

    verdict_file = ws.root / ".sync" / "inbox" / "claude" / "verdict_WO-001.md"
    assert verdict_file.exists()


def test_diagnostics_summary(tmp_path):
    gateway, _ = _setup_test_gateway(tmp_path, "claude")
    summary = gateway.diagnostics_summary()
    assert "total_operations" in summary
    assert "denied_operations" in summary
    assert "running_processes" in summary


def test_gateway_parameter_alias_dispatch(tmp_path):
    gateway, ws = _setup_test_gateway(tmp_path, "codex")
    mock_adapter = MagicMock(spec=ProviderAdapter)
    exec_gw = ProviderGateway(adapter=mock_adapter, tool_gateway=gateway)

    # Calling inspect_agent with {"agent": "codex"} instead of {"agent_name": "codex"}
    call = ToolCallRequest(id="c1", name="inspect_agent", arguments={"agent": "codex"})
    output = exec_gw.execute_tool_call(call)
    assert "codex" in output
    assert "policy_name" in output

    # Calling inspect_work_order with {"work_order": "WO-001"}
    active_dir = ws.root / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump({"id": "WO-001"}), encoding="utf-8")

    call2 = ToolCallRequest(id="c2", name="inspect_work_order", arguments={"work_order": "WO-001"})
    output2 = exec_gw.execute_tool_call(call2)
    assert "WO-001" in output2
    assert "found" in output2
