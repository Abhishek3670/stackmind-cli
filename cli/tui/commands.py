"""Autonomous Delivery Command Dispatcher and REPL Execution for StackMind TUI.

Handles interactive colon commands (:plan, :approve, :reject, :roles, :rebind,
:tree, :wo, :completion, :diff, :events, :status, :help, etc.), prompt turns,
and streaming event dispatch.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import click
from rich.text import Text

from cli.tui.chat import (
    clear_transcript_cache,
    render_actions_group_str,
    render_assistant_message_str,
    render_assistant_stream_header,
    render_chat_transcript_str,
    render_user_message_str,
)
from cli.tui.diff import (
    render_unified_diff_str,
)
from cli.tui.events import (
    extract_assistant_response,
    extract_assistant_thinking,
    extract_reasoning_delta,
    extract_text_delta,
    is_connection_error,
    recover_transcript_from_events,
    render_connection_status_str,
    render_error_box_str,
    render_operational_event_str,
)
from cli.tui.governance import (
    render_consume_skills_help_str,
    render_consumed_skills_summary_str,
    render_contract_hud_str,
    render_plan_panel_str,
    render_projects_panel_str,
    render_skills_panel_str,
    render_verification_matrix_str,
)
from cli.tui.landing import (
    render_landing_block_str,
)
from cli.tui.layout import (
    compute_layout,
    compute_viewport_height,
    render_runtime_panel_str,
)
from cli.tui.state import (
    AutonomousDeliveryState,
    OperationNode,
    ProjectPhase,
)
from cli.tui.terminal import (
    _wrap_text_to_width,
    check_terminal_resize,
    disable_mouse_reporting,
    install_resize_handler,
    redraw_full_screen as _terminal_redraw_full_screen,
    render_top_header_bar_str,
    restore_composer_focus,
)


def redraw_full_screen(*args: Any, **kwargs: Any) -> Any:
    app_mod = sys.modules.get("cli.tui.app")
    fn = getattr(app_mod, "redraw_full_screen", None) if app_mod is not None else None
    if fn is not None and fn is not redraw_full_screen and fn is not _terminal_redraw_full_screen:
        return fn(*args, **kwargs)
    return _terminal_redraw_full_screen(*args, **kwargs)


def format_duration_human(seconds: float) -> str:
    """Format duration from seconds to human readable units transitioning from sec to min to hr.

    Examples:
    - < 60s: '14.2s', '1.2s'
    - 60s <= d < 3600s: '33m 17s', '36m 22s'
    - d >= 3600s: '1h 2m 5s'
    """
    sec = max(0.0, float(seconds))
    if sec < 60.0:
        return f"{sec:.1f}s"
    total_seconds = int(sec)
    minutes, rem_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rem_seconds}s"
    hours, rem_minutes = divmod(minutes, 60)
    return f"{hours}h {rem_minutes}m {rem_seconds}s"


from cli.tui.views import (
    render_completion_surface,
    render_operation_tree,
    render_roles_panel,
    render_work_orders_panel,
)
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


def reconnect_and_sync(
    client: DaemonClient,
    adapter: StackMindTuiAdapter,
    state: AutonomousDeliveryState,
    session: dict[str, Any],
    workspace: Path | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Attempt reconnection to daemon and replay missed events without duplication."""
    state.connection_status = "reconnecting"
    sid = session.get("session_id")
    try:
        if sid:
            updated_session = client.get_session(sid)
            session.update(updated_session)
            state.update_from_session(updated_session)
        else:
            client.health_version()

        state.connection_status = "online"
        missed: list[dict[str, Any]] = []
        if sid:
            missed = client.events(sid, after=state.last_sequence)
            recover_transcript_from_events(missed, state, workspace=workspace)
        return True, missed
    except Exception:
        state.connection_status = "offline"
        return False, []


def _compute_run_state(state: AutonomousDeliveryState) -> str:
    """Derive current run state from phase and work order activity."""
    phase = state.phase
    if phase == ProjectPhase.PROJECT_COMPLETE:
        return "complete"
    if phase == ProjectPhase.FAILED:
        return "failed"
    if phase == ProjectPhase.BLOCKED:
        return "blocked"
    if phase == ProjectPhase.PLAN_REJECTED:
        return "rejected"
    if phase in (ProjectPhase.AWAITING_APPROVAL, ProjectPhase.PLAN_PROPOSED):
        return "awaiting_approval"
    if phase in (ProjectPhase.INITIALIZING, ProjectPhase.PLAN_APPROVED):
        return "planning"
    if phase == ProjectPhase.AUTONOMOUS_EXECUTION:
        if state.work_orders and any(
            wo.status.upper() in {"RUNNING", "ACTIVE", "IN_PROGRESS"} for wo in state.work_orders
        ):
            return "active"
        return "idle"
    return "idle"


def _compute_progress(state: AutonomousDeliveryState) -> tuple[int, int]:
    """Count completed and total work orders for progress reporting."""
    if not state.work_orders:
        return (0, 0)
    total = len(state.work_orders)
    completed = sum(
        1 for wo in state.work_orders if wo.status.upper() in {"COMPLETED", "DONE", "SUCCESS"}
    )
    return (completed, total)


def _compute_blocker(state: AutonomousDeliveryState) -> str:
    """Extract current blocker message if any."""
    if state.error:
        return state.error
    # Check for blocked work orders
    blocked_wos = [wo.id for wo in state.work_orders if wo.status.upper() == "BLOCKED"]
    if blocked_wos:
        return f"Blocked WOs: {', '.join(blocked_wos)}"
    return ""


def _compute_next_action(state: AutonomousDeliveryState) -> str:
    """Determine next valid operator action based on current state."""
    run_state = _compute_run_state(state)
    if run_state == "awaiting_approval":
        return "Approve or reject pending plan (:approve/:reject)"
    if run_state == "blocked":
        return "Review and resolve blocker, then :run"
    if run_state == "failed":
        return "Review error, then :run or :cancel"
    if run_state == "rejected":
        return "Revise plan and resubmit"
    if run_state == "complete":
        return "Project delivered; use :wo or :activity for details"
    return "Waiting for work..."


def get_status_str(
    session: dict[str, Any],
    state: AutonomousDeliveryState | None = None,
    width: int = 80,
) -> str:
    """Build compact, state-aware status summary for :status command."""
    if state is not None:
        state.update_from_session(session)

    parts = []

    if state is None:
        sid = session.get("session_id")
        if sid:
            parts.append(f"Session: {sid}")

    if state is not None:
        run_state = _compute_run_state(state)
        progress_done, progress_total = _compute_progress(state)
        blocker = _compute_blocker(state)
        next_action = _compute_next_action(state)

        # Status line
        parts.append(f"Status: {run_state}")

        # Execution Profile
        prof = getattr(state, "execution_profile", None) or session.get("execution_profile")
        if not prof and session.get("workspace"):
            try:
                from validators.kernel.settings import load_settings
                prof = load_settings(session["workspace"]).execution_profile
            except Exception:
                prof = "medium"
        parts.append(f"Execution Profile: {(prof or 'medium').upper()}")

        # Progress (only if there are work orders)
        if progress_total > 0:
            parts.append(f"Progress: {progress_done}/{progress_total} WOs completed")

        # Plan approval state
        if state.phase in (ProjectPhase.AWAITING_APPROVAL, ProjectPhase.PLAN_PROPOSED):
            parts.append("Plan: awaiting approval")
        elif state.phase == ProjectPhase.PLAN_REJECTED:
            parts.append("Plan: rejected")

        # Blocker (only if present)
        if blocker:
            parts.append(f"Blocker: {blocker}")

        # Next action
        parts.append(f"Next: {next_action}")

    return "\n".join(parts)


def _show_status(
    session: dict[str, Any],
    state: AutonomousDeliveryState | None = None,
    width: int = 80,
) -> str:
    out = get_status_str(session, state=state, width=width)
    click.echo(out)
    return out


def get_help_str() -> str:
    """Return available commands and shortcuts help text."""
    return (
        "Available commands:\n"
        "  :status           Display current Session, Contract HUD, and Project Delivery View\n"
        "  :contract         Display Contract Boundary HUD and write permissions\n"
        "  :reconnect        Reconnect to daemon and replay missed events\n"
        "  :roles            Interactive Agent Roles & Execution Backend bindings panel (preferred)\n"
        "  :rebind [r] [b]   [DEPRECATED] Rebind agent role to backend (use :roles instead)\n"
        "  :config           StackMind system settings, execution profiles, governance & policies\n"
        "  :provider         Configure cloud and local LLM providers, API keys & endpoints (alias: :providers)\n"
        "  :providers        Alias for :provider\n"
        "  :wo               Display Work Orders table and progress\n"
        "  :tree, :agents    Display Hierarchical Operation Tree\n"
        "  :plan             Display Plan Approval surface and revision history\n"
        "  :skills [name]    Display available procedural skills & verified runbooks\n"
        "  :consume-skills <p> Ingest and install procedural skills from file or directory\n"
        "  :attach <p>       Upload and ingest documents (.pdf, .docx, .md), images, or archives (.zip)\n"
        "  :attachments      List all ingested workspace attachments and grounding assets\n"
        "  :projects         Display registered projects and centralized root (~/.stackmindcli)\n"
        "  :goal <request>   Submit product goal to Architecture (claude) or adopt suggested goal\n"
        "  :adopt            Adopt suggested goal proposed by Architecture\n"
        "  :panel, :side     Toggle runtime sidebar visibility (Ctrl+B) for full-width conversation\n"
        "  :approve [reason] Submit Plan approval or HITL approval\n"
        "  :reject [reason]  Submit Plan rejection feedback or HITL rejection\n"
        "  :completion       Display Project Complete handover checklist\n"
        "  :cancel [target]  Cancel in-flight session turn, agent, or operation\n"
        "  :diff             Display Unified Diff viewer for staged changes\n"
        "  :matrix           Display 6-Dimensional Verification Matrix\n"
        "  :events           Stream incremental sequenced events from daemon\n"
        "  :pause            Pause active session turn\n"
        "  :resume           Resume active session\n"
        "  :chat             Display conversation transcript\n"
        "  :actions [cmd]    Toggle/expand/collapse turn Actions disclosure group\n"
        "  :thinking [cmd]   Toggle/expand/collapse thinking blocks (Ctrl+O)\n"
        "  :govern [mode]    Configure governance verbosity: tree | flat | compact\n"
        "  :compact          Compact older transcript turns to protect context window\n"
        "  :timeout [sec]    Get or set client turn wait timeout (default: no timeout)\n"
        "  :landing          Display branded landing block\n"
        "  :copy [goal|all]  Copy latest assistant response, suggested goal, or transcript to clipboard\n"
        "  :help             Show this help menu\n"
        "  :exit             Gracefully stop daemon and exit\n"
        "  <prompt text>     Submit a governed turn to the agent\n\n"
        "Shortcuts:\n"
        "  Ctrl+B            Toggle runtime sidebar (collapse for 100% full-width view)\n"
        "  Tab / Ctrl+G      Auto-fill Architect's suggested goal into input box\n"
        "  Ctrl+O            Toggle thinking block expansion\n"
        "  Ctrl+C            Cancel active turn or abort\n"
        "  Ctrl+V            Paste clipboard content into input composer"
    )


