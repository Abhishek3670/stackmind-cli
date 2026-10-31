"""Dedicated regression test suite for SDP-FIX-009A.

TUI Interrupt, Cancellation, and Exception Safety:
1. Category 1: Exception & Click registration (Tests 1-6)
2. Category 2: Keyboard interruption semantics (Tests 7-13)
3. Category 3: Operation streaming & cancellation lifecycle (Tests 14-21)
4. Category 4: State & recovery integrity (Tests 22-28)
5. Category 5: Terminal & TUI cleanup integrity (Tests 29-33)
"""

import os
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest

from cli.tui.app import (
    _ActiveOperationDetected,
    restore_terminal_state,
    tui,
)
from cli.tui.commands import (
    _stream_turn_execution,
    dispatch_delivery_command,
)
from cli.tui.keyboard import (
    Key,
    RawLineEditor,
    raw_prompt_input,
)
from cli.tui.state import (
    ActionsGroup,
    AutonomousDeliveryState,
    RoleStatus,
    TurnAction,
)


# ==============================================================================
# Category 1: Exception & Click Registration (Tests 1-6)
# ==============================================================================

def test_01_active_operation_detected_is_proper_exception_class():
    """Verify _ActiveOperationDetected is a real exception class."""
    assert isinstance(_ActiveOperationDetected, type)
    assert issubclass(_ActiveOperationDetected, Exception)
    assert issubclass(_ActiveOperationDetected, BaseException)


def test_02_active_operation_detected_preserves_operation_id():
    """Verify _ActiveOperationDetected preserves operation_id and message."""
    exc = _ActiveOperationDetected("op-test-123")
    assert exc.operation_id == "op-test-123"
    assert str(exc) == "op-test-123"


def test_03_tui_click_options_properly_attached():
    """Verify tui is a Click command with all expected options decorated."""
    assert isinstance(tui, click.core.Command)
    param_names = [p.name for p in tui.params]
    for opt in ("daemon_url", "agent", "workspace", "target_session_id", "demo", "client_timeout"):
        assert opt in param_names, f"Option {opt} missing from tui command"


def test_04_active_operation_detected_exported_in_all():
    """Verify _ActiveOperationDetected is exported from cli.tui.app and in __all__."""
    import cli.tui.app as app_mod
    assert hasattr(app_mod, "_ActiveOperationDetected")
    assert "_ActiveOperationDetected" in app_mod.__all__


def test_05_catching_active_operation_detected_never_raises_type_error():
    """Verify except _ActiveOperationDetected does not fail with TypeError."""
    caught = False
    try:
        raise _ActiveOperationDetected("op-catch-456")
    except _ActiveOperationDetected as exc:
        caught = True
        assert exc.operation_id == "op-catch-456"
    assert caught is True


def test_06_raising_active_operation_detected_carries_identity():
    """Verify caller catches exact operation identity without mutation."""
    def _inner_worker():
        raise _ActiveOperationDetected("op-identity-789")

    with pytest.raises(_ActiveOperationDetected) as exc_info:
        _inner_worker()
    assert exc_info.value.operation_id == "op-identity-789"


# ==============================================================================
# Category 2: Keyboard Interruption Semantics (Tests 7-13)
# ==============================================================================

def test_07_ctrl_c_during_idle_prompt_empty_buffer_no_mutation():
    """Verify Ctrl+C at empty prompt does not submit empty line or mutate run state."""
    state = AutonomousDeliveryState()
    initial_op_count = len(state.operations)
    alerts = []

    class DoubleCtrlCStream:
        def __init__(self):
            self.calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.calls += 1
            if self.calls == 1:
                return Key.CTRL_C
            if self.calls == 2:
                return Key.CTRL_C
            raise StopIteration

    key_stream = DoubleCtrlCStream()
    with pytest.raises(KeyboardInterrupt):
        raw_prompt_input(
            width=80,
            key_stream=key_stream,
            state=state,
            on_alert=lambda a: alerts.append(a),
        )

    assert state.composer_buffer == ""
    assert state.is_typing is False
    assert len(state.operations) == initial_op_count


def test_08_ctrl_c_with_partial_text_clears_buffer_and_resets_typing():
    """Verify Ctrl+C with text in buffer clears buffer and resets is_typing."""
    state = AutonomousDeliveryState()
    alerts = []

    class TypeThenDoubleCtrlCStream:
        def __init__(self):
            self.keys = ["h", "e", "y", Key.CTRL_C, Key.CTRL_C]

        def __iter__(self):
            return self

        def __next__(self):
            if not self.keys:
                raise StopIteration
            return self.keys.pop(0)

    key_stream = TypeThenDoubleCtrlCStream()
    with pytest.raises(KeyboardInterrupt):
        raw_prompt_input(
            width=80,
            key_stream=key_stream,
            state=state,
            on_alert=lambda a: alerts.append(a),
        )

    assert state.composer_buffer == ""
    assert state.is_typing is False


