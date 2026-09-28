"""Tests for Phase D: Work Order + Contract Authoring Gate and Governance (Phase D)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from cli.init import init
from validators.harness.authoring_gate import (
    AuthoringGate,
    AuthoringGateDecision,
    AuthoringValidationError,
)
from validators.harness.runner import (
    AgentRunner,
    HarnessTask,
)
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.providers import OpenAICompatibleAdapter
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


VALID_WORK_ORDER_YAML = """id: "WO-088"
type: "FEATURE"
title: "Implement Distributed Cache Provider"
status: "ACTIVE"
priority: "P1"
assigned_agents:
  - "codex"
dependencies: []
contract_ref: ".sync/contracts/WO-088.yaml"
created: "2026-09-27T00:00:00Z"
updated: "2026-09-27T00:00:00Z"
deliverable:
  type: "code"
  path: "src/cache/provider.py"
  description: "Distributed cache implementation"
rework_budget: 2
rework_count: 0
blocked_by_rework: false
log: []
"""

VALID_CONTRACT_YAML = """schema_version: 1
agent_id: "codex"
work_order: "WO-088"
identity:
  role: "Backend Lead"
  reports_to: "claude"
scope:
  allow:
    - module: "src.cache"
      depth: 2
  deny:
    - module: "src.security"
  write: "read-write"
budget:
  max_files_touched: 5
  max_tokens: 20000
  expires_at: "2026-10-01T23:59:59Z"
"""


def test_classify_artifact():
    gate = AuthoringGate()
    assert gate.classify_artifact(".sync/work-orders/ACTIVE/WO-088.yaml") == "work_order"
    assert gate.classify_artifact(".sync/work-orders/COMPLETED/WO-027.yaml") == "work_order"
    assert gate.classify_artifact(".sync/work-orders/INDEX.yaml") == "index"
    assert gate.classify_artifact(".sync/contracts/WO-088.yaml") == "contract"
    assert gate.classify_artifact("src/module.py") == "unknown"


def test_role_authorization_enforcement():
    gate = AuthoringGate()
    # Architect and CEO are authorized
    ok, reason = gate.validate_author_role("claude", ".sync/contracts/WO-088.yaml")
    assert ok is True and reason is None

    ok, reason = gate.validate_author_role("architecture", ".sync/work-orders/ACTIVE/WO-088.yaml")
    assert ok is True and reason is None

    ok, reason = gate.validate_author_role("ceo", ".sync/contracts/WO-088.yaml")
    assert ok is True and reason is None

    # Worker roles are forbidden from authoring contracts/work-orders
    for worker in ("codex", "gemini", "local-llm", "gemma"):
        ok, reason = gate.validate_author_role(worker, ".sync/contracts/WO-088.yaml")
        assert ok is False
        assert "not authorized" in reason
        assert "CONTRACT-01" in reason or "AGENTS.md" in reason

        ok, reason = gate.validate_author_role(worker, ".sync/work-orders/ACTIVE/WO-088.yaml")
        assert ok is False
        assert "not authorized" in reason


def test_work_order_validation_success():
    gate = AuthoringGate()
    decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-088.yaml", VALID_WORK_ORDER_YAML, agent="claude")
    assert decision.passed is True
    assert decision.errors == []
    assert decision.artifact_type == "work_order"


def test_work_order_validation_syntax_error():
    gate = AuthoringGate()
    bad_yaml = "id: WO-088\n  bad_indent:\n    - : oops\n"
    decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-088.yaml", bad_yaml, agent="claude")
    assert decision.passed is False
    assert any("syntax" in err.lower() or "parsing" in err.lower() for err in decision.errors)


def test_work_order_validation_schema_violation():
    gate = AuthoringGate()
    # Missing required 'priority' and 'dependencies'
    incomplete_wo = """id: "WO-088"
