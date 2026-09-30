"""Tests for Phase 5 Planning, Todo, and Governance tools in ToolGateway."""

from pathlib import Path
import pytest

from validators.kernel import (
    AgentSession, ContractNormalizer,
    OperationJournal, OperationType, RuntimeBoundary, ToolGateway,
    ScratchWorkspace,
)
from validators.kernel.identity import get_role_policy


def _setup_gateway(tmp_path: Path, agent: str = "claude", allow_patterns=None, deny_patterns=None, write_mode="read-write"):
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
    return gateway, journal


def test_todo_management(tmp_path):
    gateway, journal = _setup_gateway(tmp_path, "codex")

    # Add items
    todos = gateway.todo("add", "Write database models")
    assert len(todos) == 1
    assert todos[0]["id"] == 1
    assert todos[0]["status"] == "pending"

    todos = gateway.todo("add", "Write migrations")
    assert len(todos) == 2

    # Update item
    todos = gateway.todo("update", item_id=1, status="completed")
    assert todos[0]["status"] == "completed"

    # Delete item
    todos = gateway.todo("delete", item_id=2)
    assert len(todos) == 1

    # Clear
    todos = gateway.todo("clear")
    assert len(todos) == 0


def test_ask_user(tmp_path):
    gateway, journal = _setup_gateway(tmp_path, "codex")
    choice = gateway.ask_user("Which framework?", choices=["FastAPI", "Flask"])
    assert choice == "FastAPI"
    assert any(r.request.operation_type == OperationType.ASK_USER for r in journal.records)


def test_plan_mode_lifecycle_and_role_enforcement(tmp_path):
    # Claude can enter/exit plan mode
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    res_in = claude_gw.enter_plan_mode()
    assert res_in["status"] == "plan_mode_active"

    res_out = claude_gw.exit_plan_mode()
    assert res_out["status"] == "plan_mode_inactive"

    # Codex cannot enter plan mode
    codex_gw, _ = _setup_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.enter_plan_mode()


def test_work_order_creation_and_role_enforcement(tmp_path):
    # Codex cannot create work order
    codex_gw, _ = _setup_gateway(tmp_path, "codex")
    with pytest.raises(PermissionError, match="policy denies operation"):
        codex_gw.create_work_order(
            title="Implement Auth",
            deliverable={"path": "auth.py", "type": "code"},
            assigned_agent="codex",
        )

    # Claude can create work order
    claude_gw, _ = _setup_gateway(tmp_path, "claude")
    wo_id = claude_gw.create_work_order(
        title="Implement Auth System",
        deliverable={"file": "auth.py", "kind": "code"},
        assigned_agent="codex",
        wo_id="WO-002",
    )
    assert wo_id == "WO-002"
    wo_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml"
    assert wo_file.is_file()

    # Claude can update work order
    updated = claude_gw.update_work_order("WO-002", {"title": "Updated Auth System"})
    assert updated["title"] == "Updated Auth System"


def test_contract_introspection_and_scope_verification(tmp_path):
    gateway, _ = _setup_gateway(tmp_path, "codex", allow_patterns=["src/**"], deny_patterns=["src/secret/**"])

    # Get contract
    contract_data = gateway.get_contract()
    assert contract_data["agent_id"] == "codex"
    assert contract_data["work_order"] == "WO-001"

    # Inspect budget
    budget = gateway.inspect_budget()
    assert budget["max_tokens"] == 10000

    # Verify scope
    assert gateway.verify_scope("src/app.py", "read_file") is True
    assert gateway.verify_scope("src/secret/key.txt", "read_file") is False
    assert gateway.verify_scope("outside/file.py", "read_file") is False

    # Explain denial
    explanation_denied = gateway.explain_denial("src/secret/key.txt", "read_file")
    assert "target is explicitly denied" in explanation_denied

    explanation_ok = gateway.explain_denial("src/app.py", "read_file")
    assert "in scope and authorized" in explanation_ok
