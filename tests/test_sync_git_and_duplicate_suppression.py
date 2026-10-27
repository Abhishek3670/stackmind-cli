"""Tests for .sync auto-commit governance and TUI duplicate response suppression."""

import subprocess
from pathlib import Path
import pytest

from validators.kernel.sync_git import commit_sync_governance
from cli.tui.state import AutonomousDeliveryState


def test_commit_sync_governance_creates_commit_and_sets_provenance(tmp_path: Path):
    # Set up mock project with .sync git repo
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(sync_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(sync_dir), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(sync_dir),
        check=True,
        capture_output=True,
    )

    # Initial commit so HEAD exists
    dummy_file = sync_dir / "README.md"
    dummy_file.write_text("# Sync Governance\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(sync_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: init"], cwd=str(sync_dir), check=True, capture_output=True
    )

    # Write a new governance report by specialist frontend
    outbox = sync_dir / "outbox" / "gemini"
    outbox.mkdir(parents=True)
    report_file = outbox / "harness-report.md"
    report_file.write_text("All frontend redesign deliverables verified.\n", encoding="utf-8")

    # Call commit_sync_governance
    sha = commit_sync_governance(
        tmp_path,
        "chore(governance): record completed turn by gemini [WO-001]",
        author="gemini",
    )
    assert sha is not None
    assert len(sha) == 40

    # Verify commit details
    log_proc = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%an|%ae|%cn|%ce|%s"],
        cwd=str(sync_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    parts = log_proc.stdout.split("|")
    assert parts[0] == "StackMind [gemini]"
    assert parts[1] == "gemini@stackmind.dev"
    assert parts[2] == "StackMind Governance"
    assert parts[3] == "governance@stackmind.dev"
    assert "record completed turn by gemini [WO-001]" in parts[4]

    # Clean tree should return None
    sha2 = commit_sync_governance(
        tmp_path,
        "chore(governance): noop",
        author="gemini",
    )
    assert sha2 is None


def test_commit_sync_governance_no_git_is_noop(tmp_path: Path):
    sync_dir = tmp_path / ".sync"
    sync_dir.mkdir(parents=True)
    # No .git directory
    sha = commit_sync_governance(tmp_path, "chore: test", author="claude")
    assert sha is None


def test_tui_state_recent_duplicate_assistant_message_suppression():
    state = AutonomousDeliveryState()
    resp_text = (
        "Done — I've dispatched the Frontend (Gemini) specialist to do the redesign."
    )

    # First delivery
    msg1 = state.add_message("assistant", resp_text)
    assert len(state.messages) == 1
    assert msg1.content.strip() == resp_text

    # Second delivery with slight trailing whitespace or duplicate SSE replay
    msg2 = state.add_message("assistant", resp_text + "   ")
    assert len(state.messages) == 1
    assert msg2 is msg1

    # An intermediate operational/status message or action occurs
    state.add_message("user", "how is it going?")
    assert len(state.messages) == 2

    # Replay of the first assistant message within the recent window
    msg3 = state.add_message("assistant", resp_text)
    # Must NOT create a duplicate message in the conversation area
    assert len(state.messages) == 2
    assert msg3 is msg1

    # A genuinely new/different assistant message is added
    msg4 = state.add_message("assistant", "The redesign is complete and verified!")
    assert len(state.messages) == 3
    assert msg4 is not msg1
    assert "redesign is complete" in msg4.content


def test_stream_turn_execution_filters_mismatched_op_id():
    from cli.tui.commands import extract_assistant_response

    # Simulated events in a stream
    parent_ev = {
        "name": "operation.completed",
        "operation_id": "op_claude_100",
        "payload": {
            "operation_id": "op_claude_100",
            "result": {
                "response": "Done — dispatched specialist.",
            },
        },
    }
    worker_ev = {
        "name": "operation.completed",
        "operation_id": "op_gemini_200",
        "payload": {
            "operation_id": "op_gemini_200",
            "result": {
                "response": "Landing page updated and brand verified.",
            },
        },
    }

    current_op_id = "op_gemini_200"

    # Simulate filtering logic applied in _stream_turn_execution
    events = [parent_ev, worker_ev]
    processed_responses = []

    for ev in events:
        ev_name = ev.get("name")
        ev_payload = ev.get("payload", {})
        if ev_name in {"operation.completed", "turn.completed"}:
            target_op = ev_payload.get("operation_id") or ev.get("operation_id")
            if current_op_id and current_op_id not in {"turn", "all"} and target_op and target_op != current_op_id:
                continue
            resp = extract_assistant_response(ev_payload, workspace=None)
            if resp:
                processed_responses.append(resp)

    # Only the worker's response should be extracted, NOT the parent's
    assert len(processed_responses) == 1
    assert processed_responses[0] == "Landing page updated and brand verified."