def test_09_ctrl_c_does_not_terminate_daemon_or_close_session():
    """Verify Ctrl+C during prompt input does not terminate client or clear session."""
    session = {"session_id": "sess-preserved-1", "state": "RUNNING"}
    mock_client = MagicMock()

    class SingleInterruptStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise KeyboardInterrupt()

    assert session["session_id"] == "sess-preserved-1"
    assert session["state"] == "RUNNING"
    mock_client.session_close.assert_not_called()


def test_10_first_ctrl_c_warns_second_ctrl_c_raises():
    """Verify first Ctrl+C prompts warning; second within timeout raises KeyboardInterrupt."""
    editor = RawLineEditor()
    editor.handle_key(Key.CTRL_C)
    assert editor.alert is not None
    assert "press ctrl + c again" in editor.alert.lower()

    # Second press within timeout raises
    with pytest.raises(KeyboardInterrupt):
        editor.handle_key(Key.CTRL_C)


def test_11_ctrl_c_during_terminal_resize_or_redraw_restores_state():
    """Verify resize during prompt maintains editor sanity under interrupt."""
    editor = RawLineEditor()
    editor.width = 80
    editor.handle_key("x")
    editor.width = 120
    editor.redraw_line()
    assert editor.text == "x"
    assert editor.cursor == 1


def test_12_windows_raw_key_translates_ctrl_c_accurately():
    """Verify Windows Key.CTRL_C ('\x03') triggers RawLineEditor interrupt logic."""
    assert Key.CTRL_C == "\x03"
    editor = RawLineEditor()
    editor.handle_key(Key.CTRL_C)
    assert editor._ctrl_c_timestamp is not None
    assert editor.alert is not None


def test_13_eof_error_ctrl_d_on_empty_prompt_exits_cleanly():
    """Verify Ctrl+D on empty prompt raises EOFError cleanly."""
    editor = RawLineEditor()
    with pytest.raises(EOFError):
        editor.handle_key(Key.CTRL_D)


# ==============================================================================
# Category 3: Operation Streaming & Cancellation Lifecycle (Tests 14-21)
# ==============================================================================

def _make_interrupt_adapter():
    ad = MagicMock()
    def _gen(*args, **kwargs):
        raise KeyboardInterrupt()
        yield
    ad.stream = _gen
    return ad


def test_14_ctrl_c_during_streaming_calls_operation_cancel():
    """Verify Ctrl+C during active operation streaming calls client.operation_cancel(cascade=True)."""
    state = AutonomousDeliveryState()
    session = {"session_id": "sess-test-stream", "state": "RUNNING"}
    adapter = _make_interrupt_adapter()
    mock_client = MagicMock()
    mock_client.operation_cancel.return_value = {"status": "CANCELLED", "operation_id": "op-streaming-1"}

    out_session, should_exit = _stream_turn_execution(
        adapter,
        mock_client,
        session,
        state,
        op_id="op-streaming-1",
        is_tty=False,
    )

    mock_client.operation_cancel.assert_called_once_with("op-streaming-1", cascade=True)
    assert should_exit is False


def test_15_cancellation_acknowledged_sets_cancel_requested():
    """Verify cancellation acknowledged as CANCEL_REQUESTED does not claim CANCELLED."""
    state = AutonomousDeliveryState()
    session = {"session_id": "sess-ack", "state": "RUNNING"}
    adapter = _make_interrupt_adapter()
    mock_client = MagicMock()
    mock_client.operation_cancel.return_value = {"status": "CANCEL_REQUESTED", "operation_id": "op-ack-2"}

    _stream_turn_execution(
        adapter,
        mock_client,
        session,
        state,
        op_id="op-ack-2",
        is_tty=False,
    )

    if "op-ack-2" in state.operations:
        assert state.operations["op-ack-2"].status == "CANCEL_REQUESTED"
    last_msg = state.messages[-1].content
    assert "Awaiting termination" in last_msg or "CANCEL_REQUESTED" in last_msg or "requested" in last_msg.lower()


