import pytest
from cli.tui.commands import format_duration_human
from cli.tui.chat import (
    is_internal_sync_action,
    render_actions_group_str,
    render_action_preview_str,
)
from cli.tui.runtime_panel import format_model_badge
from cli.tui.state import ActionsGroup, TurnAction


def test_format_duration_human_sec_min_hr():
    """Verify format_duration_human transitions from sec to min to hr."""
    # Under 60s
    assert format_duration_human(0.0) == "0.0s"
    assert format_duration_human(1.23) == "1.2s"
    assert format_duration_human(14.2) == "14.2s"
    assert format_duration_human(59.9) == "59.9s"

    # 1 min to 1 hour
    assert format_duration_human(60.0) == "1m 0s"
    assert format_duration_human(65.0) == "1m 5s"
    assert format_duration_human(1997.5) == "33m 17s"
    assert format_duration_human(2182.3) == "36m 22s"
    assert format_duration_human(3599.0) == "59m 59s"

    # 1 hour and above
    assert format_duration_human(3600.0) == "1h 0m 0s"
    assert format_duration_human(3665.0) == "1h 1m 5s"
    assert format_duration_human(7325.0) == "2h 2m 5s"
    assert format_duration_human(86400.0) == "24h 0m 0s"


def test_hide_internal_sync_actions_in_chat():
    """Verify actions targeting .sync/ are hidden from the rendered chat stream."""
    sync_write = TurnAction(
        action_id="act-sync-1",
        description="Write(.sync/inbox/claude/WO-004-qa-verdict.md)",
        status="COMPLETED",
        tool_name="write_file",
        target=".sync/inbox/claude/WO-004-qa-verdict.md",
    )
    assert is_internal_sync_action(sync_write) is True

    user_write = TurnAction(
        action_id="act-user-1",
        description="Write(src/components/Header.tsx)",
        status="COMPLETED",
        tool_name="write_file",
        target="src/components/Header.tsx",
    )
    assert is_internal_sync_action(user_write) is False

    agent_qa = TurnAction(
        action_id="act-qa",
        description="Agent (QA)",
        status="COMPLETED",
        tool_name="run_harness",
    )
    agent_qa.sub_actions = [sync_write]

    group = ActionsGroup(actions=[user_write, agent_qa])
    rendered = render_actions_group_str(group, width=80)

    # User write is visible
    assert "Write(src/components/Header.tsx)" in rendered
    # Internal .sync write is hidden!
    assert ".sync" not in rendered
    assert "WO-004-qa-verdict.md" not in rendered


def test_hide_internal_sync_action_preview():
    """Verify render_action_preview returns empty/None for .sync operations."""
    sync_action = TurnAction(
        action_id="act-sync-2",
        description="Write (.sync/inbox/claude/WO-004-qa-verdict.md)",
        status="COMPLETED",
        tool_name="exec_command",
        target=".sync/inbox/claude/WO-004-qa-verdict.md",
        output="verdict: passed",
    )
    preview = render_action_preview_str(sync_action, width=80)
    assert preview == ""
    assert "Ran" not in preview
    assert ".sync" not in preview


def test_format_model_badge_strips_daemon_provider():
    """Verify [daemon/gemma4:31b-cloud] is cleaned up to [gemma4:31b-cloud]."""
    # When backend is "daemon", daemon prefix is removed
    badge = format_model_badge(backend="daemon", model="gemma4:31b-cloud")
    assert badge == "gemma4:31b-cloud"

    # When backend is real provider like ollama, it is preserved
    badge_ollama = format_model_badge(backend="ollama", model="gemma4:31b-cloud")
    assert badge_ollama == "ollama/gemma4:31b-cloud"

    # When backend is daemon and model is None, it returns empty
    badge_empty = format_model_badge(backend="daemon", model=None)
    assert badge_empty == ""
