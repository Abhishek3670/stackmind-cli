"""Tests for tool result sanitization (preventing empty outputs from breaking providers like Cohere/OpenRouter)
and integration review failure retry handling.
"""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from validators.kernel.providers.models import Message, MessageRole, ToolCallRequest
from validators.kernel.providers.adapter import OpenAICompatibleAdapter
from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState, Phase, AdvanceResult


def test_message_tool_replaces_empty_content():
    """Message.tool must normalize empty, whitespace, or None content to non-empty string."""
    m1 = Message.tool("", tool_call_id="call_1", name="git_diff")
    assert m1.content == "(no output)"

    m2 = Message.tool("   \n\t  ", tool_call_id="call_2", name="git_diff")
    assert m2.content == "(no output)"

    m3 = Message.tool(None, tool_call_id="call_3", name="git_diff")
    assert m3.content == "(no output)"

    m4 = Message.tool("diff --git a/foo b/foo", tool_call_id="call_4", name="git_diff")
    assert m4.content == "diff --git a/foo b/foo"


def test_message_to_dict_guarantees_non_empty_tool_content():
    """Message.to_dict must ensure role='tool' content is never empty string."""
    raw_m = Message(role=MessageRole.TOOL.value, content="", tool_call_id="call_5", name="git_diff")
    d = raw_m.to_dict()
    assert d["role"] == "tool"
    assert d["content"] == "(no output)"


def test_openai_compatible_adapter_prepares_non_empty_tool_payload():
    """OpenAICompatibleAdapter payload serialization must not emit empty content for tool messages."""
    adapter = OpenAICompatibleAdapter(
        model="openrouter/cohere/north-mini-code:free",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
    )
    raw_tool_msg = Message(role="tool", content="", tool_call_id="call_git_diff", name="git_diff")
    payload = adapter._prepare_payload([raw_tool_msg], tools=None, stream=False)

    msgs = payload["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["content"] == "(no output)"


def test_integration_review_retries_transient_failure(tmp_path: Path):
    """When an integration review turn fails with a transient provider error, supervisor retries up to max_retries."""
    mock_mgr = MagicMock()
    sup = LifecycleSupervisor(manager=mock_mgr)

    state = RunState(
        run_id="run-ir-retry",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-ir",
        phase=Phase.INTEGRATION_REVIEW,
        integration_operation_id="op-failed-ir",
        integration_retries=0,
        max_integration_retries=2,
    )

    # Mock failed operation in manager
    mock_mgr.list_operations.return_value = []
    sup._get_operation = MagicMock(return_value={
        "operation_id": "op-failed-ir",
        "status": "FAILED",
        "result": {"error": "Provider returned error: [400]"},
    })

    # Advance should retry: increment retries, clear op id, and re-invoke
    # On re-invoke with no integration_operation_id, it dispatches a turn:
    mock_mgr.start_turn.return_value = {"operation_id": "op-new-ir"}
    mock_mgr.begin_operation.return_value = (MagicMock(), "op-new-ir")

    res = sup._advance_integration_review(state)

    assert state.integration_retries == 1
    assert "op-failed-ir" in state.ignored_operation_ids
    assert res in (AdvanceResult.WAITING_FOR_OPERATION, AdvanceResult.TRANSITIONED)
