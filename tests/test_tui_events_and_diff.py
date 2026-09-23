"""Automated test suite for StackMind TUI Operational Events, Inline Tools & Diff Viewer (WO-031)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.tui import (
    AutonomousDeliveryState,
    ChatMessage,
    OperationalEventManager,
    ToolActivity,
    ToolStatus,
    extract_assistant_response,
    parse_unified_diff,
    render_chat_transcript_str,
    render_file_diff,
    render_operational_event_str,
    render_tool_activity_line_str,
    render_tool_activity_str,
    render_unified_diff_str,
)


def test_tool_activity_status_rendering():
    """Verify tool activity blocks render correctly across all status states."""
    # 1. RUNNING
    running_act = ToolActivity(
        tool_name="read",
        target="app/api/users.py",
        status=ToolStatus.RUNNING,
    )
    running_str = render_tool_activity_str(running_act)
    assert "Read file" in running_str
    assert "app/api/users.py" in running_str
    assert "RUNNING" in running_str
    assert "⟳" in running_str

    # 2. COMPLETED with duration
    comp_act = ToolActivity(
        tool_name="edit",
        target="app/api/users.py",
        status=ToolStatus.COMPLETED,
        duration_seconds=0.2,
    )
    comp_str = render_tool_activity_str(comp_act)
    assert "Edit file" in comp_str
    assert "0.2s" in comp_str
    assert "✓" in comp_str

    # 3. FAILED with error message
    failed_act = ToolActivity(
        tool_name="test",
        target="pytest",
        status=ToolStatus.FAILED,
        duration_seconds=1.5,
        error="AssertionError: test failed",
    )
    failed_str = render_tool_activity_str(failed_act)
    assert "Run tests" in failed_str
    assert "FAILED" in failed_str
    assert "✗" in failed_str
    assert "AssertionError: test failed" in failed_str

    # 4. BLOCKED
    blocked_act = ToolActivity(
        tool_name="write_file",
        target="protected/secret.py",
        status=ToolStatus.BLOCKED,
    )
    blocked_str = render_tool_activity_str(blocked_act)
    assert "Write file" in blocked_str
    assert "BLOCKED" in blocked_str
    assert "⊘" in blocked_str

    # 5. CANCELLED
    canc_act = ToolActivity(
        tool_name="harness",
        target="long_running_task",
        status=ToolStatus.CANCELLED,
    )
    canc_str = render_tool_activity_str(canc_act)
    assert "CANCELLED" in canc_str
    assert "⊘" in canc_str


def test_tool_activity_compact_line_rendering():
    """Verify compact single-line rendering of tool activity."""
    act = ToolActivity(
        tool_name="read",
        target="app/api/users.py",
        status=ToolStatus.COMPLETED,
        duration_seconds=0.2,
    )
    line = render_tool_activity_line_str(act)
    assert "⚒" in line
    assert "Read file" in line
    assert "app/api/users.py" in line
    assert "0.2s" in line
    assert "✓" in line


def test_operational_event_translation():
    """Verify daemon operational events translate into clear visual transitions."""
    # operation.started
    op_started = render_operational_event_str({
        "name": "operation.started",
        "payload": {"operation_id": "op-42"},
    })
    assert "Operation op-42 started" in op_started

    # turn.started
    turn_started = render_operational_event_str({
        "name": "turn.started",
        "payload": {"operation_id": "turn-1", "prompt": "Implement user auth"},
    })
    assert "Turn started (turn-1): Implement user auth" in turn_started

    # operation.completed
    op_completed = render_operational_event_str({
        "name": "operation.completed",
        "payload": {"operation_id": "op-42", "status": "COMPLETED"},
    })
    assert "Operation op-42 completed (COMPLETED)" in op_completed

    # agentSpawned
    agent_ev = render_operational_event_str({
        "name": "event.agentSpawned",
        "payload": {"role": "Backend", "backend": "Codex"},
    })
    assert "Agent spawned: Backend (Codex)" in agent_ev

    # plan.proposed & plan.approved
    plan_prop = render_operational_event_str({
        "name": "plan.proposed",
        "payload": {"plan_id": "PLAN-001"},
    })
    assert "Plan proposed: PLAN-001" in plan_prop

    plan_app = render_operational_event_str({
        "name": "plan.approved",
        "payload": {"plan_id": "PLAN-001"},
    })
    assert "Plan approved: PLAN-001" in plan_app


def test_unified_diff_viewer_parsing_and_rendering():
    """Verify unified diffs parse headers, chunks, additions, and deletions cleanly."""
    sample_diff = """--- a/app/api/users.py
+++ b/app/api/users.py
@@ -1,6 +1,12 @@
-from fastapi import APIRouter
+from fastapi import APIRouter, HTTPException
+from pydantic import BaseModel
 