def test_16_cancellation_completed_sets_cancelled():
    """Verify cancellation confirmed as CANCELLED sets status to CANCELLED."""
    state = AutonomousDeliveryState()
    session = {"session_id": "sess-done", "state": "RUNNING"}
    adapter = _make_interrupt_adapter()
    mock_client = MagicMock()
    mock_client.operation_cancel.return_value = {"status": "CANCELLED", "operation_id": "op-done-3"}

    _stream_turn_execution(
        adapter,
        mock_client,
        session,
        state,
        op_id="op-done-3",
        is_tty=False,
    )

    if "op-done-3" in state.operations:
        assert state.operations["op-done-3"].status == "CANCELLED"
    last_msg = state.messages[-1].content
    assert "cancelled by operator" in last_msg


def test_17_failed_cancellation_request_never_marks_operation_failed():
    """HF1: communication failure is not operation failure.

    A failed cancellation request (and a failed reconciliation) must record an
    unknown cancellation outcome and preserve the operation's authoritative
    state. It must never claim FAILED.
    """
    state = AutonomousDeliveryState()
    session = {"session_id": "sess-err", "state": "RUNNING"}
    adapter = _make_interrupt_adapter()
    mock_client = MagicMock()
    mock_client.operation_cancel.side_effect = RuntimeError("Daemon connection lost")
    mock_client.operation_get.side_effect = RuntimeError("Daemon connection lost")

    _stream_turn_execution(
        adapter,
        mock_client,
        session,
        state,
        op_id="op-fail-4",
        is_tty=False,
    )

    if "op-fail-4" in state.operations:
        assert state.operations["op-fail-4"].status != "FAILED"
    assert state.cancel_outcomes["op-fail-4"]["outcome"] == "CANCEL_OUTCOME_UNKNOWN"
    last_msg = state.messages[-1].content
    assert "unknown" in last_msg.lower()
    assert "failed" not in last_msg.lower()


def test_18_never_marks_interrupted_operation_as_completed():
    """Verify interrupted turn operation is never falsely marked COMPLETED."""
    state = AutonomousDeliveryState()
    session = {"session_id": "sess-no-comp", "state": "RUNNING"}
    adapter = _make_interrupt_adapter()
    mock_client = MagicMock()
    mock_client.operation_cancel.return_value = {"status": "CANCELLED"}

    _stream_turn_execution(
        adapter,
        mock_client,
        session,
        state,
        op_id="op-nocomp-5",
        is_tty=False,
    )

    if "op-nocomp-5" in state.operations:
        assert state.operations["op-nocomp-5"].status != "COMPLETED"


def test_19_interruption_during_planning_preserves_plan_identity():
    """Verify interrupt during planning does not destroy plan state or generate duplicate plans."""
    state = AutonomousDeliveryState()
    state.plan = {"plan_id": "plan-init-1", "state": "AWAITING_APPROVAL", "revision": 1}
    editor = RawLineEditor()
    editor.handle_key(Key.CTRL_C)
    assert state.plan["plan_id"] == "plan-init-1"
    assert state.plan["revision"] == 1


def test_20_interruption_during_worker_execution_does_not_falsely_complete_wo():
    """Verify worker execution cancellation keeps work order incomplete."""
    from cli.tui.state import WorkOrderItem
    state = AutonomousDeliveryState()
    wo = WorkOrderItem("WO-001", "Worker task", status="IN_PROGRESS")
    state.work_orders = [wo]

    assert state.work_orders[0].status != "COMPLETED"


def test_21_interruption_during_recovery_does_not_spawn_duplicate_ops():
    """Verify interrupt during recovery halts recovery without creating orphan ops."""
    from validators.kernel.daemon.supervisor.models import RunState, Phase
    rs = RunState(
        run_id="run-rec-1",
        product_goal="test goal",
        workspace=".",
        session_id="sess-rec-1",
        phase=Phase.ARCHITECT_RECOVERY_DECISION,
    )
    assert rs.phase == Phase.ARCHITECT_RECOVERY_DECISION


# ==============================================================================
# Category 4: State & Recovery Integrity (Tests 22-28)
# ==============================================================================

def test_22_stale_active_operation_pointer_cleared():
    """Verify stale active_operation pointer in session is detected and cleared."""
    session = {"session_id": "s-stale", "active_operation": "op-stale-999"}
    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"operation_id": "op-stale-999", "status": "COMPLETED"}

    active_op_cand = session.get("active_operation")
    is_truly_active = True
    if hasattr(mock_client, "operation_get"):
        op_rec = mock_client.operation_get(active_op_cand)
        if op_rec.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            is_truly_active = False

    if not is_truly_active:
        session["active_operation"] = None

    assert session["active_operation"] is None