type: "FEATURE"
title: "Incomplete WO"
status: "ACTIVE"
assigned_agents: ["codex"]
"""
    decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-088.yaml", incomplete_wo, agent="claude")
    assert decision.passed is False
    assert any("schema error" in err.lower() for err in decision.errors)


def test_work_order_validation_id_filename_mismatch():
    gate = AuthoringGate()
    # File is WO-088.yaml but content says WO-099
    mismatched = VALID_WORK_ORDER_YAML.replace('id: "WO-088"', 'id: "WO-099"')
    decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-088.yaml", mismatched, agent="claude")
    assert decision.passed is False
    assert any("does not match filename stem" in err for err in decision.errors)


def test_contract_validation_success():
    gate = AuthoringGate()
    decision = gate.validate_artifact_content(".sync/contracts/WO-088.yaml", VALID_CONTRACT_YAML, agent="claude")
    assert decision.passed is True
    assert decision.errors == []
    assert decision.artifact_type == "contract"


def test_contract_validation_schema_violation():
    gate = AuthoringGate()
    # Missing required 'budget'
    missing_budget = """schema_version: 1
agent_id: "codex"
work_order: "WO-088"
scope:
  allow:
    - module: "src.cache"
  deny: []
  write: "read-write"
"""
    decision = gate.validate_artifact_content(".sync/contracts/WO-088.yaml", missing_budget, agent="claude")
    assert decision.passed is False
    assert any("schema error" in err.lower() for err in decision.errors)


def test_contract_validation_empty_allow_rules():
    gate = AuthoringGate()
    # Scope allow is empty
    empty_allow = """schema_version: 1
agent_id: "codex"
work_order: "WO-088"
scope:
  allow: []
  deny: []
  write: "read-write"
budget:
  max_files_touched: 5
"""
    decision = gate.validate_artifact_content(".sync/contracts/WO-088.yaml", empty_allow, agent="claude")
    assert decision.passed is False
    assert any("at least one 'allow' rule" in err for err in decision.errors)


def test_contract_validation_work_order_mismatch():
    gate = AuthoringGate()
    # File is WO-088.yaml but work_order says WO-099
    mismatched = VALID_CONTRACT_YAML.replace('work_order: "WO-088"', 'work_order: "WO-099"')
    decision = gate.validate_artifact_content(".sync/contracts/WO-088.yaml", mismatched, agent="claude")
    assert decision.passed is False
    assert any("does not match filename stem" in err for err in decision.errors)


def test_tool_gateway_write_file_role_enforcement(tmp_path: Path):
    """Test that ToolGateway immediately rejects contract authoring by worker agents fail-closed."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-authoring-role")
    contract = KernelContract(
        agent_id="codex",
        work_order="WO-088",
        allow=("workspace/.sync/contracts/**", "workspace/src/**"),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())

    # 1. Codex actor -> forbidden from writing to .sync/contracts
    worker_gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s1",
        attempt_id="a1",
        actor_id="codex",
        provider_id="mock",
    )
    with pytest.raises(PermissionError) as exc_info:
        worker_gateway.write_file(".sync/contracts/WO-088.yaml", VALID_CONTRACT_YAML)
    assert "Worker role 'codex' is not authorized" in str(exc_info.value)

    # 2. Claude actor -> authorized
    architect_gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s2",
        attempt_id="a2",
        actor_id="claude",
        provider_id="mock",
    )
    architect_gateway.write_file(".sync/contracts/WO-088.yaml", VALID_CONTRACT_YAML)
    assert ws.path_for(".sync/contracts/WO-088.yaml").exists()

    # 3. Claude actor with malformed YAML -> rejected by syntax/schema check
    with pytest.raises(PermissionError) as exc_info:
        architect_gateway.write_file(".sync/contracts/WO-088.yaml", "invalid: : yaml")
    assert "Authoring validation failed" in str(exc_info.value)


def _setup_authoring_project(tmp_path: Path, work_order_id: str = "WO-031") -> Path:
    project = tmp_path / "project"
    init(project, name="PhaseDAuthoring", no_git=True)
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["claude"]["assigned_work_orders"] = [work_order_id]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    (project / ".sync" / "work-orders" / "ACTIVE" / f"{work_order_id}.yaml").write_text(
        yaml.safe_dump({
            "id": work_order_id,
            "type": "FEATURE",
            "title": "Author Work Order and Contract for WO-088",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["claude"],
            "dependencies": [],
            "deliverable": {
                "type": "config",
                "path": ".sync/work-orders/ACTIVE/WO-088.yaml",
                "description": "Authored Work Order and Contract",
            },
        }, sort_keys=False),
        encoding="utf-8",
    )

    index_path = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    index_data["orders"].append({
        "id": work_order_id,
        "title": "Author Work Order and Contract for WO-088",
        "status": "ACTIVE",
        "priority": "P0",
        "dependencies": [],
        "deliverable": {
            "type": "config",
            "path": ".sync/work-orders/ACTIVE/WO-088.yaml",
            "description": "Authored Work Order and Contract",
        },
    })
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / f"{work_order_id}.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": work_order_id,
            "identity": {
                "role": "Senior Architect",
                "reports_to": "ceo",
            },
            "scope": {
                "allow": [
                    {"module": ".sync/work-orders/**"},
                    {"module": ".sync/contracts/**"},
                ],
                "deny": [{"module": "src"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 10000},
        }, sort_keys=False),
        encoding="utf-8",
    )
    return project