def _build_in_progress_turn_content(
    state: AutonomousDeliveryState,
    model_name: str | None,
    streamed_chunks: list[str],
    latest_act_desc: str,
    start_time: float,
    max_wait: float,
    layout_width: int,
) -> str:
    """Build unified live in-progress turn content: transcript + active turn."""
    transcript = (
        render_chat_transcript_str(state.messages, width=layout_width)
        if getattr(state, "messages", None)
        else ""
    )
    hdr = render_assistant_stream_header(model=model_name, width=layout_width)
    parts = [hdr]

    # In-progress actions rendered as compact ● Tool(arg) lines (never expanded during streaming)
    turn_acts = getattr(state, "current_turn_actions", None)
    if turn_acts and not getattr(turn_acts, "is_empty", True):
        act_str = render_actions_group_str(turn_acts, width=layout_width)
        if act_str:
            parts.append(act_str)

    # In-progress thinking - shows live thinking stream before and during answer streaming
    thinking = getattr(state, "current_turn_thinking", None)
    if thinking and thinking.strip():
        clean_think = thinking.strip()
        from cli.tui.chat import render_thinking_block_str
        is_exp = getattr(state, "live_thinking_expanded", True)
        think_str = render_thinking_block_str(clean_think, expanded=is_exp, in_progress=True, width=layout_width)
        if think_str:
            parts.append(think_str)

    # In-progress streamed response text (Working... progress sits at composer text box)
    if streamed_chunks:
        curr_text = "".join(streamed_chunks)
        from cli.tui.chat import format_contract_payload
        formatted_text = format_contract_payload(curr_text)
        wrapped_text = _wrap_text_to_width(formatted_text, layout_width)
        if wrapped_text:
            parts.append(wrapped_text)

    in_progress = "\n\n".join(parts)
    return f"{transcript}\n\n{in_progress}" if transcript else in_progress


