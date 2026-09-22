from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import click
from click.testing import CliRunner

from cli.tui.app import (
    dispatch_delivery_command,
    tui,
)
from cli.tui.chat import render_assistant_stream_header
from cli.tui.events import extract_text_delta
from cli.tui.state import AutonomousDeliveryState
from validators.kernel.tui.adapter import StackMindTuiAdapter
from validators.kernel.tui.client import DaemonClient


class DummySSEResponse:
    def __init__(self, lines: list[bytes]):
        self.lines = lines
        self.idx = 0

    def readline(self) -> bytes:
        if self.idx < len(self.lines):
            line = self.lines[self.idx]
            self.idx += 1
            return line
        return b""

    def close(self) -> None:
        pass

    def __enter__(self) -> DummySSEResponse:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


def test_daemon_client_stream_events_keepalive_and_deltas():
    client = DaemonClient(base_url="http://127.0.0.1:9999")
    sse_lines = [
        b": keepalive\n",
        b"\n",
        b"event: message\n",
        b'data: {"name": "stream.delta", "payload": {"delta": "Hello "}}\n',
        b"\n",
        b"event: message\n",
        b'data: {"name": "stream.delta", "payload": {"delta": "world!"}}\n',
        b"\n",
        b"event: message\n",
        b'data: {"name": "turn.finish", "payload": {"status": "ok"}}\n',
        b"\n",
    ]

    mock_resp = DummySSEResponse(sse_lines)
    with patch.object(client._opener, "open", return_value=mock_resp):
        events = list(client.stream_events(timeout=0.1))

    assert len(events) == 4
    assert events[0].get("_heartbeat") is True
    assert events[0].get("name") == "system.heartbeat"
    assert events[1]["name"] == "stream.delta"
    assert events[1]["payload"]["delta"] == "Hello "
    assert events[2]["name"] == "stream.delta"
    assert events[2]["payload"]["delta"] == "world!"
    assert events[3]["name"] == "turn.finish"


def test_tui_adapter_stream_live_mode():
    client = MagicMock(spec=DaemonClient)
    client.stream_events.return_value = iter([
        {"name": "system.heartbeat", "payload": {"status": "alive"}, "_heartbeat": True},
        {"name": "stream.delta", "payload": {"delta": "part1"}},
        {"name": "system.heartbeat", "payload": {"status": "alive"}, "_heartbeat": True},
        {"name": "turn.finish", "payload": {"status": "ok"}},
    ])
    adapter = StackMindTuiAdapter(client=client)

    # In live=True mode, heartbeats are passed through
    events = list(adapter.stream("sess-1", live=True))
    assert len(events) == 4
    assert events[0].get("_heartbeat") is True
    assert events[1]["name"] == "stream.delta"

    # In live=False mode, stops at first keepalive if events were empty, or after draining
    client.stream_events.return_value = iter([
        {"name": "op.started", "payload": {"op_id": "op-1"}},
        {"name": "system.heartbeat", "payload": {"status": "alive"}, "_heartbeat": True},
        {"name": "should.not.reach", "payload": {}},
    ])
    drained = list(adapter.stream("sess-1", live=False))
    assert len(drained) == 1
    assert drained[0]["name"] == "op.started"


def test_extract_text_delta():
    assert extract_text_delta({"name": "stream.delta", "payload": {"delta": "chunk1"}}) == "chunk1"
    assert extract_text_delta({"name": "token.delta", "payload": {"text": "chunk2"}}) == "chunk2"
    assert extract_text_delta({"name": "message.delta", "payload": {"content": "chunk3"}}) == "chunk3"
    assert extract_text_delta({"name": "assistant.delta", "payload": {"token": "chunk4"}}) == "chunk4"
    assert extract_text_delta({"name": "other.event", "payload": {}}) is None
    assert extract_text_delta(None) is None


def test_render_assistant_stream_header():
    header = render_assistant_stream_header(model="gpt-test", width=60)
    assert "gpt-test" in header
    assert "StackMind" in header


def test_keyboard_interrupt_graceful_cancellation():
    session = {"session_id": "sess-test"}
    state = AutonomousDeliveryState(session_id="sess-test", client_timeout=5.0)

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-to-cancel"}
    mock_adapter.stream.side_effect = KeyboardInterrupt()

    mock_client = MagicMock()
    mock_client.operation_cancel.return_value = {"status": "cancelled", "op_id": "op-to-cancel"}

    with patch("sys.stdout", new_callable=io.StringIO):
        _, should_exit = dispatch_delivery_command(
            mock_adapter,
            mock_client,
            session,
            "write code",
            state,
        )

    assert should_exit is False
    mock_client.operation_cancel.assert_called_once_with("op-to-cancel", cascade=True)
    assert any("cancelled" in m.content for m in state.messages)


def test_timeout_command_and_options():
    session = {"session_id": "sess-timeout"}
    state = AutonomousDeliveryState(session_id="sess-timeout", client_timeout=45.0)
    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # Test :timeout with valid number
    dispatch_delivery_command(mock_adapter, mock_client, session, ":timeout 60", state)
    assert state.client_timeout == 60.0
    assert any("Client timeout set to 60.0s" in m.content for m in state.messages)

    # Test :timeout query with no arguments
    dispatch_delivery_command(mock_adapter, mock_client, session, ":timeout", state)
    assert any("Current client timeout: 60.0s" in m.content for m in state.messages)

    # Test :timeout with invalid argument
    dispatch_delivery_command(mock_adapter, mock_client, session, ":timeout -5", state)
    assert any("Timeout must be a positive number" in m.content for m in state.messages)


