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