def test_phase_d_claude_authors_work_order_and_contract_end_to_end(tmp_path: Path):
    """Verify with a real Architecture-agent run that Work Orders and Contracts are authored through the

    governed tool loop, schema-validated, promoted to live workspace on disk, and usable.
    """
    project = _setup_authoring_project(tmp_path, work_order_id="WO-031")
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        messages = payload["messages"]
        if calls == 1:
            # Turn 1: Claude writes the Work Order
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "write_wo",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "path": ".sync/work-orders/ACTIVE/WO-088.yaml",
                                    "content": VALID_WORK_ORDER_YAML,
                                }),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        if calls == 2:
            # Turn 2: Claude writes the Contract
            assert any(m.get("role") == "tool" for m in messages)
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "write_contract",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "path": ".sync/contracts/WO-088.yaml",
                                    "content": VALID_CONTRACT_YAML,
                                }),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "status": "completed",
                        "summary": "Authored Work Order and Contract for WO-088",
                        "report_markdown": "WO-088 and its contract were created and validated.",
                        "blockers": [],
                        "modified_files": [
                            ".sync/work-orders/ACTIVE/WO-088.yaml",
                            ".sync/contracts/WO-088.yaml",
                        ],
                        "release_target": "v3.1.0",
                        "retrieval_queries": [],
                        "uncertainty": [],
                        "commands": [],
                    }),
                },
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 50},
        }

    runner = AgentRunner(project, "claude", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    # 1. Verification passed
    assert result.status == "completed", result.reason

    # 2. Both files were promoted to live workspace on disk
    live_wo = project / ".sync" / "work-orders" / "ACTIVE" / "WO-088.yaml"
    live_contract = project / ".sync" / "contracts" / "WO-088.yaml"
    assert live_wo.exists(), "Authored Work Order was not written to live project on disk"
    assert live_contract.exists(), "Authored Contract was not written to live project on disk"

    # 3. Schema & semantic integrity confirmed on disk
    gate = AuthoringGate()
    assert gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-088.yaml", live_wo.read_text(encoding="utf-8")).passed is True
    assert gate.validate_artifact_content(".sync/contracts/WO-088.yaml", live_contract.read_text(encoding="utf-8")).passed is True


def test_phase_d_claude_malformed_contract_rejected_fail_closed(tmp_path: Path):
    """Verify that a malformed or invalid Contract authored by an agent is rejected fail-closed and not promoted."""
    project = _setup_authoring_project(tmp_path, work_order_id="WO-031")
    calls = 0

    # Contract missing 'agent_id' and 'budget'
    invalid_contract = """schema_version: 1
work_order: "WO-088"
scope:
  allow:
    - module: "src.cache"
  deny: []
  write: "read-write"
"""

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "write_bad_contract",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "path": ".sync/contracts/WO-088.yaml",
                                    "content": invalid_contract,
                                }),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"total_tokens": 50},
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "status": "completed",
                        "summary": "Wrote invalid contract",
                        "report_markdown": "done",
                        "blockers": [],
                        "modified_files": [".sync/contracts/WO-088.yaml"],
                        "release_target": "v3.1.0",
                        "retrieval_queries": [],
                        "uncertainty": [],
                        "commands": [],
                    }),
                },
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 50},
        }

    runner = AgentRunner(project, "claude", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    # The tool execution or staged verification must fail fail-closed
    assert result.status in ("blocked", "failed", "stalled")

    # The malformed contract must NOT exist on disk in the live project
    live_contract = project / ".sync" / "contracts" / "WO-088.yaml"
    assert not live_contract.exists(), "Malformed contract should not have been promoted to live project root"


def test_authoring_gate_rejects_overwrite_of_existing_active_work_order(tmp_path: Path):
    """AuthoringGate rejects overwriting an existing active work order with different content."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    existing_wo = wo_dir / "WO-088.yaml"
    existing_wo.write_text(VALID_WORK_ORDER_YAML.replace("Implement Distributed Cache Provider", "Different Existing WO"), encoding="utf-8")

    gate = AuthoringGate(project_root=tmp_path)
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-088.yaml",
        VALID_WORK_ORDER_YAML,
        agent="claude",
    )
    assert decision.passed is False
    assert any("Overwrite conflict" in err for err in decision.errors)
    assert any("already exists on disk with different content" in err for err in decision.errors)


def test_authoring_gate_rejects_overwrite_of_existing_contract(tmp_path: Path):
    """AuthoringGate rejects overwriting an existing contract with different content."""
    contract_dir = tmp_path / ".sync" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    existing_contract = contract_dir / "WO-088.yaml"
    existing_contract.write_text(VALID_CONTRACT_YAML.replace("src.cache", "src.different"), encoding="utf-8")

    gate = AuthoringGate(project_root=tmp_path)
    decision = gate.validate_artifact_content(
        ".sync/contracts/WO-088.yaml",
        VALID_CONTRACT_YAML,
        agent="claude",
    )
    assert decision.passed is False
    assert any("Overwrite conflict" in err for err in decision.errors)


def test_authoring_gate_allows_idempotent_overwrite(tmp_path: Path):
    """AuthoringGate allows writing identical content to an existing active work order."""
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    existing_wo = wo_dir / "WO-088.yaml"
    existing_wo.write_text(VALID_WORK_ORDER_YAML, encoding="utf-8")

    gate = AuthoringGate(project_root=tmp_path)
    decision = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-088.yaml",
        VALID_WORK_ORDER_YAML,
        agent="claude",
    )
    assert decision.passed is True
    assert decision.errors == []


def test_authoring_gate_allows_same_session_updates(tmp_path: Path):
    """AuthoringGate allows multiple updates to a file authored during the current session."""
    gate = AuthoringGate(project_root=tmp_path)

    # First write in session (file does not yet exist on disk)
    dec1 = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-088.yaml",
        VALID_WORK_ORDER_YAML,
        agent="claude",
    )
    assert dec1.passed is True

    # Simulate scratch file being written
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-088.yaml").write_text(VALID_WORK_ORDER_YAML, encoding="utf-8")

    # Second write in the same session with modified content should succeed
    updated_wo = VALID_WORK_ORDER_YAML.replace("priority: \"P1\"", "priority: \"P0\"")
    dec2 = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-088.yaml",
        updated_wo,
        agent="claude",
    )
    assert dec2.passed is True


def test_tool_gateway_write_file_rejects_clobbering_existing_active_work_order(tmp_path: Path):
    """ToolGateway.write_file rejects overwriting an existing active WO on the authoritative root."""
    authoritative_root = tmp_path / "project"
    wo_dir = authoritative_root / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-088.yaml").write_text("id: WO-088\ntitle: Old Task\n", encoding="utf-8")

    ws = ScratchWorkspace.create(authoritative_root, attempt_id="test-overwrite-clobber")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-031",
        allow=("workspace/.sync/work-orders/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())

    gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s1",
        attempt_id="a1",
        actor_id="claude",
        provider_id="mock",
    )

    with pytest.raises(PermissionError) as exc_info:
        gateway.write_file(".sync/work-orders/ACTIVE/WO-088.yaml", VALID_WORK_ORDER_YAML)
    assert "Overwrite conflict" in str(exc_info.value)


def test_authoring_gate_session_paths_do_not_leak_across_gateways_or_sessions(tmp_path: Path):
    """Prove that _session_authored_paths is strictly scoped to a single ToolGateway / turn.

    Session/Turn 1 authors a work order.
    Session/Turn 2 (a fresh ToolGateway, as created per turn by runner._build_tool_runtime)
    must NOT inherit Session 1's authored paths, and must be rejected if attempting to overwrite.
    """
    authoritative_root = tmp_path / "project"
    wo_dir = authoritative_root / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)

    contract = KernelContract(
        agent_id="claude",
        work_order="WO-031",
        allow=("workspace/.sync/work-orders/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())

    # Turn 1: Gateway 1 creates WO-088.yaml
    ws1 = ScratchWorkspace.create(authoritative_root, attempt_id="turn-1")
    gateway1 = ToolGateway(
        workspace=ws1, boundary=boundary, contract=contract, policy=policy,
        session_id="session-1", attempt_id="turn-1", actor_id="claude", provider_id="mock",
    )
    # Turn 1 writes to scratch
    gateway1.write_file(".sync/work-orders/ACTIVE/WO-088.yaml", VALID_WORK_ORDER_YAML)
    # Simulate promotion to authoritative root
    (wo_dir / "WO-088.yaml").write_text(VALID_WORK_ORDER_YAML, encoding="utf-8")

    # Turn 2: Fresh Gateway 2 (representing a new turn or different session)
    ws2 = ScratchWorkspace.create(authoritative_root, attempt_id="turn-2")
    gateway2 = ToolGateway(
        workspace=ws2, boundary=boundary, contract=contract, policy=policy,
        session_id="session-2", attempt_id="turn-2", actor_id="claude", provider_id="mock",
    )

    # Gateway 2 has its own empty gate state
    assert getattr(gateway2, "_authoring_gate", None) is None

    # Attempting to write different content from Gateway 2 must be rejected
    different_wo = VALID_WORK_ORDER_YAML.replace("Implement Distributed Cache Provider", "Different Content")
    with pytest.raises(PermissionError) as exc_info:
        gateway2.write_file(".sync/work-orders/ACTIVE/WO-088.yaml", different_wo)

    assert "Overwrite conflict" in str(exc_info.value)
    assert "already exists on disk with different content" in str(exc_info.value)


def test_concurrent_turn_promotion_prevents_clobbering_under_runtime_lock(tmp_path: Path):
    """Prove that two concurrent runners attempting to promote conflicting work orders

    are serialized by _acquire_runtime_lock, and the second runner is blocked by
    _check_overwrite_conflict in _evaluate_verification_dimensions.
    """
    import threading
    project = _setup_authoring_project(tmp_path, work_order_id="WO-031")

    wo_a = VALID_WORK_ORDER_YAML.replace("Implement Distributed Cache Provider", "Turn A Content")
    wo_b = VALID_WORK_ORDER_YAML.replace("Implement Distributed Cache Provider", "Turn B Content")

    def make_transport(authored_wo_content: str):
        calls = 0
        def transport(payload, stream, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "tool_calls": [{
                                "id": "write_wo",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps({
                                        "path": ".sync/work-orders/ACTIVE/WO-088.yaml",
                                        "content": authored_wo_content,
                                    }),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"total_tokens": 50},
                }
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "status": "completed",
                            "summary": "Done",
                            "report_markdown": "Done",
                            "blockers": [],
                            "modified_files": [".sync/work-orders/ACTIVE/WO-088.yaml"],
                            "release_target": "v3.1.0",
                            "retrieval_queries": [],
                            "uncertainty": [],
                            "commands": [],
                        }),
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"total_tokens": 50},
            }
        return transport

    runner_a = AgentRunner(project, "claude", provider_adapter=OpenAICompatibleAdapter(transport=make_transport(wo_a)))
    runner_b = AgentRunner(project, "architect", provider_adapter=OpenAICompatibleAdapter(transport=make_transport(wo_b)))

    results = {}
    barrier = threading.Barrier(2)

    def run_worker(name, runner):
        barrier.wait()
        results[name] = runner.run_once(work_order_id="WO-031")

    t_a = threading.Thread(target=run_worker, args=("A", runner_a))
    t_b = threading.Thread(target=run_worker, args=("B", runner_b))

    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    # Exactly one runner succeeds in promoting; the other is blocked by the overwrite check
    statuses = {results["A"].status, results["B"].status}
    assert "completed" in statuses
    assert "blocked" in statuses

    # The file on disk was NOT corrupted or overwritten; exactly one content won
    live_wo = (project / ".sync" / "work-orders" / "ACTIVE" / "WO-088.yaml").read_text(encoding="utf-8")
    assert ("Turn A Content" in live_wo) ^ ("Turn B Content" in live_wo)



