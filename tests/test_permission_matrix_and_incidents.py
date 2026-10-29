"""Tests for Permission Matrix, Incident Reflection Ledger, and TUI Clean Presentation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.text import Text

from cli.tui.chat import (
    render_actions_group,
    render_assistant_message,
    render_assistant_stream_header,
)
from cli.tui.state import ActionsGroup, TurnAction
from validators.kernel.incidents import (
    INCIDENTS_RELATIVE_PATH,
    format_incidents_for_prompt,
    get_recent_incidents,
    record_permission_incident,
)
from validators.kernel.permission_matrix import (
    ARCHITECT_PERMISSION_DIRECTIVE,
    PERMISSION_MATRIX_TABLE,
    get_permission_matrix_prompt,
)


def test_permission_matrix_content_and_roles() -> None:
    """Verify matrix contains all roles and boundary definitions."""
    assert "Architect" in PERMISSION_MATRIX_TABLE
    assert "Frontend" in PERMISSION_MATRIX_TABLE
    assert "Backend" in PERMISSION_MATRIX_TABLE
    assert "QA" in PERMISSION_MATRIX_TABLE
    assert "GitOps" in PERMISSION_MATRIX_TABLE

    prompt_claude = get_permission_matrix_prompt("claude")
    assert ARCHITECT_PERMISSION_DIRECTIVE.strip() in prompt_claude
    assert "DO NOT DIRECTLY WRITE APPLICATION CODE" in prompt_claude
    assert "DO NOT RUN SHELL COMMANDS" in prompt_claude
    assert "delegate_task" in prompt_claude

    prompt_gemini = get_permission_matrix_prompt("gemini")
    assert "FRONTEND SPECIALIST" in prompt_gemini

    prompt_codex = get_permission_matrix_prompt("codex")
    assert "BACKEND SPECIALIST" in prompt_codex


def test_record_and_retrieve_incidents(tmp_path: Path) -> None:
    """Verify incident logging and bounded retrieval from .sync/state/incidents.jsonl."""
    # Initially empty
    assert get_recent_incidents(tmp_path) == []
    assert format_incidents_for_prompt(tmp_path) == ""

    # Record first incident
    rec1 = record_permission_incident(
        project_path=tmp_path,
        agent="claude",
        role="architect",
        tool="write_file",
        target="assets/js/script.js",
        reason="outside assigned scope",
    )
    assert rec1 is not None
    assert rec1["agent"] == "claude"
    assert rec1["tool"] == "write_file"
    assert "delegate" in rec1["remedy"].lower()

    # Record second incident
    rec2 = record_permission_incident(
        project_path=tmp_path,
        agent="claude",
        role="architect",
        tool="run_command",
        target="mkdir -p assets/css",
        reason="outside assigned scope",
    )
    assert rec2 is not None
    assert "shell execution is blocked" in rec2["remedy"].lower()

    # Verify retrieval order (most recent first)
    incidents = get_recent_incidents(tmp_path, limit=5)
    assert len(incidents) == 2
    assert incidents[0]["tool"] == "run_command"
    assert incidents[1]["tool"] == "write_file"

    # Verify agent filtering
    claude_incidents = get_recent_incidents(tmp_path, agent="claude")
    assert len(claude_incidents) == 2
    gemini_incidents = get_recent_incidents(tmp_path, agent="gemini")
    assert len(gemini_incidents) == 0

    # Verify format_incidents_for_prompt
    prompt_str = format_incidents_for_prompt(tmp_path, agent="claude")
    assert "RECENT PERMISSION DENIALS ENCOUNTERED" in prompt_str
    assert "Attempted `run_command`" in prompt_str
    assert "Attempted `write_file` on 'assets/js/script.js'" in prompt_str
    assert "DO NOT retry the blocked operations" in prompt_str


def test_runner_adhoc_system_message_includes_matrix_and_incidents(tmp_path: Path) -> None:
    """Verify runner adhoc system message injects permission matrix and past incidents."""
    # Seed an incident
    record_permission_incident(
        project_path=tmp_path,
        agent="claude",
        role="architect",
        tool="bash",
        target="grep -i phone landing.html",
        reason="outside assigned scope",
    )

    from validators.harness.runner import AgentRunner

    runner = AgentRunner(
        project_path=tmp_path,
        agent="claude",
        max_turns=3,
        max_tool_calls=10,
    )

    sys_msg = runner._build_adhoc_system_message()

    # Invariant: contradictory instruction must be absent
    assert "directly use `write_file` or `apply_patch` to repair deliverables" not in sys_msg

    # Invariant: delegation guidance must be present
    assert "target_agent='qa'" in sys_msg
    assert "target_agent='gitops'" in sys_msg
    assert "delegate_task" in sys_msg
    assert "NEVER directly write application source files" in sys_msg

    # Invariant: matrix and incidents must be injected
    assert "MULTI-AGENT PERMISSION & BOUNDARY MATRIX" in sys_msg
    assert "RECENT PERMISSION DENIALS ENCOUNTERED" in sys_msg
    assert "Attempted `bash` on 'grep -i phone landing.html'" in sys_msg


def test_tui_suppresses_empty_agent_actions() -> None:
    """Verify that an ActionsGroup containing only an Agent marker renders empty."""
    group = ActionsGroup(actions=[
        TurnAction(action_id="act-1", tool_name="harness", description="Agent(Worker)", status="COMPLETED")
    ])

    res = render_actions_group(group)
    assert isinstance(res, Text)
    assert res.plain == ""


def test_tui_renders_concrete_actions() -> None:
    """Verify that concrete tool actions are rendered normally."""
    group = ActionsGroup(actions=[
        TurnAction(action_id="act-1", tool_name="read_file", target="landing.html", description="Read(landing.html)", status="COMPLETED")
    ])

    res = render_actions_group(group)
    rendered_str = str(res)
    assert "Read(landing.html)" in rendered_str or "Read" in rendered_str


def test_tui_assistant_message_suppresses_empty_phantom_turn() -> None:
    """Verify that an assistant message with empty content and only empty Agent action produces Text('')."""
    group = ActionsGroup(actions=[
        TurnAction(action_id="act-1", tool_name="harness", description="Agent(Worker)", status="COMPLETED")
    ])

    res = render_assistant_message(
        content="",
        actions=group,
        model="Agent · daemon",
    )
    assert isinstance(res, Text)
    assert res.plain == ""


def test_tui_stream_header_cleans_agent_daemon_label() -> None:
    """Verify that 'Agent · daemon' label is sanitized to avoid noisy transport leaks."""
    hdr = render_assistant_stream_header(model="Agent · daemon")
    assert "StackMind" in hdr
    assert "daemon" not in hdr
    assert "Agent" not in hdr

    # Real models/roles are preserved
    hdr_arch = render_assistant_stream_header(model="Architecture · claude")
    assert "StackMind" in hdr_arch
    assert "Architecture · claude" in hdr_arch

def test_compute_actionable_remedy_patterns() -> None:
    """Verify intelligent, pattern-specific remedy generation."""
    from validators.kernel.incidents import compute_actionable_remedy

    # 1. Interpreter -c code-string flag
    rem1 = compute_actionable_remedy(
        role="backend",
        tool="run_command",
        target="python -c import fastapi",
        reason="interpreter 'python' invoked with code-string flag '-c' is denied at the agent boundary",
    )
    assert "Inline interpreter code flags" in rem1
    assert "tests/" in rem1 or "server/" in rem1

    # 2. Worker trying to read work order
    rem2 = compute_actionable_remedy(
        role="backend",
        tool="read_file",
        target=".sync/work-orders/ACTIVE/WO-001.yaml",
        reason="target is outside allowed scope",
    )
    assert "Task Contract" in rem2
    assert "Direct access to '.sync/work-orders/' is restricted" in rem2

    # 3. Schema validation failure
    rem3 = compute_actionable_remedy(
        role="architect",
        tool="write_file",
        target=".sync/work-orders/ACTIVE/WO-001.yaml",
        reason="Authoring validation failed for WO-001.yaml: unexpected property",
    )
    assert "schema" in rem3.lower()


def test_incident_alias_retrieval(tmp_path: Path) -> None:
    """Verify get_recent_incidents resolves agent aliases (backend <-> codex)."""
    record_permission_incident(
        project_path=tmp_path,
        agent="codex",
        role="backend",
        tool="run_command",
        target="python -c import foo",
        reason="interpreter 'python' invoked with code-string flag '-c' is denied at the agent boundary",
    )

    # Querying by 'backend' role matches 'codex' agent
    incidents_backend = get_recent_incidents(tmp_path, agent="backend")
    assert len(incidents_backend) == 1
    assert "Inline interpreter code flags" in incidents_backend[0]["remedy"]

    # Querying by 'codex' also matches
    incidents_codex = get_recent_incidents(tmp_path, agent="codex")
    assert len(incidents_codex) == 1


def test_contract_architect_read_sync_access() -> None:
    """Verify Architect can read and list .sync/work-orders and .sync/decisions."""
    from validators.kernel.contract import AgentContract, ContractEvaluator

    contract = AgentContract(
        agent_id="claude",
        work_order="WO-000",
        allow=("PLAN.md", ".sync/work-orders/**", ".sync/decisions/**"),
        deny=(".git/**",),
        write_mode="read-write",
    )
    evaluator = ContractEvaluator()

    # Reading work orders
    ok_read, _ = evaluator.authorize(contract, "read_file", "workspace/.sync/work-orders/ACTIVE/WO-001.yaml")
    assert ok_read is True

    # Listing work orders directory
    ok_list, _ = evaluator.authorize(contract, "list_directory", "workspace/.sync/work-orders")
    assert ok_list is True

    # Listing decisions directory
    ok_list_dec, _ = evaluator.authorize(contract, "list_directory", "workspace/.sync/decisions")
    assert ok_list_dec is True
