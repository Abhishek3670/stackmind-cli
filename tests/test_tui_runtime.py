from __future__ import annotations

from pathlib import Path

import pytest

from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import (
    DaemonClient,
    StackMindTuiAdapter,
    activity_line,
    diff_viewer,
    hitl_prompt,
    verification_matrix,
)


def session_params(tmp_path: Path) -> dict:
    return {
        "agent": "codex",
        "provider": "test",
        "contract": {"scope": {}},
        "workspace": str(tmp_path),
    }


def test_tui_daemon_connection_and_lifecycle(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        adapter = StackMindTuiAdapter(DaemonClient(daemon.url))
        session = adapter.command(":new", **session_params(tmp_path))
        assert adapter.command(f":pause {session['session_id']}")["state"] == "PAUSED"
        assert adapter.client.resume(session["session_id"])["state"] == "RUNNING"
        assert adapter.command(f":cancel {session['session_id']}")["state"] == "CANCELLED"


def test_tui_event_streaming_and_reconnect_cursor(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(**session_params(tmp_path))
        adapter = StackMindTuiAdapter(client)
        first = list(adapter.stream(session["session_id"]))
        assert [event["name"] for event in first][:2] == ["session.started", "attempt.started"]
        daemon.manager.events.publish("provider.token", session["session_id"], delta="hello")
        assert [event["payload"]["delta"] for event in adapter.stream(session["session_id"])] == [
            "hello"
        ]


def test_tui_hitl_approval_flow_is_daemon_recorded(tmp_path):
    with LocalDaemon(tmp_path / "daemon") as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(**session_params(tmp_path))
        adapter = StackMindTuiAdapter(client)
        assert (
            adapter.decide(session["session_id"], True, "reviewed")["journal"][-1]["status"]
            == "APPROVED"
        )
        assert adapter.decide(session["session_id"], False)["journal"][-1]["status"] == "REJECTED"
        assert "[Approve] [Reject]" in hitl_prompt("write_file", "tests/a.py")


def test_tui_verification_and_diff_presentation():
    dimensions = {
        name: True for name in ("scope", "state", "ast", "behavioral", "security", "outcome")
    }
    assert "PASS" in verification_matrix(dimensions)
    assert diff_viewer("--- a/file\n+++ b/file") == "--- a/file\n+++ b/file"
    assert "✓ Allowed" in activity_line({"name": "operation.started", "payload": {}})


def test_tui_runtime_boundary_isolation(tmp_path):
    adapter = StackMindTuiAdapter(DaemonClient("http://127.0.0.1:1"))
    with pytest.raises(ValueError, match="runtime-owned"):
        adapter.command("fix the issue")
    assert not hasattr(adapter, "run_command") and not hasattr(adapter.client, "read_file")