def _stream_turn_execution(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    state: AutonomousDeliveryState,
    *,
    op_id: str = "turn",
    placeholder: str = "Generating response... (Ctrl+C to cancel)",
    effective_timeout: float = 0.0,
    is_tty: bool = True,
    live_manager: Any | None = None,
    initial_activity_desc: str = "Turn submitted",
    model_name: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Synchronously await turn completion while consuming events via native SSE."""
    workspace = Path(session["workspace"]) if session.get("workspace") else None
    max_wait = effective_timeout
    start_time = time.time()
    completed = False
    assistant_rendered = False
    streaming_active = False
    streamed_chunks: list[str] = []
    last_progress_time = start_time
    last_poll_time = start_time
    last_redraw_time: float = -1.0
    last_domain_event_time = start_time
    latest_act_desc = initial_activity_desc
    op_rec: dict[str, Any] | None = None
    op_status: str | None = None

    # A new turn starts with a clean attempt/retry display. Stale counters from
    # the previous turn otherwise bleed into this turn's composer border.
    state.reset_attempts_and_retries()

    _TERMINAL_OP_STATUS = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}
    _RUNNING_OP_STATUS = {"RUNNING", "ACTIVE", "IN_PROGRESS", "PENDING"}

    _diagnostics_enabled = os.environ.get("STACKMIND_TUI_DIAGNOSTICS") in {"1", "true", "yes"}
    _diagnostics_start = time.monotonic()

    def _format_token_usage_str(tokens: int) -> str:
        if tokens < 1000:
            return f"{tokens} tokens"
        return f"{tokens / 1000:.1f}k tokens"

    SYNC_WORKING_WORDS = [
        "Infusing...",
        "Synthesizing...",
        "Harmonizing...",
        "Calibrating...",
        "Transmuting...",
    ]

    def _get_working_placeholder() -> str:
        elapsed = time.time() - start_time
        in_progress_tokens = AutonomousDeliveryState.estimate_tokens(
            "".join(streamed_chunks) + getattr(state, "current_turn_thinking", "")
        )
        cumulative = getattr(state, "cumulative_turn_tokens", 0)
        toks = cumulative + in_progress_tokens if cumulative > 0 else in_progress_tokens
        is_sync_action = False
        targets_to_check = [
            getattr(state, "active_tool_target", ""),
            getattr(state, "last_tool_target", ""),
            getattr(state, "active_tool_name", ""),
            latest_act_desc,
        ]
        turn_actions = getattr(state, "current_turn_actions", None)
        if turn_actions and hasattr(turn_actions, "actions"):
            for a in turn_actions.actions:
                if getattr(a, "status", "").upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                    targets_to_check.extend([getattr(a, "target", ""), getattr(a, "description", ""), getattr(a, "tool_name", "")])
                    for sa in getattr(a, "sub_actions", []):
                        if getattr(sa, "status", "").upper() in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
                            targets_to_check.extend([getattr(sa, "target", ""), getattr(sa, "description", ""), getattr(sa, "tool_name", "")])

        for t in targets_to_check:
            if t and (".sync" in t or ".sync/" in t or ".sync\\" in t):
                is_sync_action = True
                break

        sync_words = getattr(state, "sync_working_words", None) or SYNC_WORKING_WORDS
        if getattr(state, "randomize_sync_verbs", False):
            import random
            verb = random.choice(sync_words)
        else:
            verb = getattr(state, "current_sync_verb", None) or sync_words[0]
        prefix = verb if is_sync_action else "Working..."
        res = f"{prefix} [{format_duration_human(elapsed)}] ({_format_token_usage_str(toks)})"
        if state is not None:
            state.active_working_status = res
        return res
    _streaming_diagnostics = {
        "redraw_count": 0,
        "event_count": 0,
        "render_sum_ms": 0,
        "render_count": 0,
        "write_sum_ms": 0,
        "write_count": 0,
        "last_event_time": 0.0,
        "last_redraw_time": 0.0,
        "inter_redraw_sum_ms": 0.0,
        "inter_redraw_count": 0,
        "inter_event_sum_ms": 0.0,
        "inter_event_count": 0,
        "max_render_ms": 0.0,
        "max_write_ms": 0.0,
        "max_inter_event_ms": 0.0,
        "max_inter_redraw_ms": 0.0,
    }

    install_resize_handler()
    init_term = shutil.get_terminal_size(fallback=(80, 24))
    current_cols, current_lines = init_term.columns, init_term.lines

    def _daemon_has_running_work() -> bool:
        """True when the daemon still reports an operation in flight for this session."""
        for op in getattr(state, "operations", {}).values():
            if str(getattr(op, "status", "")).upper() in _RUNNING_OP_STATUS:
                return True
        return False

    def _reconcile_with_daemon(now: float) -> bool:
        """Refresh daemon state and detect a turn that finished without a terminal event.

        The daemon can complete a turn while the client is between SSE events
        (or when the terminal event is missed entirely). The TUI used to wait
        forever in that case, leaving a frozen "Working..." frame on screen
        even though the daemon had nothing left to do.
        """
        nonlocal op_rec, op_status
        sid = session.get("session_id")
        if sid and hasattr(client, "get_session"):
            try:
                fresh_sess = client.get_session(sid)
                if isinstance(fresh_sess, Mapping):
                    state.update_from_session(fresh_sess)
            except Exception:
                pass
        if op_id and op_id != "turn" and hasattr(client, "operation_get"):
            try:
                record = client.operation_get(op_id)
                if isinstance(record, dict):
                    op_rec = record
                    op_status = record.get("status")
                    if str(op_status).upper() in _TERMINAL_OP_STATUS:
                        return True
            except Exception:
                pass
        # No terminal event, no operation in flight, and the answer already
        # arrived: the daemon is done with this turn.
        if (streamed_chunks or assistant_rendered) and not _daemon_has_running_work():
            if now - last_domain_event_time >= 3.0:
                return True
        return False

    try:
        stream_gen = adapter.stream(
            session["session_id"],
            live=True,
            timeout=1.0,
            after=state.last_sequence,
        )
        while (max_wait <= 0 or time.time() - start_time < max_wait) and not completed:
            resized, new_cols, new_lines = check_terminal_resize(current_cols, current_lines)
            if resized:
                current_cols, current_lines = new_cols, new_lines
                clear_transcript_cache()
                if live_manager is not None and getattr(live_manager, "is_active", False):
                    live_manager.handle_resize(new_cols, new_lines)

            # Poll for scroll keys (PgUp/PgDn/Wheel) so users can scroll during streaming
            if is_tty:
                from cli.tui.keyboard import poll_scroll_key, Key as _Key
                _scroll_key = poll_scroll_key()
                if _scroll_key is not None:
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    vp_h = compute_viewport_height(term_lines, state=state)
                    if _scroll_key in (_Key.PAGE_UP, _Key.MOUSE_WHEEL_UP):
                        step = max(1, vp_h // 2) if _scroll_key == _Key.PAGE_UP else 3
                        state.scroll_conversation_up(step)
                    elif _scroll_key in (_Key.PAGE_DOWN, _Key.MOUSE_WHEEL_DOWN):
                        step = max(1, vp_h // 2) if _scroll_key == _Key.PAGE_DOWN else 3
                        state.scroll_conversation_down(step)
                    # Immediate redraw after scroll
                    _s_layout = compute_layout(shutil.get_terminal_size().columns)
                    _s_content = _build_in_progress_turn_content(
                        state, model_name, streamed_chunks,
                        latest_act_desc, start_time, max_wait,
                        _s_layout.conversation_width,
                    )
                    redraw_full_screen(
                        session, state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        conversation_content=_s_content,
                        live_manager=live_manager,
                    )
            try:
                t_event_start = time.monotonic()
                ev = next(stream_gen)
                t_event_end = time.monotonic()
                if _diagnostics_enabled:
                    _streaming_diagnostics["event_count"] += 1
                    if _streaming_diagnostics["last_event_time"] > 0.0:
                        inter = (
                            t_event_start - _streaming_diagnostics["last_event_time"]
                        ) * 1000
                        _streaming_diagnostics["inter_event_sum_ms"] += inter
                        _streaming_diagnostics["inter_event_count"] += 1
                        if inter > _streaming_diagnostics["max_inter_event_ms"]:
                            _streaming_diagnostics["max_inter_event_ms"] = inter
                    _streaming_diagnostics["last_event_time"] = t_event_start
            except StopIteration:
                now = time.time()
                if workspace:
                    try:
                        state.sync_work_orders_from_disk(workspace)
                    except Exception:
                        pass
                if is_tty and (now - last_progress_time >= 0.2):
                    layout = compute_layout(shutil.get_terminal_size().columns)
                    tick_content = _build_in_progress_turn_content(
                        state,
                        model_name,
                        streamed_chunks,
                        latest_act_desc,
                        start_time,
                        max_wait,
                        layout.conversation_width,
                    )
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        conversation_content=tick_content,
                        live_manager=live_manager,
                    )
                    last_progress_time = now

                if now - last_poll_time >= 0.5:
                    last_poll_time = now
                    if _reconcile_with_daemon(now):
                        completed = True
                        break
                time.sleep(0.05)
                stream_gen = adapter.stream(
                    session["session_id"],
                    live=True,
                    timeout=1.0,
                    after=state.last_sequence,
                )
                continue
            except Exception:
                time.sleep(0.05)
                continue

            now = time.time()

            # 1. Handle heartbeat / keepalive
            if isinstance(ev, dict) and (
                ev.get("_heartbeat") or ev.get("name") == "system.heartbeat"
            ):
                # Keep repainting even after the model stopped streaming. The
                # previous gate (not streaming_active) froze the composer on the
                # last painted frame, so a turn that was still running looked
                # dead: the timer stopped and the frame never changed again.
                if now - last_progress_time >= 2.0:
                    render_start = time.monotonic()
                    layout = compute_layout(shutil.get_terminal_size().columns)
                    tick_content = _build_in_progress_turn_content(
                        state,
                        model_name,
                        streamed_chunks,
                        latest_act_desc,
                        start_time,
                        max_wait,
                        layout.conversation_width,
                    )
                    render_ms = (time.monotonic() - render_start) * 1000

                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        composer_content="",
                        conversation_content=tick_content,
                        live_manager=live_manager,
                    )

                    if _diagnostics_enabled:
                        _streaming_diagnostics["redraw_count"] += 1
                        _streaming_diagnostics["render_sum_ms"] += render_ms
                        _streaming_diagnostics["render_count"] += 1
                        _streaming_diagnostics["last_redraw_time"] = time.monotonic()
                    last_progress_time = now

                if now - last_poll_time >= 1.5:
                    last_poll_time = now
                    if _reconcile_with_daemon(now):
                        completed = True
                        break
                continue

            # 2. Process concrete domain event
            if not isinstance(ev, dict):
                continue
            sequence = ev.get("sequence")
            if isinstance(sequence, int) and sequence <= state.last_sequence:
                continue
            last_domain_event_time = now
            state.process_event(ev)
            ev_name = str(ev.get("name", ""))
            ev_payload = ev.get("payload", {}) if isinstance(ev.get("payload"), Mapping) else {}

            if workspace:
                state.sync_work_orders_from_disk(workspace)

            # Check for incremental text or reasoning delta (stream/token/chunk/thinking)
            delta = extract_text_delta(ev)
            think_delta = extract_reasoning_delta(ev)
            if think_delta:
                if not streaming_active:
                    streaming_active = True
                    if model_name is None:
                        if op_rec and isinstance(op_rec, dict):
                            model_name = op_rec.get("model")
                        elif session and isinstance(session, dict):
                            model_name = session.get("model")
                now_mono = time.monotonic()
                if now_mono - last_redraw_time >= 0.1:
                    layout = compute_layout(shutil.get_terminal_size().columns)
                    full_content = _build_in_progress_turn_content(
                        state,
                        model_name,
                        streamed_chunks,
                        latest_act_desc,
                        start_time,
                        max_wait,
                        layout.conversation_width,
                    )
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        composer_content="",
                        conversation_content=full_content,
                        live_manager=live_manager,
                    )
                    last_redraw_time = now_mono

            if delta:
                if not streaming_active:
                    streaming_active = True
                    if model_name is None:
                        if op_rec and isinstance(op_rec, dict):
                            model_name = op_rec.get("model")
                        elif session and isinstance(session, dict):
                            model_name = session.get("model")

                streamed_chunks.append(delta)

                now_mono = time.monotonic()
                if now_mono - last_redraw_time >= 0.1:
                    render_start = time.monotonic()
                    layout = compute_layout(shutil.get_terminal_size().columns)
                    full_content = _build_in_progress_turn_content(
                        state,
                        model_name,
                        streamed_chunks,
                        latest_act_desc,
                        start_time,
                        max_wait,
                        layout.conversation_width,
                    )
                    render_end = time.monotonic()
                    render_ms = (render_end - render_start) * 1000

                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        composer_content="",
                        conversation_content=full_content,
                        live_manager=live_manager,
                    )

                    write_end = time.monotonic()
                    write_ms = (write_end - render_end) * 1000

                    if _diagnostics_enabled:
                        _streaming_diagnostics["redraw_count"] += 1
                        _streaming_diagnostics["render_sum_ms"] += render_ms
                        _streaming_diagnostics["render_count"] += 1
                        if render_ms > _streaming_diagnostics["max_render_ms"]:
                            _streaming_diagnostics["max_render_ms"] = render_ms
                        _streaming_diagnostics["write_sum_ms"] += write_ms
                        _streaming_diagnostics["write_count"] += 1
                        if write_ms > _streaming_diagnostics["max_write_ms"]:
                            _streaming_diagnostics["max_write_ms"] = write_ms
                        if _streaming_diagnostics["last_redraw_time"] > 0.0:
                            inter_redraw = (
                                now_mono - _streaming_diagnostics["last_redraw_time"]
                            ) * 1000
                            _streaming_diagnostics["inter_redraw_sum_ms"] += inter_redraw
                            _streaming_diagnostics["inter_redraw_count"] += 1
                            if inter_redraw > _streaming_diagnostics["max_inter_redraw_ms"]:
                                _streaming_diagnostics["max_inter_redraw_ms"] = inter_redraw
                        _streaming_diagnostics["last_redraw_time"] = now_mono
                    last_redraw_time = now_mono
            else:
                ev_str = (
                    ""
                    if ev_name in {"operation.started", "turn.started", "operation.completed"}
                    else render_operational_event_str(ev)
                )
                if ev_str:
                    if not streaming_active and not is_tty:
                        click.echo(ev_str)
                    from cli.tui.events import format_action_description
                    latest_act_desc = format_action_description(ev_name)
                elif ev_name in {"event.toolCall", "tool.call"}:
                    t_name = ev_payload.get("tool_name") or ev_payload.get("tool") or "tool"
                    t_args = ev_payload.get("arguments", {})
                    t_target = str(
                        t_args.get("path") or t_args.get("command") or t_args.get("file") or ""
                    )
                    t_role = str(ev_payload.get("role") or ev_payload.get("agent_id") or "")
                    if not t_role and state is not None:
                        # NOTE: never assign to op_id here. op_id tracks the turn
                        # being streamed; overwriting it with the tool's
                        # operation id made the TUI poll and match the wrong
                        # operation, so the turn never reached its terminal
                        # event and the "Working..." status stuck forever.
                        tool_op_id = ev_payload.get("operation_id")
                        if (
                            tool_op_id
                            and hasattr(state, "operations")
                            and tool_op_id in state.operations
                        ):
                            t_role = state.operations[tool_op_id].role
                        if not t_role and hasattr(state, "operations"):
                            for op in state.operations.values():
                                if getattr(op, "status", "") == "RUNNING":
                                    t_role = getattr(op, "role", "")
                                    break
                    from cli.tui.events import format_action_description
                    latest_act_desc = format_action_description(t_name, target=t_target, role=t_role)

                is_tool_lifecycle = ev_name in {
                    "event.toolCall", "tool.call", "event.toolResult", "tool.result"
                } or ev_name.startswith(("tool_call.", "tool_result."))
                now_mono = time.monotonic()
                if is_tty and (is_tool_lifecycle or now_mono - last_redraw_time >= 0.1):
                    layout = compute_layout(shutil.get_terminal_size().columns)
                    full_content = _build_in_progress_turn_content(
                        state,
                        model_name,
                        streamed_chunks,
                        latest_act_desc,
                        start_time,
                        max_wait,
                        layout.conversation_width,
                    )
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        composer_placeholder=_get_working_placeholder(),
                        composer_content="",
                        conversation_content=full_content,
                        live_manager=live_manager,
                    )
                    last_redraw_time = now_mono

                # Only terminal turn/operation completion events finalize the response
                resp = None
                thinking = None
                response_events = {"operation.completed", "turn.completed"}
                if ev_name in response_events:
                    resp = extract_assistant_response(ev_payload, workspace=workspace)
                    thinking = (
                        extract_assistant_thinking(ev_payload, workspace=workspace)
                        or getattr(state, "current_turn_thinking", None)
                        or None
                    )
                if resp and not assistant_rendered and not streamed_chunks:
                    turn_acts = state.current_turn_actions
                    if turn_acts:
                        turn_acts.expanded = False
                    state.add_message("assistant", resp, actions=turn_acts, thinking=thinking)
                    if is_tty:
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=False,
                            composer_placeholder=_get_working_placeholder(),
                            live_manager=live_manager,
                        )
                    else:
                        click.echo(
                            render_assistant_message_str(
                                resp, actions=turn_acts, thinking=thinking
                            )
                        )
                    assistant_rendered = True

            # Check terminal event status
            if ev_name in {
                "operation.completed",
                "operation.failed",
                "operation.cancelled",
                "operation.blocked",
                "turn.completed",
                "turn.blocked",
                "run.complete",
                "run.failed",
                "run.blocked",
                "project.completed",
                "project.complete",
            } or state.is_complete or getattr(state, "phase", None) == ProjectPhase.PROJECT_COMPLETE:
                target_op = ev_payload.get("operation_id") or ev.get("operation_id")
                if (
                    ev_name in {
                        "turn.completed",
                        "turn.blocked",
                        "run.complete",
                        "run.failed",
                        "run.blocked",
                        "project.completed",
                        "project.complete",
                    }
                    or state.is_complete
                    or getattr(state, "phase", None) == ProjectPhase.PROJECT_COMPLETE
                    or (target_op and target_op == op_id)
                    or (not target_op and op_id in {"turn", "all"})
                    or op_id == "all"
                ):
                    completed = True
                    if ev_name in {
                        "operation.failed",
                        "operation.cancelled",
                        "operation.blocked",
                        "turn.blocked",
                        "run.failed",
                        "run.blocked",
                    }:
                        op_status = (
                            "FAILED"
                            if ev_name in {"operation.failed", "run.failed"}
                            else "BLOCKED"
                            if ev_name in {"operation.blocked", "turn.blocked", "run.blocked"}
                            else "CANCELLED"
                        )
                        if op_rec is None:
                            op_rec = {"status": op_status, "result": ev_payload}
                        elif isinstance(op_rec, dict):
                            op_rec["status"] = op_status
                            if "result" not in op_rec:
                                op_rec["result"] = ev_payload
                    break

            if now - last_poll_time >= 2.0:
                last_poll_time = now
                try:
                    op_rec = (
                        client.operation_get(op_id)
                        if hasattr(client, "operation_get") and op_id and op_id != "turn"
                        else None
                    )
                    if op_rec and isinstance(op_rec, dict):
                        op_status = op_rec.get("status")
                        if op_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                            completed = True
                            break
                except Exception:
                    pass
    except KeyboardInterrupt:
        try:
            if op_id and op_id != "turn" and hasattr(client, "operation_cancel"):
                client.operation_cancel(op_id, cascade=True)
            elif hasattr(client, "cancel"):
                client.cancel(session["session_id"])
        except Exception:
            try:
                adapter.command(f":cancel {op_id}", session_id=session["session_id"])
            except Exception:
                pass

        if op_id in state.operations:
            state.operations[op_id].status = "CANCELLED"
        for role_obj in state.roles.values():
            if getattr(role_obj, "state", "").upper() in {"RUNNING", "ACTIVE"}:
                role_obj.state = "CANCELLED"
        state.add_activity("User", "interrupted turn", op_id)

        if streaming_active and not is_tty:
            click.echo("")

        state.active_working_status = None
        cancel_msg = f"Turn operation {op_id} cancelled by operator (Ctrl+C)."
        turn_acts = state.current_turn_actions
        state.add_message("assistant", cancel_msg, actions=turn_acts)
        if is_tty:
            redraw_full_screen(
                session,
                state,
                clear=False,
                include_composer=True,
                composer_is_active=False,
                live_manager=live_manager,
            )
        else:
            click.echo(
                render_error_box_str(
                    cancel_msg,
                    title="TURN INTERRUPTED",
                    hint="In-flight turn operation was cancelled. Daemon process remains running.",
                )
            )
        if hasattr(state, "current_turn_actions") and state.current_turn_actions:
            state.current_turn_actions.complete_all()
        if live_manager is not None and not getattr(live_manager, "is_active", False):
            live_manager.start(include_composer=False)
        restore_composer_focus(state)
        return session, False

    # The turn is over (completed, failed, blocked, or timed out). Drop the
    # transient timer and retry counters before the final frame so they cannot
    # linger in the composer or be painted into the next idle frame.
    state.active_working_status = None
    state.reset_attempts_and_retries()

    # Finalize streaming if active
    if streamed_chunks and not assistant_rendered:
        full_resp = "".join(streamed_chunks).strip()
        from cli.tui.chat import format_contract_payload
        full_resp = format_contract_payload(full_resp)
        turn_acts = state.current_turn_actions
        if turn_acts:
            turn_acts.expanded = False
        thinking = getattr(state, "current_turn_thinking", None) or None
        state.add_message("assistant", full_resp, actions=turn_acts, thinking=thinking)
        assistant_rendered = True
        streamed_chunks = []
        if not is_tty:
            click.echo(
                render_assistant_message_str(
                    full_resp, actions=turn_acts, thinking=thinking
                )
            )

    if _diagnostics_enabled:
        duration = time.monotonic() - _diagnostics_start
        n = _streaming_diagnostics["redraw_count"]
        render_median = (_streaming_diagnostics["render_sum_ms"] / n) if n > 0 else 0.0
        write_median = (_streaming_diagnostics["write_sum_ms"] / n) if n > 0 else 0.0
        inter_redraw_median = (
            (_streaming_diagnostics["inter_redraw_sum_ms"] / _streaming_diagnostics["inter_redraw_count"])
            if _streaming_diagnostics["inter_redraw_count"] > 0
            else 0.0
        )
        inter_event_median = (
            (_streaming_diagnostics["inter_event_sum_ms"] / _streaming_diagnostics["inter_event_count"])
            if _streaming_diagnostics["inter_event_count"] > 0
            else 0.0
        )
        try:
            sys.stderr.write("\n=== STACKMIND TUI DIAGNOSTICS ===\n")
            sys.stderr.write(
                f"Turn duration: {duration:.2f}s | Events: {_streaming_diagnostics['event_count']} | Redraws: {n}\n"
            )
            sys.stderr.write(
                f"Render: median={render_median:.1f}ms | max={_streaming_diagnostics['max_render_ms']:.1f}ms\n"
            )
            sys.stderr.write(
                f"Write: median={write_median:.1f}ms | max={_streaming_diagnostics['max_write_ms']:.1f}ms\n"
            )
            sys.stderr.write(
                f"Event gap: median={inter_event_median:.1f}ms | max={_streaming_diagnostics['max_inter_event_ms']:.1f}ms\n"
            )
            sys.stderr.write(
                f"Redraw interval: median={inter_redraw_median:.1f}ms | max={_streaming_diagnostics['max_inter_redraw_ms']:.1f}ms\n"
            )
            sys.stderr.write("=== END DIAGNOSTICS ===\n\n")
        except Exception:
            pass

    if not assistant_rendered:
        try:
            if (
                op_rec is None
                or not completed
                or op_status in {"FAILED", "CANCELLED", "BLOCKED"}
            ) and hasattr(client, "operation_get") and op_id and op_id != "turn":
                fresh_op = client.operation_get(op_id)
                if fresh_op and isinstance(fresh_op, dict):
                    op_rec = fresh_op
                    op_status = op_rec.get("status")
                    if op_status in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}:
                        completed = True
        except Exception:
            pass

        try:
            if hasattr(client, "events"):
                remaining_events = list(
                    client.events(session["session_id"], after=state.last_sequence)
                )
                for ev in remaining_events:
                    state.process_event(ev)
                    ev_str = (
                        ""
                        if ev.get("name")
                        in {"operation.started", "turn.started", "operation.completed"}
                        else render_operational_event_str(ev)
                    )
                    if ev_str and not is_tty:
                        click.echo(ev_str)
                    resp = None
                    thinking = None
                    response_events = {
                        "operation.completed",
                        "turn.completed",
                    }
                    if ev.get("name") in response_events:
                        resp = extract_assistant_response(
                            ev.get("payload", {}), workspace=workspace
                        )
                        thinking = (
                            extract_assistant_thinking(
                                ev.get("payload", {}), workspace=workspace
                            )
                            or getattr(state, "current_turn_thinking", None)
                            or None
                        )
                    if resp and not assistant_rendered:
                        turn_acts = state.current_turn_actions
                        state.add_message(
                            "assistant", resp, actions=turn_acts, thinking=thinking
                        )
                        if is_tty:
                            redraw_full_screen(
                                session,
                                state,
                                clear=False,
                                include_composer=True,
                                composer_is_active=False,
                                live_manager=live_manager,
                            )
                        else:
                            click.echo(
                                render_assistant_message_str(
                                    resp, actions=turn_acts, thinking=thinking
                                )
                            )
                        assistant_rendered = True
        except Exception:
            pass

        if not assistant_rendered and op_rec and isinstance(op_rec, dict):
            res = op_rec.get("result") or {}
            summary = res.get("summary") or res.get("report_markdown") or res.get("reason")
            thinking = (
                res.get("thinking")
                or res.get("thought")
                or res.get("reasoning")
                or res.get("reasoning_content")
                or op_rec.get("thinking")
                or op_rec.get("thought")
                or getattr(state, "current_turn_thinking", None)
                or None
            )
            report_path_raw = res.get("report_path")
            if report_path_raw:
                rp = Path(report_path_raw)
                if not rp.is_absolute() and workspace:
                    rp = workspace / rp
                if rp.exists():
                    try:
                        cnt = rp.read_text(encoding="utf-8")
                        if "## Thinking" in cnt and not thinking:
                            _, _, tp = cnt.partition("## Thinking")
                            tb, _, _ = tp.partition("## ")
                            thinking = tb.strip() or None
                        if "## Report" in cnt:
                            _, _, b = cnt.partition("## Report")
                            rb, _, _ = b.partition("## Meta")
                            rep_body = rb.strip()
                            if not summary or summary == "Completed task via tools":
                                summary = rep_body or cnt.strip()
                        elif not summary or summary == "Completed task via tools":
                            summary = cnt.strip()
                    except Exception:
                        pass
            if op_status in {"FAILED", "CANCELLED", "BLOCKED"}:
                from cli.tui.events import clean_error_message

                err_raw = (
                    res.get("error")
                    or res.get("reason")
                    or res.get("message")
                    or op_rec.get("error")
                    or summary
                    or f"Operation {op_status.lower() if op_status else 'failed'}."
                )
                err_msg = (
                    clean_error_message(err_raw)
                    or f"Operation {op_status.lower() if op_status else 'failed'}."
                )
                err_title = (
                    "OLLAMA ERROR"
                    if "ollama" in err_msg.lower()
                    else (f"OPERATION {op_status}" if op_status else "OPERATION FAILED")
                )
                turn_acts = state.current_turn_actions
                if turn_acts:
                    turn_acts.expanded = False
                state.add_message(
                    "assistant",
                    f"Operation {op_status.lower() if op_status else 'failed'}: {err_msg}",
                    actions=turn_acts,
                )
                if is_tty:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        live_manager=live_manager,
                    )
                else:
                    click.echo(render_error_box_str(err_msg, title=err_title))
                assistant_rendered = True
            elif summary:
                if summary.strip() == "Completed task via tools":
                    summary = "Completed turn via tools. All executed actions are recorded in the Actions group above."
                from cli.tui.chat import format_contract_payload
                summary = format_contract_payload(summary)
                turn_acts = state.current_turn_actions
                state.add_message("assistant", summary, actions=turn_acts, thinking=thinking)
                if is_tty:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        live_manager=live_manager,
                    )
                else:
                    click.echo(
                        render_assistant_message_str(
                            summary, actions=turn_acts, thinking=thinking
                        )
                    )
                assistant_rendered = True

        if not assistant_rendered:
            if (
                op_rec
                and isinstance(op_rec, dict)
                and op_rec.get("status") in {"FAILED", "CANCELLED", "BLOCKED"}
            ):
                op_status = op_rec.get("status")
                from cli.tui.events import clean_error_message

                err_raw = (
                    (op_rec.get("result") or {}).get("error")
                    or (op_rec.get("result") or {}).get("reason")
                    or (op_rec.get("result") or {}).get("message")
                    or op_rec.get("error")
                    or f"Operation {op_status.lower()}."
                )
                err_msg = clean_error_message(err_raw) or f"Operation {op_status.lower()}."
                err_title = (
                    "OLLAMA ERROR" if "ollama" in err_msg.lower() else f"OPERATION {op_status}"
                )
                turn_acts = state.current_turn_actions
                if turn_acts:
                    turn_acts.expanded = False
                state.add_message(
                    "assistant", f"Operation {op_status.lower()}: {err_msg}", actions=turn_acts
                )
                if is_tty:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        live_manager=live_manager,
                    )
                else:
                    click.echo(render_error_box_str(err_msg, title=err_title))
                assistant_rendered = True
            elif completed:
                if state.is_complete or getattr(state, "phase", None) == ProjectPhase.PROJECT_COMPLETE:
                    completion_msg = "✓ Autonomous delivery complete. All work orders implemented, verified, and released."
                else:
                    completion_msg = f"Turn operation {op_id} completed."
                turn_acts = state.current_turn_actions
                state.add_message("assistant", completion_msg, actions=turn_acts)
                if is_tty:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        live_manager=live_manager,
                    )
                else:
                    click.echo(render_assistant_message_str(completion_msg, actions=turn_acts))
            else:
                timeout_msg = "No response within client wait time. Operation may still be running. Use :status or :events to inspect."
                state.add_message("system", timeout_msg)
                if is_tty:
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=False,
                        live_manager=live_manager,
                    )
                else:
                    click.echo(
                        render_error_box_str(
                            timeout_msg,
                            title="REQUEST TIMEOUT",
                            hint="Use :status or :events to inspect.",
                        )
                    )

    if hasattr(state, "current_turn_actions") and state.current_turn_actions:
        state.current_turn_actions.complete_all()

    # Always repaint the final frame. When the turn completed via a full
    # response event (assistant_rendered is already True) there are no
    # streamed_chunks left to trigger a redraw, and the last frame painted
    # during streaming still carries "Working..." and the attempt counters.
    if is_tty:
        is_awaiting_plan = (
            getattr(state, "phase", None) == ProjectPhase.AWAITING_APPROVAL
            or (getattr(state, "plan", None) and state.plan.get("state") == "AWAITING_APPROVAL")
        )
        post_placeholder = (
            "Plan ready: type :approve or 'continue' to execute..."
            if is_awaiting_plan
            else "Type a message..."
        )
        redraw_full_screen(
            session,
            state,
            clear=False,
            include_composer=True,
            composer_is_active=False,
            composer_placeholder=post_placeholder,
            composer_content="",
            live_manager=live_manager,
        )

    if live_manager is not None and not getattr(live_manager, "is_active", False):
        live_manager.start(include_composer=False)
    restore_composer_focus(state)
    return session, False


def dispatch_delivery_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState,
    *,
    client_timeout: float | None = None,
    live_manager: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    """Dispatch interactive TUI commands, updating delivery state reactively."""
    effective_timeout = (
        client_timeout if client_timeout is not None else getattr(state, "client_timeout", 0.0)
    )
    state.client_timeout = effective_timeout
    # Clear thinking buffer at start of new turn to prevent stale thinking from previous turn
    state.current_turn_thinking = ""
    normalized = text.strip()
    if not normalized:
        return session, False

    if normalized in {":exit", ":quit", "q"}:
        disable_mouse_reporting()
        if live_manager is not None and getattr(live_manager, "is_active", False):
            live_manager.stop()
        return session, True

    is_tty = False
    is_interactive = False
    try:
        is_tty = sys.stdout.isatty()
        is_interactive = is_tty and getattr(sys.stdin, "isatty", lambda: False)()
    except Exception:
        pass

    if normalized == ":help":
        output_str = get_help_str()
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":copy") or normalized.startswith(":yank") or normalized == ":clipboard":
        from cli.tui.clipboard import set_clipboard_text

        parts = normalized.split()
        mode = parts[1].lower() if len(parts) > 1 else "last"
        text_to_copy = ""
        if mode in ("goal", "plan"):
            suggested = (
                state.get_latest_suggested_goal()
                if hasattr(state, "get_latest_suggested_goal")
                else None
            )
            if suggested:
                text_to_copy = f":goal {suggested}"
                desc = "suggested :goal command"
            else:
                desc = "suggested goal"
        elif mode in ("all", "full", "transcript"):
            lines = []
            for msg in getattr(state, "messages", []):
                sender = str(getattr(msg, "role", getattr(msg, "sender", "message"))).upper()
                content = str(getattr(msg, "content", getattr(msg, "text", ""))).strip()
                if content:
                    lines.append(f"[{sender}]:\n{content}\n")
            text_to_copy = "\n".join(lines)
            desc = "entire conversation transcript"
        else:
            for msg in reversed(getattr(state, "messages", [])):
                role = getattr(msg, "role", getattr(msg, "sender", ""))
                if role not in ("user",):
                    text_to_copy = str(getattr(msg, "content", getattr(msg, "text", ""))).strip()
                    if text_to_copy:
                        break
            if not text_to_copy and getattr(state, "messages", []):
                last = state.messages[-1]
                text_to_copy = str(getattr(last, "content", getattr(last, "text", ""))).strip()
            desc = "latest response"

        if text_to_copy:
            ok = set_clipboard_text(text_to_copy)
            msg_str = (
                f"[CLIPBOARD] Copied {len(text_to_copy):,} characters ({desc}) to system clipboard."
                if ok
                else "[ERROR] Failed to write to system clipboard."
            )
        else:
            msg_str = (
                "[CLIPBOARD] No suggested goal found from the Architect to copy."
                if mode in ("goal", "plan")
                else "[CLIPBOARD] No messages available to copy."
            )

        state.add_message("user", normalized)
        state.add_message("system", msg_str)
        if not is_tty:
            click.echo(msg_str)
        return session, False

    if normalized in (":panel", ":sidebar", ":side", ":toggle panel", ":panel toggle"):
        is_now_visible = state.toggle_runtime_panel() if hasattr(state, "toggle_runtime_panel") else True
        status_text = (
            "[SIDEBAR] Runtime panel restored (2-column split layout)."
            if is_now_visible
            else "[SIDEBAR] Runtime panel collapsed (100% full-width conversation layout). Press Ctrl+B to toggle."
        )
        state.add_message("user", normalized)
        state.add_message("system", status_text)
        if not is_tty:
            click.echo(status_text)
        return session, False

    # Auto-approve pending plan if operator intuitively enters 'continue', 'proceed', or ':adopt'
    is_awaiting_plan = (
        getattr(state, "phase", None) == ProjectPhase.AWAITING_APPROVAL
        or (getattr(state, "plan", None) and state.plan.get("state") == "AWAITING_APPROVAL")
    )
    if is_awaiting_plan and normalized.lower() in {
        "continue", ":continue", "proceed", ":proceed", "go ahead", ":go", ":adopt", ":accept"
    }:
        return dispatch_delivery_command(
            adapter,
            client,
            session,
            ":approve Approved by operator",
            state,
            client_timeout=effective_timeout,
            live_manager=live_manager,
        )

    if normalized in (":adopt", ":accept", ":run goal"):
        goal = state.get_latest_suggested_goal() if hasattr(state, "get_latest_suggested_goal") else None
        if not goal:
            if is_awaiting_plan:
                return dispatch_delivery_command(
                    adapter,
                    client,
                    session,
                    ":approve Approved by operator",
                    state,
                    client_timeout=effective_timeout,
                    live_manager=live_manager,
                )
            msg = "[GOAL] No suggested goal found from the Architect to adopt."
            state.add_message("user", normalized)
            state.add_message("system", msg)
            if not is_tty:
                click.echo(msg)
            return session, False
        state.add_message("system", f"[AUTO-ADOPT] Adopting Architect's suggested goal: :goal {goal}")
        return dispatch_delivery_command(
            adapter,
            client,
            session,
            f":goal {goal}",
            state,
            client_timeout=effective_timeout,
            live_manager=live_manager,
        )

    if normalized == ":debugkeys":
        from cli.tui.keyboard import DEBUG_KEYS_ENABLED, DEBUG_LOG_FILE, set_debug_keys

        new_state = not DEBUG_KEYS_ENABLED
        set_debug_keys(new_state)
        status_str = (
            f"[DEBUG KEYS] Logging ENABLED -> writing raw key events to {DEBUG_LOG_FILE}"
            if new_state
            else "[DEBUG KEYS] Logging DISABLED."
        )
        state.add_message("user", normalized)
        state.add_message("system", status_str)
        if not is_tty:
            click.echo(status_str)
        return session, False

    if normalized == ":reconnect":
        output_parts = [render_connection_status_str("reconnecting")]
        ok, missed = reconnect_and_sync(client, adapter, state, session)
        if ok:
            output_parts.append(
                f"{render_connection_status_str('online')} (reconnected, {len(missed)} missed events synced)"
            )
        else:
            output_parts.append(
                render_error_box_str(
                    f"Could not reconnect to daemon at {client.url}",
                    title="RECONNECTION FAILED",
                    hint="Verify that the daemon process is running and reachable.",
                )
            )
        output_str = "\n".join(output_parts)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":status":
        error_str = None
        try:
            session = adapter.command(":status", session_id=session["session_id"])
            state.connection_status = "online"
            sid = session.get("session_id")
            if sid:
                missed = client.events(sid, after=state.last_sequence)
                if missed:
                    workspace = Path(session["workspace"]) if session.get("workspace") else None
                    recover_transcript_from_events(missed, state, workspace=workspace)
        except Exception as err:
            if is_connection_error(err):
                state.connection_status = "reconnecting"
                error_str = render_error_box_str(
                    f"Daemon connection failed: {err}",
                    title="CONNECTION ERROR",
                    hint="Daemon appears unreachable. Use :reconnect or check daemon status.",
                )
            else:
                error_str = render_error_box_str(str(err), title="STATUS ERROR")

        output_parts = []
        if error_str:
            output_parts.append(error_str)
        sid = session.get("session_id") or (getattr(state, "session_id", None) if state else None)
        if sid:
            output_parts.append(f"Session: {sid}")
        output_parts.append(get_status_str(session, state=state))
        if state is not None:
            from cli.tui.views import render_project_delivery_view
            output_parts.append(render_project_delivery_view(state, include_runtime_items=False))
        output_str = "\n\n".join(output_parts)

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":contract":
        contract_data = session.get("contract", {})
        output_str = render_contract_hud_str(contract_data)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized == ":roles"
        or normalized.startswith(":roles ")
        or normalized in {":role"}
    ):
        from cli.tui.settings_panel import (
            render_roles_panel,
            run_interactive_roles_panel,
        )
        from validators.kernel.settings import load_settings

        ws = Path(session["workspace"]) if session.get("workspace") else None
        subcmd = normalized.split(maxsplit=1)[1] if " " in normalized else None
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns

        # Query daemon role bindings if supported, and update state roles
        try:
            roles_data = client.list_roles()
            if roles_data and isinstance(roles_data, list):
                for rd in roles_data:
                    role_key = rd.get("role", "").title()
                    normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = rd.get(
                            "backend", state.roles[normalized_key].backend
                        )
                        state.roles[normalized_key].model = rd.get("model")
        except Exception:
            pass

        # Also sync from disk settings if present
        try:
            from validators.kernel.settings import find_settings_file, load_settings
            if find_settings_file(ws):
                s = load_settings(ws)
                for r_name, r_cfg in s.roles.items():
                    r_title = "Q/A" if r_name.upper() in {"QA", "Q/A"} else r_name.title()
                    if r_title in state.roles:
                        if r_cfg.get("provider"):
                            state.roles[r_title].backend = r_cfg["provider"]
                        if r_cfg.get("model"):
                            state.roles[r_title].model = r_cfg["model"]
        except Exception:
            pass

        if is_interactive and not subcmd:
            output_str = run_interactive_roles_panel(
                session=session,
                state=state,
                client=client,
                live_manager=live_manager,
                workspace=ws,
                width=term_cols,
            )
        else:
            output_str = render_roles_panel(state, detailed=True)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":rebind") or normalized.startswith(":configure"):
        parts = normalized.split()
        if len(parts) >= 3:
            role_arg = parts[1]
            backend_arg = parts[2]
            model_arg = " ".join(parts[3:]).strip() if len(parts) >= 4 else None
            from validators.kernel.settings import load_settings, sanitize_model_name, save_settings

            if model_arg:
                model_arg = sanitize_model_name(model_arg, backend_arg)
            try:
                client.configure_role_backend(role=role_arg, backend=backend_arg, model=model_arg)
                role_key = role_arg.title()
                normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                ws = Path(session["workspace"]) if session.get("workspace") else None
                s = load_settings(ws)
                m_to_save = model_arg or (
                    s.providers.get(backend_arg).default_model
                    if backend_arg in s.providers and s.providers[backend_arg].default_model
                    else "default"
                )
                if role_arg.lower() in ("code", "coding"):
                    for code_role in ("Backend", "Frontend"):
                        if code_role in state.roles:
                            state.roles[code_role].backend = backend_arg
                            if model_arg:
                                state.roles[code_role].model = model_arg
                    s.roles["backend"] = {"provider": backend_arg, "model": m_to_save}
                    s.roles["frontend"] = {"provider": backend_arg, "model": m_to_save}
                else:
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = backend_arg
                        if model_arg:
                            state.roles[normalized_key].model = model_arg
                    s.roles["qa" if role_arg.lower() in ("qa", "q/a") else role_arg.lower()] = {
                        "provider": backend_arg,
                        "model": m_to_save,
                    }
                save_settings(s, ws)
                output_str = (
                    "[DEPRECATED] ':rebind' is deprecated and scheduled for removal. "
                    "Use ':roles' to assign providers to agent roles, or ':provider' for provider configuration.\n"
                    f"[SUCCESS] Rebound role '{role_arg}' to backend '{backend_arg}'"
                    + (f" (model: {model_arg})" if model_arg else "")
                )
            except Exception as err:
                output_str = (
                    "[DEPRECATED] ':rebind' is deprecated. Please use ':roles' instead.\n"
                    f"[ERROR] Failed to rebind role '{role_arg}': {err}\n"
                    f"Hint: Syntax is ':rebind <role> <backend> [model]' (e.g., ':rebind {role_arg} custom <model>'). "
                    "Ensure no turn is currently active before rebinding."
                )
        else:
            output_str = (
                "[DEPRECATED] ':rebind' is deprecated and scheduled for removal. "
                "Use ':roles' to configure agent backends interactively, or ':provider' for provider credentials.\n"
                "Usage: :rebind <role> <backend> [model]  (e.g., :rebind architecture custom <model>)"
            )
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized == ":config"
        or normalized.startswith(":config ")
        or normalized in {":settings", ":setting"}
        or normalized.startswith(":settings ")
        or normalized.startswith(":setting ")
    ):
        from cli.tui.settings_panel import (
            render_stackmind_settings_str,
            run_interactive_stackmind_settings,
        )
        from validators.kernel.settings import load_settings, save_settings

        ws = Path(session["workspace"]) if session.get("workspace") else None
        subcmd = normalized.split(maxsplit=1)[1] if " " in normalized else None
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if is_interactive and not subcmd:
            output_str = run_interactive_stackmind_settings(
                session=session,
                state=state,
                client=client,
                live_manager=live_manager,
                workspace=ws,
                width=term_cols,
            )
        else:
            settings_obj = load_settings(ws)
            if subcmd:
                parts = subcmd.strip().split()
                scmd = parts[0].lower()
                if scmd in ("profile", "execution-profile", "exec-profile"):
                    if len(parts) >= 2:
                        prof_val = parts[1].lower()
                        if prof_val in ("low", "medium", "high"):
                            settings_obj.execution_profile = prof_val
                            save_settings(settings_obj, ws)
                            state.execution_profile = prof_val
                            if client and hasattr(client, "settings_update"):
                                try:
                                    client.settings_update(settings_obj.to_dict())
                                except Exception:
                                    pass
                            output_str = f"[SUCCESS] Execution profile set to {prof_val.upper()} in settings.json"
                        else:
                            output_str = f"[ERROR] Invalid profile '{prof_val}'. Available: low, medium, high"
                    else:
                        output_str = f"Current execution profile: {settings_obj.effective_profile.upper()}"
                elif scmd in ("timeout", "turn-timeout"):
                    if len(parts) >= 2 and parts[1].isdigit():
                        t_val = int(parts[1])
                        settings_obj.client_timeout = t_val
                        state.turn_timeout = t_val
                        save_settings(settings_obj, ws)
                        output_str = f"[SUCCESS] Client turn timeout set to {t_val}s."
                    else:
                        output_str = f"Current client turn timeout: {getattr(settings_obj, 'client_timeout', 0)}s"
                elif scmd in ("providers", "provider", "models"):
                    from cli.tui.settings_panel import render_settings_overview_str

                    output_str = render_settings_overview_str(settings_obj, width=term_cols)
                elif scmd in ("roles", "role"):
                    from cli.tui.settings_panel import render_roles_panel

                    output_str = render_roles_panel(state, detailed=True)
                else:
                    output_str = render_stackmind_settings_str(settings_obj, width=term_cols)
            else:
                output_str = render_stackmind_settings_str(settings_obj, width=term_cols)
        try:
            roles_data = client.list_roles()
            if roles_data and isinstance(roles_data, list):
                for rd in roles_data:
                    role_key = rd.get("role", "").title()
                    normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = rd.get(
                            "backend", state.roles[normalized_key].backend
                        )
                        state.roles[normalized_key].model = rd.get("model")
        except Exception:
            pass
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized in {":provider", ":providers"}
        or normalized.startswith(":provider ")
        or normalized.startswith(":providers ")
        or normalized == ":models"
        or normalized.startswith(":models ")
    ):
        from cli.tui.settings_panel import (
            render_settings_overview_str,
            run_interactive_providers_panel,
            run_interactive_settings_panel,
        )
        from validators.kernel.settings import load_settings, save_settings

        ws = Path(session["workspace"]) if session.get("workspace") else None
        subcmd = normalized.split(maxsplit=1)[1] if " " in normalized else None
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if is_interactive and not subcmd:
            output_str = run_interactive_providers_panel(
                session=session,
                state=state,
                client=client,
                live_manager=live_manager,
                workspace=ws,
                width=term_cols,
            )
        else:
            if subcmd:
                output_str = run_interactive_settings_panel(
                    workspace=ws,
                    client=client,
                    width=term_cols,
                    initial_subcmd=subcmd,
                )
            else:
                settings_obj = load_settings(ws)
                output_str = render_settings_overview_str(settings_obj, width=term_cols)
        if normalized == ":models" or normalized.startswith(":models "):
            output_str = f"[INFO] ':models' is unified into ':provider'. Configure endpoints and models via ':provider', or assign role backends via ':roles'.\n\n{output_str}"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":wo":
        output_str = render_work_orders_panel(state)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized in {":tree", ":agents"}:
        try:
            agents_list = client.list_agents(session_id=session["session_id"])
            if agents_list and isinstance(agents_list, list):
                for a in agents_list:
                    aid = a.get("agent_id")
                    rname = (a.get("role") or "Backend").title()
                    normalized_rname = "Q/A" if rname.upper() in {"QA", "Q/A"} else rname
                    op_id = a.get("operation_id") or f"op-{aid}"
                    parent_id = a.get("parent_operation_id") or "op-root"
                    backend = a.get("backend", "Codex")
                    status = a.get("status", "RUNNING")
                    node = OperationNode(
                        op_id,
                        normalized_rname,
                        role=normalized_rname,
                        backend=backend,
                        status=status,
                        parent_id=parent_id,
                    )
                    state.operations[op_id] = node
                    if (
                        parent_id in state.operations
                        and op_id not in state.operations[parent_id].children
                    ):
                        state.operations[parent_id].children.append(op_id)
        except Exception:
            pass
        output_str = render_operation_tree(state)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":plan":
        try:
            plan_obj = client.plan_get(session["session_id"])
            if plan_obj:
                state.plan = dict(plan_obj)
                if plan_obj.get("state") == "AWAITING_APPROVAL":
                    state.phase = ProjectPhase.AWAITING_APPROVAL
        except Exception:
            pass
        output_str = render_plan_panel_str(state)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":skills" or normalized.startswith(":skills ") or normalized.startswith(":skill"):
        _, _, filter_arg = normalized.partition(" ")
        clean_filter = filter_arg.strip() or None
        ws_path = session.get("workspace") if isinstance(session, dict) else None
        output_str = render_skills_panel_str(ws_path, skill_filter=clean_filter)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized == ":consume-skills"
        or normalized.startswith(":consume-skills ")
        or normalized == ":consume-skill"
        or normalized.startswith(":consume-skill ")
    ):
        _, _, raw_arg = normalized.partition(" ")
        clean_arg = raw_arg.strip()
        ws_path = session.get("workspace") if isinstance(session, dict) else None

        if not clean_arg:
            output_str = render_consume_skills_help_str()
        else:
            is_global = True
            tokens = clean_arg.split()
            filtered_tokens = []
            for tok in tokens:
                if tok in ("--local", "-l"):
                    is_global = False
                elif tok in ("--global", "-g"):
                    is_global = True
                else:
                    filtered_tokens.append(tok)

            source_target = " ".join(filtered_tokens).strip()
            if (source_target.startswith('"') and source_target.endswith('"')) or (
                source_target.startswith("'") and source_target.endswith("'")
            ):
                source_target = source_target[1:-1].strip()

            if not source_target:
                output_str = render_consume_skills_help_str()
            else:
                from validators.skill.consumer import consume_skills
                from validators.kernel.home import get_global_skills_dir

                try:
                    records = consume_skills(
                        source=source_target,
                        project_path=ws_path,
                        is_global=is_global,
                    )
                    dest_desc = (
                        str(get_global_skills_dir() / "global")
                        if is_global
                        else str(Path(ws_path or ".").resolve() / ".sync" / "skills" / "active")
                    )
                    output_str = render_consumed_skills_summary_str(records, is_global, dest_desc)
                except Exception as exc:
                    output_str = f"[Error consuming skills] {exc}"

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized == ":attach"
        or normalized.startswith(":attach ")
        or normalized == ":upload"
        or normalized.startswith(":upload ")
    ):
        from validators.ingest import AttachmentStore
        from cli.tui.attachments_panel import render_attachment_ingested_str

        _, _, raw_arg = normalized.partition(" ")
        clean_path = raw_arg.strip()
        if (clean_path.startswith('"') and clean_path.endswith('"')) or (
            clean_path.startswith("'") and clean_path.endswith("'")
        ):
            clean_path = clean_path[1:-1].strip()

        ws_path = session.get("workspace") if isinstance(session, dict) else None
        store = AttachmentStore(ws_path or ".")

        if not clean_path:
            output_str = (
                "Usage: :attach <file_or_archive_path> (or :upload <path>)\n"
                "Supported formats:\n"
                "  • Documents: .pdf, .docx, .md, .txt, .csv, .html\n"
                "  • Images: .png, .jpg, .jpeg, .webp, .svg\n"
                "  • Archives: .zip, .tar.gz (extracted safely with Zip-Slip protection)\n"
                "Attachments are indexed into .sync/attachments/ and grounded into multi-agent turns."
            )
        else:
            try:
                record = store.ingest(clean_path)
                term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                output_str = render_attachment_ingested_str(record, width=term_cols)
            except Exception as exc:
                output_str = f"[Error ingesting attachment] {exc}"

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized in {":attachments", ":files"}:
        from validators.ingest import AttachmentStore
        from cli.tui.attachments_panel import render_attachments_list_str

        ws_path = session.get("workspace") if isinstance(session, dict) else None
        store = AttachmentStore(ws_path or ".")
        records = store.list_attachments()
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        output_str = render_attachments_list_str(records, width=term_cols)

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":attachment ") or normalized.startswith(":file "):
        from validators.ingest import AttachmentStore
        from cli.tui.attachments_panel import render_attachment_detail_str

        _, _, target_id = normalized.partition(" ")
        ws_path = session.get("workspace") if isinstance(session, dict) else None
        store = AttachmentStore(ws_path or ".")
        record = store.get_attachment(target_id.strip())
        term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        if record:
            output_str = render_attachment_detail_str(record, width=term_cols)
        else:
            output_str = f"Attachment '{target_id.strip()}' not found. Use :attachments to view all files."

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":projects" or normalized.startswith(":projects "):
        output_str = render_projects_panel_str()
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":profile" or normalized.startswith(":profile "):
        output_str = "':profile' has been merged into ':config'. Open ':config' to configure execution profile and skills policy."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":approve"):
        _, _, reason = normalized.partition(" ")
        clean_reason = reason.strip() or "Approved by operator"
        # Check if plan is awaiting approval
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (
            state.phase == ProjectPhase.AWAITING_APPROVAL
            or state.plan.get("state") == "AWAITING_APPROVAL"
        ) and plan_id:
            try:
                client.plan_approve(session["session_id"], plan_id, reason=clean_reason)
                # Fetch and process events to sync state with daemon (e.g., plan.approved, operation.started)
                sid = session.get("session_id")
                if sid:
                    events = client.events(sid, after=state.last_sequence)
                    for ev in events:
                        state.process_event(ev)
                state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                state.completion_checklist["PLAN.md"] = True
                if state.plan:
                    state.plan["state"] = "APPROVED"
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "APPROVED"
                feedback_msg = f"Plan '{plan_id}' approved. Approval recorded."
                state.add_message("user", normalized)
                state.add_message("assistant", f"✓ {feedback_msg} (reason: {clean_reason})")
                if not is_tty:
                    click.echo(feedback_msg)
                    return session, False
                return _stream_turn_execution(
                    adapter,
                    client,
                    session,
                    state,
                    op_id="turn",
                    placeholder="Autonomous delivery in progress... (Ctrl+C to pause/cancel)",
                    effective_timeout=effective_timeout,
                    is_tty=is_tty,
                    live_manager=live_manager,
                    initial_activity_desc="Authoring work orders and contracts",
                )
            except Exception as e:
                err_msg = f"Plan approval error: {e}"
                state.add_message("user", normalized)
                state.add_message("system", err_msg)
                if not is_tty:
                    click.echo(err_msg)
                return session, False
        # Fallback to standard HITL approval
        adapter.command(f":approve {clean_reason}".strip(), session_id=session["session_id"])
        state.add_message("user", normalized)
        state.add_message("assistant", f"✓ HITL Approval recorded: {clean_reason}")
        if not is_tty:
            click.echo("Approval recorded.")
            return session, False
        return _stream_turn_execution(
            adapter,
            client,
            session,
            state,
            op_id="turn",
            placeholder="Execution in progress... (Ctrl+C to pause/cancel)",
            effective_timeout=effective_timeout,
            is_tty=is_tty,
            live_manager=live_manager,
            initial_activity_desc="Execution resumed after HITL approval",
        )

    if normalized.startswith(":reject"):
        _, _, reason = normalized.partition(" ")
        clean_feedback = reason.strip() or "Operator requested revision"
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (
            state.phase == ProjectPhase.AWAITING_APPROVAL
            or state.plan.get("state") == "AWAITING_APPROVAL"
        ) and plan_id:
            try:
                client.plan_reject(session["session_id"], plan_id, reason=clean_feedback)
                state.phase = ProjectPhase.PLAN_REJECTED
                if state.plan:
                    state.plan["state"] = "REJECTED"
                    state.plan["feedback"] = clean_feedback
                if state.plan_revisions:
                    state.plan_revisions[-1].state = "REJECTED"
                    state.plan_revisions[-1].feedback = clean_feedback
                feedback_msg = f"Plan '{plan_id}' rejected. Rejection recorded."
                state.add_message("user", normalized)
                state.add_message("assistant", f"✗ {feedback_msg} (feedback: {clean_feedback})")
                if not is_tty:
                    click.echo(feedback_msg)
                return session, False
            except Exception as e:
                err_msg = f"Plan rejection error: {e}"
                state.add_message("user", normalized)
                state.add_message("system", err_msg)
                if not is_tty:
                    click.echo(err_msg)
                return session, False
        # Fallback to standard HITL rejection
        adapter.command(f":reject {clean_feedback}".strip(), session_id=session["session_id"])
        state.add_message("user", normalized)
        state.add_message("assistant", f"✗ HITL Rejection recorded: {clean_feedback}")
        if not is_tty:
            click.echo("Rejection recorded.")
        return session, False

    if normalized == ":completion":
        output_str = render_completion_surface(state)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":cancel"):
        _, _, target = normalized.partition(" ")
        clean_target = target.strip()
        if clean_target:
            try:
                client.cancel_agent(clean_target, session_id=session["session_id"])
                if clean_target in state.operations:
                    state.operations[clean_target].status = "CANCELLED"
                for role_key, role_obj in state.roles.items():
                    if (
                        role_key.lower() == clean_target.lower()
                        or getattr(role_obj, "agent_id", "") == clean_target
                    ):
                        role_obj.state = "CANCELLED"
                output_str = f"Agent/Operation {clean_target} CANCELLED"
            except Exception:
                try:
                    client.operation_cancel(clean_target)
                    if clean_target in state.operations:
                        state.operations[clean_target].status = "CANCELLED"
                    output_str = f"Operation {clean_target} CANCELLED"
                except Exception as e:
                    output_str = render_error_box_str(
                        f"Cancellation error: {e}", title="CANCEL ERROR"
                    )
        else:
            # General session turn cancellation
            session = adapter.command(":cancel", session_id=session["session_id"])
            state.update_from_session(session)
            output_str = f"Session {session.get('state', 'CANCELLED')}"

        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":pause":
        session = adapter.command(":pause", session_id=session["session_id"])
        output_str = f"Session {session.get('state', 'PAUSED')}"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":resume"):
        _, _, run_arg = normalized.partition(" ")
        run_id_clean = run_arg.strip() or None
        run_resumed_msg = ""
        try:
            session = adapter.command(":resume", session_id=session["session_id"])
        except Exception:
            try:
                session = client.get_session(session["session_id"])
            except Exception:
                pass
        try:
            resumed_run = client.run_resume(session["session_id"], run_id=run_id_clean)
            if resumed_run:
                run_resumed_msg = f" | Supervisor run '{resumed_run.get('run_id')}' resumed in phase {resumed_run.get('phase')}"
                state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
                state.error = None
        except Exception as run_err:
            if run_id_clean:
                run_resumed_msg = f" (supervisor note: {run_err})"
        if session.get("workspace"):
            state.sync_work_orders_from_disk(session.get("workspace"))
        output_str = f"Session {session.get('state', 'RUNNING')}{run_resumed_msg}"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":diff"):
        _, _, diff_arg = normalized.partition(" ")
        diff_val = diff_arg.strip() if diff_arg.strip() else None
        if diff_val:
            raw_diff = adapter.command(
                f":diff diff={diff_val}", session_id=session["session_id"], diff=diff_val
            )
        else:
            raw_diff = adapter.command(":diff", session_id=session["session_id"])
        output_str = render_unified_diff_str(str(raw_diff))
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":matrix":
        dims = state.verification_dimensions if state is not None else None
        output_str = render_verification_matrix_str(dims)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":events":
        output_parts = []
        try:
            events = adapter.command(":events", session_id=session["session_id"])
            rendered_any = False
            if events:
                workspace = Path(session["workspace"]) if session.get("workspace") else None
                recovered_msgs = recover_transcript_from_events(events, state, workspace=workspace)
                for event in events:
                    ev_str = render_operational_event_str(event)
                    if ev_str:
                        output_parts.append(ev_str)
                        rendered_any = True
                for msg in recovered_msgs:
                    if msg.role == "assistant":
                        output_parts.append(
                            render_assistant_message_str(
                                msg.content, actions=msg.actions, thinking=msg.thinking
                            )
                        )
                        rendered_any = True
            if not rendered_any:
                output_parts.append("No new events.")
        except Exception as err:
            if is_connection_error(err):
                state.connection_status = "reconnecting"
                output_parts.append(
                    render_error_box_str(
                        f"Event stream disconnected: {err}",
                        title="CONNECTION ERROR",
                        hint="Use :reconnect to recover missed events once daemon is restored.",
                    )
                )
            else:
                output_parts.append(render_error_box_str(str(err), title="EVENT ERROR"))
        output_str = "\n".join(output_parts)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized in {":chat", ":history"}:
        output_str = render_chat_transcript_str(state.messages)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":landing":
        output_str = f"{render_top_header_bar_str(session, width=80)}\n{render_landing_block_str(session=session, status=state.connection_status, width=80)}"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":runtime":
        output_str = render_runtime_panel_str(width=36, state=state)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":actions"):
        _, _, subcmd = normalized.partition(" ")
        subcmd = subcmd.strip().lower()
        target_group = state.get_latest_actions_group()
        if not target_group or target_group.is_empty:
            output_str = "No actions recorded for the current turn."
            state.add_message("user", normalized)
            state.add_message("system", output_str)
            if not is_tty:
                click.echo(output_str)
            return session, False

        if subcmd in {"expand", "open"}:
            target_group.expand()
        elif subcmd in {"collapse", "close"}:
            target_group.collapse()
        elif subcmd.isdigit():
            idx = int(subcmd)
            state.toggle_actions(turn_index=idx)
            target_group = state.get_latest_actions_group()
        else:
            target_group.toggle()

        output_str = render_actions_group_str(target_group)
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":thinking"):
        _, _, subcmd = normalized.partition(" ")
        subcmd = subcmd.strip().lower()
        if subcmd in {"expand", "open"}:
            state.expand_thinking()
        elif subcmd in {"collapse", "close"}:
            state.collapse_thinking()
        elif subcmd.isdigit():
            state.toggle_thinking(index=int(subcmd))
        else:
            state.toggle_thinking()

        status_str = "EXPANDED" if getattr(state, "thinking_expanded", False) else "COLLAPSED"
        output_str = f"Thinking blocks: {status_str} (toggle with Ctrl+O or :thinking)"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if (
        normalized in {":click actions", ":mouse click actions", ":click"}
        or normalized.startswith("\x1b[<")
        or normalized.startswith("\x1b[M")
    ):
        target_group = state.get_latest_actions_group()
        if target_group and not target_group.is_empty:
            target_group.handle_click()
            output_str = render_actions_group_str(target_group)
        else:
            output_str = "No actions to toggle."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":govern" or normalized.startswith(":govern "):
        _, _, mode = normalized.partition(" ")
        clean_mode = mode.strip().lower()
        if clean_mode in {"tree", "flat", "compact"}:
            state.govern_verbosity = clean_mode
            if hasattr(state, "current_turn_actions") and state.current_turn_actions:
                state.current_turn_actions.verbosity = clean_mode
            if hasattr(state, "turn_actions"):
                for group in state.turn_actions.values():
                    group.verbosity = clean_mode
            ws = Path(session["workspace"]) if session and session.get("workspace") else None
            if not ws and getattr(state, "workspace_path", None):
                ws = Path(state.workspace_path)
            try:
                from validators.kernel.settings import load_settings, save_settings
                s = load_settings(ws)
                s.govern_verbosity = clean_mode
                save_settings(s, ws)
            except Exception:
                pass
            output_str = f"✓ Governance verbosity set to '{clean_mode}'."
        elif clean_mode == "":
            curr = getattr(state, "govern_verbosity", "tree")
            output_str = (
                f"Current governance verbosity: {curr}\n"
                "Usage: :govern [tree|flat|compact]\n"
                "  tree    - Show 'Run harness' with indented sub-actions (default)\n"
                "  flat    - Direct linear stream of individual sub-actions\n"
                "  compact - High-level harness summary only"
            )
        else:
            output_str = f"Unknown governance verbosity '{clean_mode}'. Choose: tree | flat | compact"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":timeout"):
        _, _, to_arg = normalized.partition(" ")
        clean_to = to_arg.strip()
        if clean_to:
            try:
                new_to = float(clean_to)
                if new_to < 0:
                    output_str = "Timeout must be zero (no timeout) or a positive number."
                elif new_to == 0:
                    state.client_timeout = 0.0
                    output_str = "Client timeout disabled (no timeout — will wait until operation completes)."
                else:
                    state.client_timeout = new_to
                    output_str = f"Client timeout set to {new_to:.1f}s."
            except ValueError:
                output_str = f"Invalid timeout '{clean_to}'. Provide seconds (e.g., :timeout 60) or 0 to disable."
        else:
            cur_to = getattr(state, "client_timeout", effective_timeout)
            if cur_to <= 0:
                output_str = "Current client timeout: disabled (no timeout)."
            else:
                output_str = f"Current client timeout: {cur_to:.1f}s."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":compact":
        compacted = state.compact_transcript()
        output_str = (
            "Transcript compacted."
            if compacted
            else "Transcript is already within its compact retention window."
        )
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":goal" or (normalized.startswith(":goal ") and not normalized[6:].strip()):
        suggested = (
            state.get_latest_suggested_goal()
            if hasattr(state, "get_latest_suggested_goal")
            else None
        )
        if suggested:
            normalized = f":goal {suggested}"
            state.add_message("system", f"[AUTO-ADOPT] Using Architect's suggested goal: :goal {suggested}")
        else:
            output_str = "Usage: :goal <product goal or feature request>"
            state.add_message("user", normalized)
            state.add_message("system", output_str)
            if not is_tty:
                click.echo(output_str)
            return session, False

    if (
        normalized.startswith(":")
        and not normalized.startswith(":prompt ")
        and not normalized.startswith(":goal ")
    ):
        output_str = "Unknown command. Type :help."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    # Governed turn prompt
    if live_manager is not None and getattr(live_manager, "is_active", False):
        live_manager.stop()

    is_goal = normalized.startswith(":goal ")
    if is_goal:
        prompt_text = normalized
        placeholder = "Routing goal to Architecture (claude)... (Ctrl+C to cancel)"
    elif normalized.startswith(":prompt "):
        prompt_text = normalized[8:].strip()
        placeholder = "Generating response... (Ctrl+C to cancel)"
    else:
        prompt_text = normalized
        placeholder = "Generating response... (Ctrl+C to cancel)"
    state.add_message("user", prompt_text)
    # Redraw at prompt submission time; reuse is_tty from top of function
    if is_tty:
        redraw_full_screen(
            session,
            state,
            clear=False,
            include_composer=True,
            composer_is_active=False,
            composer_placeholder=placeholder,
            composer_content="",
            live_manager=live_manager,
        )
    else:
        click.echo(render_user_message_str(prompt_text))
    op_id = "turn"

    try:
        turn_kwargs: dict[str, Any] = {}
        if not is_goal and hasattr(state, "messages") and state.messages:
            c_hist: list[dict[str, str]] = []
            for msg in state.messages:
                m_role = getattr(msg, "role", getattr(msg, "sender", None))
                m_content = getattr(msg, "content", getattr(msg, "text", None))
                if m_role in ("user", "assistant") and m_content and isinstance(m_content, str) and m_content.strip():
                    c_hist.append({"role": m_role, "content": m_content.strip()})
            if c_hist:
                turn_kwargs["chat_history"] = c_hist[-12:]
        result = adapter.command(normalized, session_id=session["session_id"], **turn_kwargs)
        op_id = result.get("operation_id", "turn") if isinstance(result, dict) else "turn"
        if is_goal:
            state.add_activity("User", "submitted goal to Architecture", op_id)
            # Convention note: state.roles keys are Title-cased ("Architecture", "Q/A").
            # The daemon registers operations under lowercase ("architecture").
            if op_id and op_id not in state.operations:
                arch_backend = (
                    state.roles["Architecture"].backend
                    if "Architecture" in state.roles and state.roles["Architecture"].backend
                    else "Claude"
                )
                state.operations[op_id] = OperationNode(
                    op_id, "Goal", role="Architecture", backend=arch_backend, status="RUNNING"
                )
            if "Architecture" in state.roles:
                state.roles["Architecture"].state = "RUNNING"
        else:
            state.add_activity("User", "submitted turn to Architecture", op_id)
            if op_id and op_id not in state.operations:
                arch_backend = (
                    state.roles["Architecture"].backend
                    if "Architecture" in state.roles and state.roles["Architecture"].backend
                    else "Claude"
                )
                state.operations[op_id] = OperationNode(
                    op_id, "Turn", role="Architecture", backend=arch_backend, status="RUNNING"
                )
            if "Architecture" in state.roles:
                state.roles["Architecture"].state = "RUNNING"
        # This is a local wait indicator, not a claim about daemon state.
        if not is_tty:
            msg = (
                "● Routing goal to Architecture (claude)... Turn submitted to the governed daemon."
                if is_goal
                else "● Thinking (Architecture / claude)... Turn submitted to the governed daemon."
            )
            status_line = Text(msg, style="dim cyan")
            click.echo(status_line)

        return _stream_turn_execution(
            adapter,
            client,
            session,
            state,
            op_id=op_id,
            placeholder=placeholder,
            effective_timeout=effective_timeout,
            is_tty=is_tty,
            live_manager=live_manager,
            initial_activity_desc="Routing goal to Architecture" if is_goal else "Turn submitted",
        )
    except KeyboardInterrupt:
        click.echo()
        restore_composer_focus(state)
        return session, False
    except Exception as err:
        if is_connection_error(err):
            state.connection_status = "reconnecting"
            err_box = render_error_box_str(
                f"Could not submit turn: {err}",
                title="CONNECTION ERROR",
                hint="Daemon connection was lost. Use :reconnect to restore connection.",
            )
        else:
            err_box = render_error_box_str(
                f"Could not submit turn: {err}",
                title="TURN SUBMISSION ERROR",
                hint="An operation may already be in flight. Use :status, :cancel, or :reconnect.",
            )
        state.add_message("system", f"[ERROR] Could not submit turn: {err}")
        if is_tty:
            redraw_full_screen(
                session,
                state,
                clear=False,
                include_composer=True,
                composer_is_active=False,
                live_manager=live_manager,
            )
        else:
            click.echo(err_box)
    finally:
        if hasattr(state, "current_turn_actions") and state.current_turn_actions:
            state.current_turn_actions.complete_all()
        if live_manager is not None and not getattr(live_manager, "is_active", False):
            live_manager.start(include_composer=False)
    restore_composer_focus(state)
    return session, False


# Legacy dispatch adapter wrapper for backward compatibility with _dispatch_command
def _dispatch_command(
    adapter: StackMindTuiAdapter,
    client: DaemonClient,
    session: dict[str, Any],
    text: str,
    state: AutonomousDeliveryState | None = None,
    *,
    client_timeout: float = 0.0,
) -> tuple[dict[str, Any], bool]:
    if state is None:
        state = AutonomousDeliveryState(session_id=session.get("session_id", "session-1"))
        state.update_from_session(session)
    return dispatch_delivery_command(
        adapter, client, session, text, state, client_timeout=client_timeout
    )
