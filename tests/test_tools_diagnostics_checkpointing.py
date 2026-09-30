"""Tests for Phase 8 Diagnostics, Health, Checkpointing, and Recovery Tools in ToolGateway."""

from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "codex", allow_patterns=None, deny_patterns=None, write_mode="read-write"):
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


def test_checkpoint_and_restore_lifecycle(tmp_path):
    gateway, journal = _setup_gateway(tmp_path, "codex")

    # Initial file state
    code_file = tmp_path / "app.py"
    code_file.write_text("v1_original = True\n", encoding="utf-8")

    # Create checkpoint
    cp_id = gateway.checkpoint(label="before_refactor")
    assert cp_id.startswith("cp-")

    # List checkpoints
    cps = gateway.list_checkpoints()
    assert len(cps) == 1
    assert cps[0]["checkpoint_id"] == cp_id
    assert cps[0]["label"] == "before_refactor"

    # Modify file
    code_file.write_text("v2_modified = True\n", encoding="utf-8")
    assert "v2_modified" in code_file.read_text(encoding="utf-8")

    # Restore checkpoint
    restored = gateway.restore_checkpoint(cp_id)
    assert restored is True
    assert "v1_original" in code_file.read_text(encoding="utf-8")


def test_inspect_logs_and_diagnostics_summary(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex")

    # Perform some operations
    gateway.todo("add", "Task 1")
    gateway.inspect_budget()

    # Inspect logs
    logs = gateway.inspect_logs()
    assert "todo" in logs
    assert "inspect_budget" in logs

    filtered_logs = gateway.inspect_logs(filter_term="budget")
    assert "budget" in filtered_logs
    assert "todo" not in filtered_logs

    # Diagnostics summary
    summary = gateway.diagnostics_summary()
    assert summary["total_operations"] >= 3
    assert summary["todo_items"] == 1


def test_system_metrics_and_artifacts_collection(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex")

    # Collect test artifacts
    (tmp_path / "test-report.xml").write_text("<xml/>", encoding="utf-8")
    (tmp_path / "coverage.json").write_text("{}", encoding="utf-8")
    artifacts = gateway.collect_test_artifacts()
    assert "test-report.xml" in artifacts
    assert "coverage.json" in artifacts

    # System metrics
    metrics = gateway.system_metrics()
    assert "python_version" in metrics
    assert "platform" in metrics
    assert metrics["actor"] == "codex"
