"""Cooperative cancellation boundaries for the governed runner."""

from __future__ import annotations

from threading import Event

from validators.harness.runner import AgentRunner


def test_runner_stops_after_task_discovery_when_operation_is_cancelled(tmp_path):
    sync = tmp_path / ".sync"
    (sync / "runtime" / "boot").mkdir(parents=True)
    (sync / "inbox" / "codex").mkdir(parents=True)
    (sync / "outbox" / "codex").mkdir(parents=True)
    (sync / "agents").mkdir(parents=True)
    (sync / "runtime" / "TREE.yaml").write_text(
        "agents:\n  codex:\n    session_count: 0\n", encoding="utf-8"
    )
    (sync / "runtime" / "boot" / "codex.boot.yaml").write_text("agent: codex\n", encoding="utf-8")
    (sync / "agents" / "codex.agent.md").write_text("# Codex\n", encoding="utf-8")
    (sync / "inbox" / "codex" / "task.md").write_text("# Cancelled task\n", encoding="utf-8")

    cancellation = Event()
    cancellation.set()
    result = AgentRunner(tmp_path, "codex").run_once(cancellation, "operation-1")

    assert result.status == "cancelled"
    assert result.persisted is False
    assert result.reason == "operation cancelled"
    assert result.meta == {"operation_id": "operation-1"}
