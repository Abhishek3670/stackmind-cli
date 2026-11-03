"""Unit tests verifying adhoc greeting guardrails and work-order isolation in StackMind CLI."""

import pytest
from unittest.mock import MagicMock
from pathlib import Path

from validators.kernel.providers.gateway import (
    ProviderGateway,
    get_curated_tools_for_phase,
    ARCHITECTURE_ADHOC_TOOL_NAMES,
)
from validators.kernel.providers.models import Message, ProviderResponse
from validators.harness.runner import AgentRunner


def test_curated_tools_for_adhoc_phase_excludes_write_tools():
    """In adhoc phase, Architect palette must exclude direct file writing tools."""
    tools = get_curated_tools_for_phase("claude", phase="adhoc")
    tool_names = {t.name for t in tools}

    # Must contain inquiry, inspection, and delegation tools
    assert "read_file" in tool_names
    assert "list_directory" in tool_names
    assert "delegate_task" in tool_names
    assert "trigger_goal" in tool_names

    # Must NOT contain authoring tools that let local models scaffold unprompted
    assert "write_file" not in tool_names
    assert "batch_write_files" not in tool_names
    assert "apply_patch" not in tool_names


def test_gateway_blocks_work_order_authoring_in_adhoc_mode():
    """Gateway must reject write_file or batch_write_files targeting work orders in adhoc mode."""
    mock_adapter = MagicMock()
    mock_tool_gw = MagicMock()
    mock_tool_gw.actor_id = "claude"

    gw = ProviderGateway(
        adapter=mock_adapter,
        tool_gateway=mock_tool_gw,
        tools=(),
    )
    gw.is_adhoc = True

    # 1. Test write_file for work order
    res_wo = gw.execute_tool_call(
        "write_file",
        {"path": ".sync/work-orders/ACTIVE/WO-001.yaml", "content": "id: WO-001\n"}
    )
    assert "Error: Authoring work orders or contracts" in res_wo
    assert mock_tool_gw.write_file.call_count == 0

    # 2. Test write_file for contract
    res_c = gw.execute_tool_call(
        "write_file",
        {"path": ".sync/contracts/WO-001.yaml", "content": "scope: ...\n"}
    )
    assert "Error: Authoring work orders or contracts" in res_c
    assert mock_tool_gw.write_file.call_count == 0

    # 3. Test batch_write_files targeting work orders
    res_bw = gw.execute_tool_call(
        "batch_write_files",
        {
            "files": [
                {"path": ".sync/work-orders/INDEX.yaml", "content": "orders: []\n"}
            ]
        }
    )
    assert "Error: Authoring work orders or contracts" in res_bw
    assert mock_tool_gw.batch_write_files.call_count == 0


def test_adhoc_prompt_contains_greeting_invariants_and_preserves_governance(tmp_path: Path):
    """Adhoc system prompt must forbid unprompted scaffolding on greetings and retain schema specs."""
    runner = AgentRunner(tmp_path, agent="claude")
    msg = runner._build_adhoc_system_message()

    # Invariant 9: Greeting protection
    assert "CASUAL GREETING & CONVERSATIONAL INVARIANT" in msg
    assert "You MUST NOT execute any file-writing or scaffolding tools" in msg
    assert "You MUST NOT fabricate or assume a 'Project Scaffold'" in msg

    # Preserved specs for existing unit tests
    assert "WORK ORDER & CONTRACT GOVERNANCE SPECIFICATION" in msg
    assert "VALIDATION" in msg
    assert "deliverable:" in msg
    assert "code" in msg
    assert "id: WO-002" in msg
    assert "Testing, QA, and verification tasks MUST declare `type: 'VALIDATION'` (NEVER 'QA')" in msg
    assert "set `type: 'code'` and `path: 'tests/test_...py'` (NEVER `type: 'test'`)" in msg


def test_gateway_adhoc_mode_does_not_nudge_for_work_orders():
    """In adhoc mode, an assistant text reply without tool calls must not trigger work-order nudge."""
    mock_adapter = MagicMock()
    del mock_adapter.stream
    # Adapter returns a friendly greeting text on the first turn
    mock_adapter.complete.return_value = ProviderResponse(
        message=Message.assistant("Hello! I'm Claude, the Lead Architect. What are we building today?"),
    )

    mock_tool_gw = MagicMock()
    mock_tool_gw.actor_id = "claude"

    gw = ProviderGateway(
        adapter=mock_adapter,
        tool_gateway=mock_tool_gw,
        tools=get_curated_tools_for_phase("claude", phase="adhoc"),
    )
    gw.is_adhoc = True

    messages = [Message.user("Hey")]
    final_messages = gw.run_loop(messages)

    # Must only have called the adapter once (no nudges injected)
    assert mock_adapter.complete.call_count == 1
    # Result must be the greeting without any 'CRITICAL: You are the Lead Architect' nudge
    assert len(final_messages) == 2
    assert "Hello! I'm Claude" in final_messages[-1].content
    assert "CRITICAL: You are the Lead Architect" not in final_messages[-1].content