+class CreateUserRequest(BaseModel):
+    name: str
+    email: str
"""
    files = parse_unified_diff(sample_diff)
    assert len(files) == 1
    assert files[0]["path"] == "app/api/users.py"

    rendered = render_unified_diff_str(sample_diff)
    assert "app/api/users.py" in rendered
    assert "@@ -1,6 +1,12 @@" in rendered
    assert "-from fastapi import APIRouter" in rendered
    assert "+from fastapi import APIRouter, HTTPException" in rendered
    assert "+from pydantic import BaseModel" in rendered


def test_unified_diff_multi_file_and_empty():
    """Verify multi-file diffs and empty diff fallbacks."""
    multi_diff = """diff --git a/app/service.py b/app/service.py
--- a/app/service.py
+++ b/app/service.py
@@ -1,2 +1,3 @@
+import logging
diff --git a/tests/test_service.py b/tests/test_service.py
--- a/tests/test_service.py
+++ b/tests/test_service.py
@@ -1,2 +1,3 @@
+import pytest
"""
    files = parse_unified_diff(multi_diff)
    assert len(files) == 2
    assert files[0]["path"] == "app/service.py"
    assert files[1]["path"] == "tests/test_service.py"

    rendered_multi = render_unified_diff_str(multi_diff)
    assert "app/service.py" in rendered_multi
    assert "tests/test_service.py" in rendered_multi

    # Empty / no diff
    empty_diff = render_unified_diff_str("No staged daemon diff has been published.")
    assert "No staged daemon diff has been published." in empty_diff


def test_turn_response_extraction(tmp_path: Path):
    """Verify extraction of assistant response from report files and event payloads."""
    # 1. From harness markdown report
    report_file = tmp_path / "harness-report.md"
    report_file.write_text(
        "# Harness Report: task-1\n\n"
        "## Report\n"
        "I have implemented the user auth endpoint and validated it with tests.\n\n"
        "## Meta\n"
        "tokens: 100\n",
        encoding="utf-8",
    )

    payload_with_report = {
        "result": {
            "status": "completed",
            "report_path": str(report_file),
            "summary": "Processed task-1",
        }
    }
    extracted = extract_assistant_response(payload_with_report, workspace=tmp_path)
    assert extracted == "I have implemented the user auth endpoint and validated it with tests."

    # 2. From direct summary payload
    payload_summary = {
        "result": {
            "summary": "Completed code refactor in auth/service.py"
        }
    }
    assert extract_assistant_response(payload_summary) == "Completed code refactor in auth/service.py"


def test_operational_event_manager_lifecycle(tmp_path: Path):
    """Verify OperationalEventManager handles tool call start, completion, and response emission."""
    manager = OperationalEventManager(workspace=tmp_path)
    state = AutonomousDeliveryState(project_name="test-project")

    # 1. Tool Call start
    call_ev = {
        "name": "event.toolCall",
        "payload": {
            "call_id": "call-1",
            "tool_name": "read",
            "arguments": {"path": "cli/main.py"},
        }
    }
    renderable, resp = manager.process_event(call_ev, state=state)
    assert renderable is not None
    assert resp is None
    assert "call-1" in manager.active_tools

    # 2. Tool Result completion with assistant response
    result_ev = {
        "name": "event.toolResult",
        "payload": {
            "call_id": "call-1",
            "tool_name": "read",
            "status": "success",
            "result": {
                "summary": "Successfully read cli/main.py and verified syntax."
            }
        }
    }
    renderable_res, resp_res = manager.process_event(result_ev, state=state)
    assert renderable_res is not None
    assert resp_res == "Successfully read cli/main.py and verified syntax."
    assert "call-1" not in manager.active_tools
    assert len(manager.tool_history) == 1
    # Check that assistant response was added to state conversation
    assert state.has_conversation
    assert state.messages[-1].role == "assistant"
    assert "Successfully read cli/main.py" in state.messages[-1].content


def test_chat_transcript_with_inline_tools_and_diff():
    """Verify chat transcript renders user prompt, tool activity, inline diff, and assistant response."""
    messages = [
        ChatMessage("user", "Explain and refactor app/api/users.py"),
        ChatMessage("tool", "read app/api/users.py"),
        ChatMessage("diff", "--- a/app/api/users.py\n+++ b/app/api/users.py\n@@ -1,2 +1,3 @@\n+from fastapi import HTTPException\n"),
        ChatMessage("assistant", "I have added the HTTPException import."),
    ]
    transcript = render_chat_transcript_str(messages)
    assert "You" in transcript
    assert "Explain and refactor app/api/users.py" in transcript
    assert "Read file" in transcript
    assert "app/api/users.py" in transcript
    assert "+from fastapi import HTTPException" in transcript
    assert "StackMind" in transcript
    assert "I have added the HTTPException import." in transcript


def test_tui_repl_diff_and_events_commands(tmp_path: Path):
    """Verify interactive REPL handles :diff and :events commands with inline formatting."""
    user_inputs = "\n".join([
        ":diff",
        ":events",
        ":exit",
    ]) + "\n"

    result = CliRunner().invoke(
        cli,
        ["tui", "--workspace", str(tmp_path)],
        input=user_inputs,
    )
    assert result.exit_code == 0, result.output
    # :diff outputs clean diff panel
    assert "Diff" in result.output
    # :events reports status
    assert "No new events." in result.output or "session.started" in result.output or "Operation" in result.output
