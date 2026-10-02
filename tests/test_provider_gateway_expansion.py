"""Unit and integration tests for Phase 9: Tool Gateway Runtime Integration & Provider Exposure.

Verifies:
1. get_tools_for_role filters STANDARD_KERNEL_TOOLS according to role policies for all 5 roles.
2. ProviderGateway dynamic tool call dispatch for extended tools.
3. Role boundary enforcement via ProviderGateway and ToolGateway.
4. Argument normalization across tool schemas and ToolGateway signatures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest

from validators.kernel import (
    AgentSession,
    AuthorizationPolicy,
    ContractNormalizer,
    OperationJournal,
    ProviderGateway,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
)
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationType
from validators.kernel.providers.adapter import OpenAICompatibleAdapter
from validators.kernel.providers.gateway import STANDARD_KERNEL_TOOLS, get_tools_for_role


def test_get_tools_for_role_all_roles() -> None:
    """Verify tool schemas are correctly filtered per role capability policies."""
    # 1. Claude (Architecture)
    claude_tools = {t.name for t in get_tools_for_role("claude")}
    assert "create_work_order" in claude_tools
    assert "update_work_order" in claude_tools
    assert "enter_plan_mode" in claude_tools
    assert "exit_plan_mode" in claude_tools
    assert "ask_user" in claude_tools
    assert "todo" in claude_tools
    assert "read_file" in claude_tools
    assert "find_symbol" in claude_tools
    assert "find_callers" in claude_tools
    assert "impact_analysis" in claude_tools
    assert "skill_mine" in claude_tools
    # Claude forbidden from code editing and git mutations
    assert "apply_patch" not in claude_tools
    assert "git_commit" not in claude_tools
    assert "create_release" not in claude_tools
    assert "submit_verdict" not in claude_tools

    # 2. Codex (Backend Lead)
    codex_tools = {t.name for t in get_tools_for_role("codex")}
    assert "apply_patch" in codex_tools
    assert "write_file" in codex_tools
    assert "move_file" in codex_tools
    assert "delete_file" in codex_tools
    assert "format_file" in codex_tools
    assert "run_tests" in codex_tools
    assert "run_lint" in codex_tools
    assert "process_start" in codex_tools
    assert "find_symbol" in codex_tools
    assert "find_references" in codex_tools
    assert "find_callers" in codex_tools
    # Codex cannot create work orders, commit, or issue QA verdicts
    assert "create_work_order" not in codex_tools
    assert "git_commit" not in codex_tools
    assert "create_release" not in codex_tools
    assert "submit_verdict" not in codex_tools
    assert "approve_work_order" not in codex_tools

    # 3. Gemini (Frontend Lead)
    gemini_tools = {t.name for t in get_tools_for_role("gemini")}
    assert "apply_patch" in gemini_tools
    assert "write_file" in gemini_tools
    assert "run_tests" in gemini_tools
    assert "find_symbol" in gemini_tools
    # Gemini cannot commit or create work orders
    assert "git_commit" not in gemini_tools
    assert "create_work_order" not in gemini_tools
    assert "submit_verdict" not in gemini_tools

    # 4. Gemma (QA Lead)
    gemma_tools = {t.name for t in get_tools_for_role("gemma")}
    assert "verify_deliverable" in gemma_tools
    assert "verify_tests" in gemma_tools
    assert "verify_diff" in gemma_tools
    assert "verify_provenance" in gemma_tools
    assert "submit_verdict" in gemma_tools
    assert "request_changes" in gemma_tools
    assert "approve_work_order" in gemma_tools
    assert "run_tests" in gemma_tools
    assert "skill_test" in gemma_tools
    assert "skill_audit" in gemma_tools
    # Gemma strictly forbidden from code mutation
    assert "write_file" not in gemma_tools
    assert "apply_patch" not in gemma_tools
    assert "move_file" not in gemma_tools
    assert "delete_file" not in gemma_tools
    assert "git_commit" not in gemma_tools

    # 5. Local-LLM (GitOps & Release Lead)
    local_tools = {t.name for t in get_tools_for_role("local-llm")}
    assert "git_stage" in local_tools
    assert "git_restore" in local_tools
    assert "git_create_branch" in local_tools
    assert "git_commit" in local_tools
    assert "git_tag" in local_tools
    assert "git_push" in local_tools
    assert "create_release" in local_tools
    assert "rollback_release" in local_tools
    assert "validate_release_metadata" in local_tools
    # Local-LLM cannot author architecture or work orders
    assert "create_work_order" not in local_tools
    assert "enter_plan_mode" not in local_tools


def _setup_mock_gateway(
    tmp_path: Path,
    role: str = "codex",
) -> tuple[ProviderGateway, ScratchWorkspace]:
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "sample.py").write_text("class UserService:\n    pass\n", encoding="utf-8")

    workspace = ScratchWorkspace.create(authoritative, "attempt-phase9")
    contract = ContractNormalizer.normalize({
        "agent": role,
        "wo": "WO-099",
        "scope": {
            "allow": ["workspace/**", "graph/**"],
            "deny": ["authoritative/**"],
            "write": "read-write",
        },
        "budget": {"max_tokens": 10000, "max_files_touched": 20},
    })
    policy = get_role_policy(role)
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession(role, "mock-provider", str(authoritative), session_id="session-p9")
    attempt = session.create_attempt(contract, attempt_id="attempt-p9")

    tool_gw = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-p9",
        attempt_id="attempt-p9",
        actor_id=role,
        provider_id="mock-provider",
        graph_query=lambda q: {"knowledge": f"info for {q}"},
    )

    def dummy_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        return {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        }

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com",
        model="test-gpt",
        transport=dummy_transport,
    )
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)
    return gateway, workspace


def test_provider_gateway_dynamic_dispatch(tmp_path: Path) -> None:
    """Verify ProviderGateway.execute_tool_call dispatches to dynamic ToolGateway methods."""
    gateway, workspace = _setup_mock_gateway(tmp_path, role="codex")

    # 1. find_symbol
    result = gateway.execute_tool_call("find_symbol", {"name": "UserService"})
    assert "UserService" in result

    # 2. todo tool call
    todo_res = gateway.execute_tool_call("todo", {"action": "list"})
    assert isinstance(todo_res, str)
    assert "Todos" in todo_res or "[]" in todo_res

    # 3. system_metrics
    metrics_res = gateway.execute_tool_call("system_metrics", {})
    metrics = json.loads(metrics_res)
    assert "platform" in metrics
    assert "python_version" in metrics

    # 4. read_many_files
    rm_res = gateway.execute_tool_call("read_many_files", {"paths": ["sample.py"]})
    assert "sample.py" in rm_res
    assert "UserService" in rm_res

    # 5. checkpoint
    cp_res = gateway.execute_tool_call("checkpoint", {"label": "test_checkpoint"})
    assert "Created checkpoint" in cp_res or "cp-" in cp_res


def test_provider_gateway_role_enforcement(tmp_path: Path) -> None:
    """Verify unauthorized tool calls are rejected per role policy."""
    # Claude attempting apply_patch must fail authorization
    claude_gw, _ = _setup_mock_gateway(tmp_path, role="claude")
    patch_result = claude_gw.execute_tool_call("apply_patch", {
        "path": "sample.py",
        "patch": "--- sample.py\n+++ sample.py\n@@ -1 +1 @@\n-class UserService:\n+class AdminService:\n",
    })
    # Either returns error message or contains 'not permitted' / 'denied'
    assert "not permitted" in patch_result.lower() or "denied" in patch_result.lower() or "unauthorized" in patch_result.lower()

    # Codex attempting git_commit must fail authorization
    codex_gw, _ = _setup_mock_gateway(tmp_path, role="codex")
    commit_result = codex_gw.execute_tool_call("git_commit", {
        "message": "test commit",
    })
    assert "not permitted" in commit_result.lower() or "denied" in commit_result.lower() or "unauthorized" in commit_result.lower()
