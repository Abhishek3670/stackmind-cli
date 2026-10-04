"""Tests for deliverable nudge in ProviderGateway, tool extraction in OllamaAdapter,
and outcome_verified auto-retry in LifecycleSupervisor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest
import yaml

from validators.kernel import (
    AgentSession,
    AuthorizationPolicy,
    ContractNormalizer,
    Message,
    OpenAICompatibleAdapter,
    OperationJournal,
    ProviderGateway,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
)
from validators.kernel.providers.adapter import OllamaAdapter
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)


def _setup_test_gateway(tmp_path):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    workspace = ScratchWorkspace.create(authoritative, "attempt-nudge")
    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-003",
        "scope": {
            "allow": ["workspace/**"],
            "deny": [],
            "write": "read-write",
        },
        "budget": {"max_tokens": 10000, "max_files_touched": 12},
    })
    policy = AuthorizationPolicy.permit("human-approved", ["read_file", "write_file", "list_directory"])
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession("codex", "mock-provider", str(authoritative), session_id="session-nudge")
    attempt = session.create_attempt(contract, attempt_id="attempt-nudge")
    tools = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-nudge",
        attempt_id="attempt-nudge",
        actor_id="codex",
        provider_id="mock-provider",
    )
    return authoritative, workspace, tools, attempt, contract


def test_gateway_nudges_when_required_deliverable_not_written(tmp_path):
    """If provider emits conversational text without authoring deliverable, gateway nudges it."""
    authoritative, workspace, tool_gw, attempt, contract = _setup_test_gateway(tmp_path)

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        messages = payload.get("messages", [])

        if turn_count == 1:
            # Model gives conversational text without calling write_file
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Let me check the environment and look for any venv.",
                    },
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }
        elif turn_count == 2:
            # Verify the nudge was sent in the conversation history
            last_msg = messages[-1]
            assert "You responded with text without calling tools" in last_msg["content"]
            assert "src/backend.py" in last_msg["content"]
            # Now model calls write_file
            return {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "path": "src/backend.py",
                                    "content": "# Auth backend implementation\n",
                                }),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
            }
        else:
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Deliverable authored successfully."},
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    messages = [Message.user("Execute task 3")]
    history = gateway.run_loop(messages, max_turns=5, required_deliverable="src/backend.py")

    assert turn_count == 3
    assert "src/backend.py" in gateway.written_files
    assert (workspace.root / "src" / "backend.py").is_file()


def test_gateway_nudge_exhaustion_on_unwritten_deliverable(tmp_path):
    """Gateway bounds empty nudges to 2 and exits without infinite loop."""
    authoritative, workspace, tool_gw, attempt, contract = _setup_test_gateway(tmp_path)

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": f"Still thinking (turn {turn_count}).",
                },
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    messages = [Message.user("Execute task 3")]
    history = gateway.run_loop(messages, max_turns=10, required_deliverable="src/backend.py")

    # Initial turn (0) + 2 nudges = 3 total provider calls, then it exits
    assert turn_count == 3
    assert not gateway.written_files


def test_ollama_adapter_extracts_all_standard_tools():
    """OllamaAdapter recognizes list_directory, request_tools, and all standard tools in raw text."""
    adapter = OllamaAdapter()

    # 1. list_directory in <tool_call> tag
    content1 = '<tool_call>{"name": "list_directory", "arguments": {"path": "src"}}</tool_call>'
    calls1 = adapter._extract_tool_calls_from_content(content1)
    assert len(calls1) == 1
    assert calls1[0].name == "list_directory"
    assert calls1[0].arguments == {"path": "src"}

    # 2. request_tools in markdown json block
    content2 = '```json\n{"name": "request_tools", "arguments": {"query": "knowledge"}}\n```'
    calls2 = adapter._extract_tool_calls_from_content(content2)
    assert len(calls2) == 1
    assert calls2[0].name == "request_tools"
    assert calls2[0].arguments == {"query": "knowledge"}


class MockSupervisorManager:
    def __init__(self):
        self.operations = {}
        self.turns = []

    def list_operations(self, session_id):
        return list(self.operations.values())

    def get_operation(self, operation_id):
        return self.operations.get(operation_id)

    def start_turn(self, session_id, prompt, role, agent_id, work_order_id=None):
        op_id = f"op-retry-{len(self.turns) + 1}"
        op = {
            "operation_id": op_id,
            "session_id": session_id,
            "prompt": prompt,
            "role": role,
            "agent_id": agent_id,
            "work_order_id": work_order_id,
            "status": "RUNNING",
        }
        self.operations[op_id] = op
        self.turns.append(op)
        return op


def test_supervisor_retries_outcome_verified_failure(tmp_path):
    """When a worker operation is blocked due to outcome_verified deliverable failure, supervisor auto-retries."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo_path = active_dir / "WO-003.yaml"
    wo_path.write_text(yaml.dump({
        "id": "WO-003",
        "title": "Secure Authentication",
        "assigned_agents": ["codex"],
        "deliverable": {"path": "src/backend.py", "type": "code"},
        "status": "BLOCKED",
        "error": "Work order WO-003 blocked: verification gate failed: outcome_verified (declared deliverable 'src/backend.py' was not added or modified in this turn)",
    }), encoding="utf-8")

    mgr = MockSupervisorManager()
    blocked_op = {
        "operation_id": "op-blocked-1",
        "work_order_id": "WO-003",
        "agent_id": "codex",
        "status": "BLOCKED",
        "result": {
            "status": "blocked",
            "reason": "verification gate failed: outcome_verified (declared deliverable 'src/backend.py' was not added or modified in this turn)",
        },
    }
    mgr.operations["op-blocked-1"] = blocked_op

    state = RunState(
        run_id="run-test",
        product_goal="Build app",
        workspace=str(ws),
        session_id="session-test",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-003"],
        max_retries=2,
    )

    supervisor = LifecycleSupervisor(manager=mgr)
    result = supervisor.advance(state)

    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.retry_counts.get("WO-003") == 1
    assert "op-blocked-1" in state.ignored_operation_ids
    assert len(mgr.turns) == 1
    assert "declared deliverable 'src/backend.py' was not created" in mgr.turns[0]["prompt"]
    assert "write_file" in mgr.turns[0]["prompt"]

    # Verify work order on disk has error cleared and status ACTIVE
    data = yaml.safe_load(wo_path.read_text(encoding="utf-8"))
    assert data["status"] == "ACTIVE"
    assert "error" not in data