def test_panel_aware_streaming_rate_limiting():
    """Verify rapid delta events are rate-limited to ~10 Hz and flushed on completion (WO-011)."""
    session = {"session_id": "sess-stream-1", "model": "claude-3-5-sonnet"}
    state = AutonomousDeliveryState(session_id="sess-stream-1", client_timeout=5.0)

    # 50 rapid deltas that arrive in a single batch
    deltas = [
        {"name": "stream.delta", "payload": {"delta": f"token_{i} "}}
        for i in range(50)
    ]
    completion = {"name": "turn.completed", "payload": {"operation_id": "op-stream"}}
    all_events = deltas + [completion]

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-stream"}
    mock_adapter.stream.return_value = iter(all_events)

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED"}

    mock_live = MagicMock()

    with patch("cli.tui.app.redraw_full_screen") as mock_redraw, \
         patch("click.echo") as mock_echo:
        dispatch_delivery_command(
            mock_adapter,
            mock_client,
            session,
            "write a function",
            state,
            live_manager=mock_live,
        )

        # Rate-limiting check: 50 rapid events in <100ms should only trigger
        # at most 2 redraws: initial delta gate + final post-stream flush
        assert mock_redraw.call_count <= 2
        assert mock_redraw.call_count >= 1

        # Verify arguments on the final redraw call:
        final_call = mock_redraw.call_args_list[-1]
        assert final_call.kwargs.get("clear") is False
        assert final_call.kwargs.get("include_composer") is False
        assert final_call.kwargs.get("live_manager") == mock_live

        # Verify assistant message has accumulated all 50 tokens
        assistant_msgs = [m for m in state.messages if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        for i in range(50):
            assert f"token_{i}" in assistant_msgs[0].content

        # Verify click.echo was NOT called for individual deltas
        echoed_text = " ".join(str(call.args[0]) for call in mock_echo.call_args_list if call.args)
        assert "token_0" not in echoed_text
        assert "token_49" not in echoed_text


def test_panel_aware_streaming_periodic_redraw_at_100ms_intervals():
    """Verify redraw_full_screen triggers when >=100ms elapses between deltas (WO-011)."""
    session = {"session_id": "sess-stream-2"}
    state = AutonomousDeliveryState(session_id="sess-stream-2", client_timeout=5.0)

    # Simulated monotonic timestamps:
    # 0: initial delta (t=0.0) -> triggers redraw
    # 1: rapid delta (t=0.03) -> throttled (<0.1s)
    # 2: rapid delta (t=0.07) -> throttled (<0.1s)
    # 3: gated delta (t=0.12) -> triggers redraw (>=0.1s)
    # 4: rapid delta (t=0.18) -> throttled (<0.1s)
    # 5: gated delta (t=0.25) -> triggers redraw (>=0.1s)
    timestamps = [0.0, 0.03, 0.07, 0.12, 0.18, 0.25, 0.26]
    time_iter = iter(timestamps)

    deltas = [
        {"name": "stream.delta", "payload": {"delta": f"chunk{i} "}}
        for i in range(len(timestamps) - 1)
    ]
    completion = {"name": "turn.completed", "payload": {"operation_id": "op-stream-2"}}
    all_events = deltas + [completion]

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-stream-2"}
    mock_adapter.stream.return_value = iter(all_events)

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED"}

    with patch("time.monotonic", side_effect=lambda: next(time_iter, 1.0)), \
         patch("cli.tui.app.redraw_full_screen") as mock_redraw:
        dispatch_delivery_command(
            mock_adapter,
            mock_client,
            session,
            "test interval streaming",
            state,
        )

        # Expected redraws:
        # 1. t=0.0 (initial delta)
        # 2. t=0.12 (delta 3)
        # 3. t=0.25 (delta 5)
        # 4. final completion redraw
        assert mock_redraw.call_count == 4

        # Verify all streaming redraws have include_composer=False and clear=False
        for call in mock_redraw.call_args_list:
            assert call.kwargs.get("clear") is False
            assert call.kwargs.get("include_composer") is False


def test_heartbeat_tick_suppression_during_active_streaming():
    """Verify heartbeat ticks do not corrupt active streaming output (WO-011 AC-7)."""
    session = {"session_id": "sess-stream-3"}
    state = AutonomousDeliveryState(session_id="sess-stream-3", client_timeout=5.0)

    events = [
        {"name": "stream.delta", "payload": {"delta": "Streaming output..."}},
        {"name": "system.heartbeat", "_heartbeat": True, "payload": {}},
        {"name": "system.heartbeat", "_heartbeat": True, "payload": {}},
        {"name": "stream.delta", "payload": {"delta": " more tokens"}},
        {"name": "turn.completed", "payload": {"operation_id": "op-stream-3"}},
    ]

    mock_adapter = MagicMock()
    mock_adapter.command.return_value = {"operation_id": "op-stream-3"}
    mock_adapter.stream.return_value = iter(events)

    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"status": "COMPLETED"}

    with patch("click.echo") as mock_echo:
        dispatch_delivery_command(
            mock_adapter,
            mock_client,
            session,
            "stream with heartbeats",
            state,
        )

        # Heartbeat ticks ('Working...') should NOT have been echoed during active streaming
        echoed = [str(call.args[0]) for call in mock_echo.call_args_list if call.args]
        assert not any("Working..." in msg for msg in echoed)
        assert not any("Streaming output" in msg for msg in echoed)