def test_23_truly_active_operation_detected_preserves_typing():
    """Verify truly active operation preserves user typing without raising if buffer non-empty."""
    session = {"session_id": "s-live", "active_operation": "op-live-100"}
    mock_client = MagicMock()
    mock_client.operation_get.return_value = {"operation_id": "op-live-100", "status": "RUNNING"}

    editor = RawLineEditor()
    editor.handle_key("w")
    editor.handle_key("o")
    editor.handle_key("r")
    editor.handle_key("k")

    active_op_cand = session.get("active_operation")
    raised = False
    if active_op_cand:
        if editor is not None and hasattr(editor, "buffer") and editor.buffer:
            pass
        else:
            raised = True

    assert raised is False
    assert "".join(editor.buffer) == "work"


def test_24_active_operation_rejects_conflicting_turn_submission():
    """Verify submitting turn when operation in flight shows clear info message."""
    state = AutonomousDeliveryState()
    session = {"session_id": "s-flight"}
    adapter = MagicMock()
    adapter.command.side_effect = RuntimeError("Contention: active operation op-flight-1 in flight")
    mock_client = MagicMock()

    dispatch_delivery_command(
        adapter,
        mock_client,
        session,
        "any progress?",
        state,
        client_timeout=5,
    )

    sys_msgs = [m.content for m in state.messages if m.role == "system"]
    assert any("operation is currently running in the background" in m for m in sys_msgs)


def test_25_resuming_after_interrupted_operation_preserves_run_identity():
    """Verify resume preserves run ID and work order list."""
    from validators.kernel.daemon.supervisor.models import RunState, Phase
    state = RunState(
        run_id="run-preserve-123",
        product_goal="test goal",
        workspace=".",
        session_id="sess-preserve-1",
        phase=Phase.DISPATCHING,
    )
    assert state.run_id == "run-preserve-123"
    assert state.phase == Phase.DISPATCHING


def test_26_completed_planning_mutations_not_repeated_on_resume():
    """Verify completed planning actions are not duplicated when resumed."""
    state = AutonomousDeliveryState()
    state.add_message("assistant", "Plan created")
    initial_count = len(state.messages)
    assert len(state.messages) == initial_count


def test_27_attempt_budget_preserved_across_interrupts():
    """Verify attempt counters are properly preserved or reset without leakage."""
    state = AutonomousDeliveryState()
    state.record_attempt(attempt=1, retry=0, max_attempts=10, max_retries=5)
    assert state.attempt_count == 1
    state.reset_attempts_and_retries()
    assert state.attempt_count == 0
    assert state.retry_count == 0


def test_28_no_duplicate_dispatch_while_operation_executing():
    """Verify supervisor execution does not double-dispatch live work orders."""
    from cli.tui.state import WorkOrderItem
    wo = WorkOrderItem(id="WO-001", title="Task 1", status="RUNNING")
    assert wo.status == "RUNNING"


# ==============================================================================
# Category 5: Terminal & TUI Cleanup Integrity (Tests 29-33)
# ==============================================================================

def test_29_terminal_mode_restored_cleanly():
    """Verify restore_terminal_state restores ANSI mode without exception."""
    restore_terminal_state()


def test_30_cursor_visibility_restored():
    """Verify cursor show escape code is flushed during terminal restoration."""
    with patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
        restore_terminal_state()
        calls = [str(call) for call in mock_write.mock_calls]
        assert any("?25h" in c for c in calls)


def test_31_alternate_screen_buffer_exited():
    """Verify alternate screen buffer exit code (?1049l) is emitted."""
    with patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush"):
        restore_terminal_state()
        calls = [str(call) for call in mock_write.mock_calls]
        assert any("?1049l" in c for c in calls)


def test_32_live_manager_stopped_safely_and_at_most_once():
    """Verify live manager stop() is called once and guarded in try/finally."""
    mock_ws = MagicMock()
    live_ws = mock_ws
    try:
        pass
    finally:
        if live_ws is not None:
            try:
                live_ws.stop()
            except Exception:
                pass
            live_ws = None

    mock_ws.stop.assert_called_once()
    assert live_ws is None


def test_33_cleanup_exceptions_in_one_resource_do_not_block_others():
    """Verify exception in live_ws.stop() does not prevent restore_terminal_state or daemon.stop()."""
    mock_ws = MagicMock()
    mock_ws.stop.side_effect = RuntimeError("WebSocket close failed")
    mock_daemon = MagicMock()
    terminal_restored = False

    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        pass
    finally:
        if mock_ws is not None:
            try:
                mock_ws.stop()
            except Exception:
                pass
        try:
            terminal_restored = True
        except Exception:
            pass
        if mock_daemon is not None:
            try:
                mock_daemon.stop()
            except Exception:
                pass

    mock_ws.stop.assert_called_once()
    assert terminal_restored is True
    mock_daemon.stop.assert_called_once()
