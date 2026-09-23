"""Tests for auto-scaffolding protocol citizenship and conversational adhoc turns (WO-037)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from cli.tui.app import dispatch_delivery_command
from cli.tui.state import AutonomousDeliveryState
from validators.harness.runner import (
    AgentRunner,
    CompletionRecord,
    HarnessTask,
    LLMRequest,
)
from validators.kernel.daemon.manager import (
    SessionManager,
    _scaffold_protocol_citizenship,
)
from validators.kernel.daemon.storage import DaemonStorage


class DummyChatProvider:
    """Mock LLM provider returning a pure conversational response."""

    provider_name = "mock-ollama"
    model_name = "qwen2.5-coder:7b"

    def __init__(self, response_text: str = "Hello! I am ready to help.") -> None:
        self.response_text = response_text

    def complete(self, request: LLMRequest) -> CompletionRecord:
        return CompletionRecord(
            provider=self.provider_name,
            model=self.model_name,
            payload={
                "status": "completed",
                "summary": self.response_text,
                "report_markdown": f"# Response\n\n{self.response_text}\n",
                "blockers": [],
                "modified_files": [],
                "commands": [],
                "retrieval_queries": [],
                "uncertainty": [],
            },
        )


def test_scaffold_protocol_citizenship_in_empty_workspace(tmp_path: Path):
    """Test that _scaffold_protocol_citizenship creates all required protocol files."""
    ws = tmp_path / "fresh-workspace"
    ws.mkdir()

    _scaffold_protocol_citizenship(ws, "codex")

    sync = ws / ".sync"
    tree_file = sync / "runtime" / "TREE.yaml"
    boot_file = sync / "runtime" / "boot" / "codex.boot.yaml"
    agent_file = sync / "agents" / "codex.agent.md"
    inbox_read = sync / "inbox" / "codex" / "_read"
    outbox = sync / "outbox" / "codex"

    assert tree_file.is_file()
    assert boot_file.is_file()
    assert agent_file.is_file()
    assert inbox_read.is_dir()
    assert outbox.is_dir()

    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assert "codex" in tree_data["agents"]
    assert tree_data["schema_version"] == 1

    boot_data = yaml.safe_load(boot_file.read_text(encoding="utf-8"))
    assert boot_data["agent"] == "codex"
    assert boot_data["schema_version"] == 1


def test_agent_runner_in_fresh_workspace_auto_scaffolds_and_runs_adhoc(tmp_path: Path):
    """Test that AgentRunner runs successfully in a completely uninitialized workspace."""
    ws = tmp_path / "fresh-workspace"
    ws.mkdir()

    # Workspace has no .sync/ directory at all!
    provider = DummyChatProvider("I am running smoothly in an uninitialized directory.")
    runner = AgentRunner(ws, "codex", llm_provider=provider)

    result = runner.run_once(prompt="Hey StackMind!")

    assert result.status == "completed"
    assert result.persisted is True
    assert result.report_path is not None
    assert result.report_path.exists()
    assert "I am running smoothly" in result.report_path.read_text(encoding="utf-8")
    assert result.meta is not None
    assert "I am running smoothly" in result.meta["summary"]

    # Verify protocol files were created
    assert (ws / ".sync" / "runtime" / "TREE.yaml").exists()
    assert (ws / ".sync" / "runtime" / "boot" / "codex.boot.yaml").exists()
    assert (ws / ".sync" / "agents" / "codex.agent.md").exists()


def test_session_manager_run_turn_descriptive_error_reporting(tmp_path: Path):
    """Test that manager._run_turn captures and records detailed error messages."""
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("codex", "test", {}, str(tmp_path))
    session_id = session["session_id"]

    # Set up a runner factory that raises a custom exception with a clear message
    def faulty_runner(ws: str, agent: str):
        runner = MagicMock()
        runner.run_once.side_effect = RuntimeError("Failed to connect to local Ollama daemon on port 11434")
        return runner

    manager._runner_factory = faulty_runner

    op = manager.start_turn(session_id, "Hello")
    op_id = op["operation_id"]

    thread = manager._turn_threads.get(op_id)
    if thread:
        thread.join(timeout=3.0)

    op_rec = manager.get_operation(op_id)
    assert op_rec["status"] == "FAILED"
    result = op_rec.get("result") or {}
    assert result.get("type") == "RuntimeError"
    assert "Failed to connect to local Ollama daemon on port 11434" in str(result.get("message"))
    assert "Failed to connect to local Ollama daemon on port 11434" in str(result.get("error"))


def test_tui_renders_descriptive_error_on_operation_failure(capsys):
    """Test that TUI renders an error box with the actual error message when turn fails."""
    mock_adapter = MagicMock(spec=StackMindTuiAdapter)
    mock_client = MagicMock(spec=DaemonClient)

    mock_adapter.command.return_value = {"operation_id": "op-fail-123"}
    mock_adapter.stream.return_value = iter([])

    mock_client.operation_get.return_value = {
        "operation_id": "op-fail-123",
        "status": "FAILED",
        "result": {
            "type": "ValueError",
            "message": "Agent 'codex' is not protocol-registered (codex.boot.yaml missing)",
            "error": "Agent 'codex' is not protocol-registered (codex.boot.yaml missing)",
        },
    }

    session = {"session_id": "sess-test", "workspace": "/tmp"}
    state = AutonomousDeliveryState(session_id="sess-test")

    session, should_exit = dispatch_delivery_command(
        mock_adapter,
        mock_client,
        session,
        "Explain the repo",
        state,
    )

    captured = capsys.readouterr().out
    assert "OPERATION FAILED" in captured
    assert "Agent 'codex' is not protocol-registered" in captured
    assert "Turn operation op-fail-123 completed" not in captured
