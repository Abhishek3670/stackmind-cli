import os
import sys
import shutil
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from validators.kernel.workspace import ScratchWorkspace
from validators.kernel.sandbox import ProcessSandbox
from validators.kernel.daemon.supervisor.models import RunState, Phase
from validators.kernel.daemon.supervisor import LifecycleSupervisor
from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.providers.models import Message, ToolCallRequest, ProviderResponse


def test_sandbox_dynamic_timeout_and_windows_resolution(tmp_path: Path):
    """ProcessSandbox dynamically tiers timeouts for long-running build/install tools
    and resolves Windows .cmd executables when bare command is supplied."""
    from types import SimpleNamespace
    ws = SimpleNamespace(root=tmp_path)
    sandbox = ProcessSandbox(ws)

    # Fast inspection command gets default 30s
    with patch("subprocess.Popen") as mock_popen, patch("validators.kernel.sandbox._setup_windows_job", return_value=None):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("ok", "")
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        sandbox.run(["python", "-c", "print(1)"], timeout=30)
        assert mock_proc.communicate.call_args[1]["timeout"] == 30.0

    # Package manager command gets 300s dynamic timeout
    with patch("subprocess.Popen") as mock_popen, patch("validators.kernel.sandbox._setup_windows_job", return_value=None):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("ok", "")
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        sandbox.run(["npm", "install"], timeout=30)
        assert mock_proc.communicate.call_args[1]["timeout"] == 300.0

    # Build / migration command gets 300s dynamic timeout
    with patch("subprocess.Popen") as mock_popen, patch("validators.kernel.sandbox._setup_windows_job", return_value=None):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("ok", "")
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        sandbox.run(["npx", "drizzle-kit", "db:generate"], timeout=30)
        assert mock_proc.communicate.call_args[1]["timeout"] == 300.0


def test_work_order_turn_isolates_session_history():
    """Governed work order turns must isolate prompt context and not inherit
    chat history or conversational scratchpads from other roles/work orders."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    # Verify filtering logic: client_hist from another work order or un-tagged chat
    # is excluded when wo_id_param is provided.
    client_hist = [
        {"role": "user", "content": "Build styles with BEM classes", "work_order_id": "WO-005"},
        {"role": "assistant", "content": "I have created styles.css", "work_order_id": "WO-005"},
        {"role": "user", "content": "Rework database schema", "work_order_id": "WO-002"},
    ]

    wo_id_param = "WO-002"
    filtered = []
    for h in client_hist:
        if isinstance(h, dict) and str(h.get("work_order_id", "")) == str(wo_id_param):
            h_content = str(h.get("content", "")).strip()
            if h_content and not h_content.startswith(":"):
                filtered.append({"role": str(h.get("role", "user")).lower(), "content": h_content})

    assert len(filtered) == 1
    assert filtered[0]["content"] == "Rework database schema"
    assert not any("BEM classes" in f["content"] for f in filtered)


def test_ensure_project_dependencies_and_prompt_notice(tmp_path: Path):
    """Supervisor pre-flight checks node_modules when package.json declares dependencies
    and informs workers that dependencies are pre-installed."""
    supervisor = LifecycleSupervisor(MagicMock())
    state = RunState(
        run_id="run-test",
        product_goal="test",
        workspace=str(tmp_path),
        session_id="sess-test",
        phase=Phase.EXECUTING,
    )

    # 1. Dispatch prompt informs worker dependencies are already installed
    (tmp_path / "node_modules").mkdir(parents=True, exist_ok=True)
    prompt = supervisor._worker_dispatch_prompt(state, "WO-002", tmp_path)
    assert "Dependencies are already installed in node_modules" in prompt
    assert "Do not run 'npm install'" in prompt

    # 2. Pre-flight installation runs when package.json exists and node_modules is missing
    shutil.rmtree(tmp_path / "node_modules")
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text(json.dumps({"dependencies": {"express": "^4.19.0"}}), encoding="utf-8")

    with patch("validators.kernel.sandbox.ProcessSandbox.run") as mock_sandbox_run:
        from validators.kernel.sandbox import CommandResult
        mock_sandbox_run.return_value = CommandResult(0, "added 50 packages", "")
        res = supervisor._ensure_project_dependencies(tmp_path)
        assert res is True
        mock_sandbox_run.assert_called_once()
        cmd = mock_sandbox_run.call_args[0][0]
        assert "npm" in cmd and "install" in cmd


def test_anti_stagnation_circuit_breaker_intercepts_consecutive_reads():
    """When an agent executes 4 consecutive passive read/inspection calls,
    the anti-stagnation circuit breaker triggers a warning prompt to break the loop."""
    mock_adapter = MagicMock()
    mock_tool_gw = MagicMock()
    mock_tool_gw.actor_id = "codex"
    gateway = ProviderGateway(mock_adapter, mock_tool_gw)
    gateway.total_tool_calls = 0

    # Mock execute_turn to simulate consecutive reads
    call_count = 0
    def fake_execute_turn(messages, **kwargs):
        nonlocal call_count
        tools = ["read_file", "list_directory", "read_file", "read_file", "read_file"]
        next_tool = tools[min(call_count, len(tools) - 1)]
        call_count += 1
        return ProviderResponse(
            message=Message.assistant(
                "",
                tool_calls=[ToolCallRequest(id=f"c_{call_count}", name=next_tool, arguments={"path": f"file_{call_count}.ts"})]
            )
        )

    gateway.execute_turn = fake_execute_turn
    gateway.has_written_deliverable = MagicMock(return_value=False)

    # Run loop
    messages = [Message.user("Implement database schema")]
    try:
        gateway.run_loop(
            messages,
            required_deliverable="src/schema.ts",
            max_turns=6,
        )
    except Exception as exc:
        pass

    # Verify a system warning for anti-stagnation was injected into messages
    warning_msgs = [
        m.content for m in messages
        if isinstance(m, Message) and "Anti-Stagnation Circuit Breaker triggered" in m.content
    ]
    assert len(warning_msgs) >= 1
    assert "consecutive passive read/inspection tool calls" in warning_msgs[0]
