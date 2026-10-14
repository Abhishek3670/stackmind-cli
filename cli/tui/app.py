"""Autonomous Delivery TUI & Control Plane Integration (Milestone P7-4).

Implements the user-facing control plane for the StackMind governed runtime:
- Project Delivery View (Phase Status Banner, Agent Roles panel, Work Orders panel,
  Hierarchical Operation Tree, Live Activity Stream)
- Plan Approval Surface & Governance Controls (:plan, :approve, :reject, revision history)
- Role/Backend Surface (:roles)
- Hierarchical Operation Tree (:tree, :agents)
- Work Orders View (:wo)
- Project Completion Handover Surface (:completion)
- Reactive SSE Event Integration updating state on agent spawn & operation progress.

Modular architecture:
- cli.tui.views: pure delivery panel and surface rendering
- cli.tui.terminal: screen buffer, chrome, cursor, and interactive input
- cli.tui.commands: command dispatcher, execution loop, and status helpers
- cli.tui.app: daemon resolution, application lifecycle, and CLI entry point
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import click

from cli.daemon import DEFAULT_DAEMON_PORT, clear_daemon_pid, daemon_health, write_daemon_pid
from cli.tui.chat import clear_transcript_cache
from cli.tui.commands import (
    _build_in_progress_turn_content,
    _compute_blocker,
    _compute_next_action,
    _compute_progress,
    _compute_run_state,
    _dispatch_command,
    _show_status,
    dispatch_delivery_command,
    get_help_str,
    get_status_str,
    reconnect_and_sync,
)
from cli.tui.events import recover_transcript_from_events
from cli.tui.keyboard import raw_prompt_input
from cli.tui.landing import render_landing_block_str
from cli.tui.layout import (
    compute_composer_cursor_row,
    compute_layout,
    compute_viewport_height,
    render_workspace_layout_str,
)
from cli.tui.state import (
    ActivityEntry,
    AutonomousDeliveryState,
    ChatMessage,
    OperationNode,
    PlanRevision,
    ProjectPhase,
    RoleStatus,
    WorkOrderItem,
)
from cli.tui.terminal import (
    _compute_cursor_col,
    _ensure_utf8,
    _get_version,
    _sigwinch_handler,
    _wrap_text_to_width,
    check_terminal_resize,
    disable_mouse_reporting,
    enable_mouse_reporting,
    enter_alternate_screen,
    exit_alternate_screen,
    format_session_header,
    install_resize_handler,
    preserve_composer_buffer,
    prompt_composer_input,
    redraw_full_screen,
    render_bottom_footer_bar,
    render_bottom_footer_bar_str,
    render_composer_bottom_border,
    render_composer_bottom_border_str,
    render_composer_box,
    render_composer_box_str,
    render_composer_top_border,
    render_composer_top_border_str,
    render_top_header_bar,
    render_top_header_bar_str,
    restore_composer_focus,
    restore_terminal_state,
)
from cli.tui.views import (
    render_activity_stream,
    render_completion_surface,
    render_operation_tree,
    render_phase_banner,
    render_plan_surface,
    render_project_delivery_view,
    render_roles_panel,
    render_work_orders_panel,
)
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from validators.kernel.tui.views import diff_viewer, verification_matrix

_terminal_resized: bool = False


def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; all actions remain daemon RPC calls."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))


def resolve_tui_daemon(workspace: Path) -> tuple[LocalDaemon | None, str, bool]:
    """Attach to the default daemon, or start one owned by this TUI instance.

    Returns ``(daemon, url, is_attached)``. A daemon object is returned only
    when this TUI started it and is therefore responsible for stopping it.
    """
    default_url = f"http://127.0.0.1:{DEFAULT_DAEMON_PORT}"
    if daemon_health(default_url) is not None:
        return None, default_url, True

    state_dir = workspace.resolve() / ".sync" / "runtime" / "daemon"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        daemon = LocalDaemon(str(state_dir), port=DEFAULT_DAEMON_PORT).start()
    except OSError:
        click.echo(
            f"StackMind daemon port {DEFAULT_DAEMON_PORT} is occupied by a non-daemon service; "
            "using an ephemeral local daemon.",
            err=True,
        )
        daemon = LocalDaemon(str(state_dir), port=0).start()
    write_daemon_pid(workspace, daemon.address[1])
    return daemon, daemon.url, False


def _default_contract() -> dict[str, Any]:
    return {"allow": [], "deny": [], "write_mode": "governed"}


def _run_demo(client: DaemonClient, session: dict[str, Any]) -> None:
    """Render a deterministic walkthrough demonstrating Milestone P7-4 surfaces."""
    click.echo("StackMind TUI demo")
    state = AutonomousDeliveryState(project_name="my-project", session_id=session["session_id"])
    state.update_from_session(session)

    # 1. Show Session & Legacy HUD for backward compatibility
    _show_status(session, state=None)
    click.echo(
        "Verification: "
        + verification_matrix(
            {
                "scope": True,
                "state": True,
                "ast": True,
                "behavioral": True,
                "security": True,
                "outcome": True,
            }
        )
    )
    click.echo("Diff: " + diff_viewer("No staged daemon diff has been published."))

    # 2. Simulate Plan Proposal
    state.process_event(
        {
            "name": "plan.proposed",
            "payload": {
                "plan_id": "PLAN-001",
                "title": "Autonomous Delivery Foundation",
                "metadata": {
                    "work_orders": [
                        {
                            "id": "WO-001",
                            "title": "Architecture & Orchestration",
                            "role": "Architecture",
                        },
                        {"id": "WO-002", "title": "Backend API & Services", "role": "Backend"},
                        {"id": "WO-003", "title": "Frontend Client & Views", "role": "Frontend"},
                        {"id": "WO-004", "title": "Q/A Verification Suite", "role": "Q/A"},
                        {"id": "WO-005", "title": "GitOps Release & Hygiene", "role": "GitOps"},
                    ]
                },
            },
        }
    )

    # 3. Simulate Plan Approval
    state.process_event(
        {
            "name": "plan.approved",
            "payload": {
                "plan_id": "PLAN-001",
                "reason": "Operator approved delivery architecture",
                "created_records": [
                    {
                        "id": "WO-001",
                        "title": "Architecture & Orchestration",
                        "assigned_agents": ["claude"],
                    },
                    {
                        "id": "WO-002",
                        "title": "Backend API & Services",
                        "assigned_agents": ["codex"],
                    },
                    {
                        "id": "WO-003",
                        "title": "Frontend Client & Views",
                        "assigned_agents": ["gemini"],
                    },
                    {
                        "id": "WO-004",
                        "title": "Q/A Verification Suite",
                        "assigned_agents": ["gemma"],
                    },
                    {
                        "id": "WO-005",
                        "title": "GitOps Release & Hygiene",
                        "assigned_agents": ["local-llm"],
                    },
                ],
            },
        }
    )

    # 4. Simulate Subagent Spawns & Activity
    state.process_event(
        {
            "name": "event.agentSpawned",
            "payload": {
                "agent_id": "codex-sub-1",
                "role": "Backend",
                "backend": "Codex",
                "work_order_id": "WO-002",
            },
        }
    )
    state.process_event(
        {
            "name": "event.agentSpawned",
            "payload": {
                "agent_id": "gemini-sub-1",
                "role": "Frontend",
                "backend": "AGY",
                "work_order_id": "WO-003",
            },
        }
    )
    state.process_event(
        {"name": "tool_call.read", "payload": {"role": "Backend", "path": "auth/service.py"}}
    )
    state.process_event(
        {"name": "tool_call.edit", "payload": {"role": "Backend", "path": "auth/service.py"}}
    )
    state.process_event(
        {"name": "tool_call.create", "payload": {"role": "Frontend", "path": "LoginPage.tsx"}}
    )
    state.process_event(
        {"name": "tool_call.test", "payload": {"role": "Backend", "command": "pytest"}}
    )

    # 5. Render Primary Autonomous Delivery View
    click.echo("\n" + render_project_delivery_view(state))


@click.command("tui")
@click.option("--daemon-url", default=None, help="URL of an existing local daemon.")
@click.option("--agent", "-a", "agent", default="claude", show_default=True)
@click.option("--workspace", "-w", "workspace", type=click.Path(path_type=Path), default=Path("."))
@click.option(
    "--session-id",
    "-s",
    "target_session_id",
    default=None,
    help="Attach to a specific daemon session ID.",
)
@click.option("--demo", is_flag=True, help="Run the automated daemon-backed walkthrough.")
@click.option(
    "--timeout",
    "client_timeout",
    type=float,
    default=0,
    show_default=True,
    help="Turn operation wait timeout in seconds (0=no timeout).",
)
def tui(
    daemon_url: str | None,
    agent: str,
    workspace: Path,
    demo: bool,
    client_timeout: float = 0.0,
    target_session_id: str | None = None,
) -> None:
    """Start the governed Python-native terminal control plane."""
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    daemon: LocalDaemon | None = None
    is_attached = daemon_url is not None
    live_ws: Any | None = None
    if daemon_url is None:
        daemon, daemon_url, is_attached = resolve_tui_daemon(workspace)

    try:
        client = DaemonClient(daemon_url)
        adapter = StackMindTuiAdapter(client)

        session = None
        if target_session_id:
            try:
                session = client.get_session(target_session_id)
            except Exception as err:
                click.echo(
                    f"Warning: could not attach to session '{target_session_id}': {err}", err=True
                )

        if session is None:
            try:
                all_sessions = client.list_sessions()
                ws_resolved_posix = workspace.resolve().as_posix().lower()
                matching = [
                    s
                    for s in all_sessions
                    if Path(s.get("workspace", "")).resolve().as_posix().lower()
                    == ws_resolved_posix
                ]
                active_matching = [
                    s
                    for s in matching
                    if str(s.get("state", "")).upper() not in {"COMPLETED", "CANCELLED", "FAILED"}
                ]
                candidate = active_matching[-1] if active_matching else None
                if candidate and "session_id" in candidate:
                    session = client.get_session(candidate["session_id"])
            except Exception:
                session = None

        if session is None:
            session = client.create_session(
                agent=agent,
                provider="daemon",
                contract=_default_contract(),
                workspace=str(workspace.resolve()),
            )
        state = AutonomousDeliveryState(
            project_name=workspace.resolve().name,
            session_id=session["session_id"],
            work_orders=[],
        )
        state.client_timeout = client_timeout
        state.update_from_session(session)
        state.populate_from_runtime(client=client, session=session, workspace=workspace.resolve())

        # Recover any pre-existing events/transcript from daemon session
        try:
            init_events = client.events(session["session_id"], after=0)
            if init_events:
                recover_transcript_from_events(init_events, state, workspace=workspace.resolve())
        except Exception:
            pass

        _ensure_utf8()

        if demo:
            _run_demo(client, session)
            return

        is_tty = False
        try:
            is_tty = sys.stdout.isatty()
        except Exception:
            pass

        install_resize_handler()

        # The TUI owns the terminal while it is in the alternate screen.
        # Do not start Rich.Live here: Live and the raw msvcrt/termios composer
        # both move the cursor, so running them concurrently causes cursor
        # races, duplicate frames and broken composer placement. The workspace
        # is already a fixed-height frame and is redrawn atomically instead.
        live_ws = None

        if is_tty:
            _ensure_utf8()
            enter_alternate_screen()
            enable_mouse_reporting()
            redraw_full_screen(
                session, state, clear=True, include_composer=True, composer_is_active=True
            )
            current_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
            c_row = compute_composer_cursor_row(current_lines, state=state)
            # WO-022: fixed column — layout is flush at column 0 across all tiers.
            sys.stdout.write(f"\x1b[{c_row};5H")
            sys.stdout.flush()
        else:
            term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
            click.echo(
                render_top_header_bar_str(
                    session,
                    width=term_cols,
                    context_meter=(state.context_meter_text, state.context_warning_level),
                )
            )

            layout = compute_layout(term_cols)
            if layout.show_runtime:
                landing_str = render_landing_block_str(
                    session=session,
                    status=state.connection_status,
                    width=layout.conversation_width,
                )
                click.echo(
                    render_workspace_layout_str(
                        landing_str,
                        width=term_cols,
                        state=state,
                        scroll=state.scroll,
                        conversation_scroll=state.conversation_scroll,
                    )
                )
            else:
                click.echo(
                    render_landing_block_str(
                        session=session,
                        status=state.connection_status,
                        width=term_cols,
                    )
                )

        term_size = shutil.get_terminal_size(fallback=(80, 24))
        current_cols, current_lines = term_size.columns, term_size.lines
        last_idle_poll_time = 0.0
        while True:
            try:
                resized, new_cols, new_lines = check_terminal_resize(current_cols, current_lines)
                if resized:
                    current_cols, current_lines = new_cols, new_lines
                    clear_transcript_cache()
                    if live_ws is not None and getattr(live_ws, "is_active", False):
                        live_ws.handle_resize(new_cols, new_lines)
                    elif is_tty:
                        redraw_full_screen(
                            session,
                            state,
                            width=new_cols,
                            height=new_lines,
                            clear=True,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=getattr(state, "composer_buffer", ""),
                            live_manager=live_ws,
                        )
                term_cols = current_cols
                layout_info = compute_layout(term_cols)
                conv_width = (
                    layout_info.conversation_width if layout_info.show_runtime else term_cols
                )
                comp_width = conv_width if layout_info.show_runtime else term_cols

                def _handle_ctrl_k() -> None:
                    out = get_help_str()
                    state.add_message("user", ":help")
                    state.add_message("system", out)
                    if is_tty:
                        redraw_full_screen(
                            session, state, clear=True, include_composer=False, live_manager=live_ws
                        )
                    else:
                        click.echo(out)

                def _handle_ctrl_l(editor: Any = None) -> None:
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        redraw_full_screen(
                            session,
                            state,
                            clear=True,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    else:
                        click.echo(
                            render_top_header_bar_str(
                                session,
                                width=term_cols,
                                context_meter=(
                                    state.context_meter_text,
                                    state.context_warning_level,
                                ),
                            )
                        )
                        l_layout = compute_layout(term_cols)
                        if l_layout.show_runtime:
                            l_str = render_landing_block_str(
                                session=session,
                                status=state.connection_status,
                                width=l_layout.conversation_width,
                            )
                            click.echo(
                                render_workspace_layout_str(
                                    l_str,
                                    width=term_cols,
                                    state=state,
                                    scroll=state.scroll,
                                    conversation_scroll=state.conversation_scroll,
                                    height=compute_viewport_height(term_lines, state=state),
                                )
                            )
                        else:
                            click.echo(
                                render_landing_block_str(
                                    session=session,
                                    status=state.connection_status,
                                    width=term_cols,
                                )
                            )
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_ctrl_o(editor: Any = None) -> None:
                    """Toggle thinking block expansion."""
                    state.toggle_thinking()
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_page_up(editor: Any = None) -> None:
                    """Scroll conversation viewport up by half a page."""
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    vp_height = compute_viewport_height(term_lines, state=state)
                    state.scroll_conversation_up(max(1, vp_height // 2))
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_page_down(editor: Any = None) -> None:
                    """Scroll conversation viewport down by half a page."""
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    vp_height = compute_viewport_height(term_lines, state=state)
                    state.scroll_conversation_down(max(1, vp_height // 2))
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_wheel_up(editor: Any = None) -> None:
                    """Scroll conversation viewport up by wheel step (3 lines)."""
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    state.scroll_conversation_up(3)
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_wheel_down(editor: Any = None) -> None:
                    """Scroll conversation viewport down by wheel step (3 lines)."""
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    state.scroll_conversation_down(3)
                    if is_tty:
                        current_text = (
                            "".join(editor.buffer)
                            if (
                                editor is not None
                                and isinstance(getattr(editor, "buffer", None), list)
                            )
                            else None
                        )
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=True,
                            composer_is_active=True,
                            composer_content=current_text,
                            live_manager=live_ws,
                        )
                        cursor_col = _compute_cursor_col(editor)
                        cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                        c_row = compute_composer_cursor_row(
                            term_lines,
                            state=state,
                            composer_content=current_text,
                            cursor_line=cursor_line,
                        )
                        sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                        sys.stdout.flush()
                    if editor is not None and hasattr(editor, "redraw_line"):
                        editor.redraw_line()

                def _handle_height_change(editor: Any = None) -> None:
                    """Dynamically redraw full screen when composer content height changes."""
                    if not is_tty:
                        return
                    current_text = (
                        "".join(editor.buffer)
                        if (
                            editor is not None
                            and isinstance(getattr(editor, "buffer", None), list)
                        )
                        else None
                    )
                    term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                    redraw_full_screen(
                        session,
                        state,
                        clear=False,
                        include_composer=True,
                        composer_is_active=True,
                        composer_content=current_text,
                        live_manager=live_ws,
                    )
                    cursor_col = _compute_cursor_col(editor)
                    cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                    c_row = compute_composer_cursor_row(
                        term_lines,
                        state=state,
                        composer_content=current_text,
                        cursor_line=cursor_line,
                    )
                    sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                    sys.stdout.flush()

                def _handle_idle(editor: Any = None) -> None:
                    if not is_tty or client is None:
                        return
                    sid = session.get("session_id")
                    if not sid:
                        return
                    nonlocal last_idle_poll_time
                    now = time.monotonic()
                    if now - last_idle_poll_time < 0.15:
                        return
                    last_idle_poll_time = now
                    try:
                        missed = client.events(sid, after=state.last_sequence)
                        did_update = False
                        if missed:
                            recover_transcript_from_events(
                                missed, state, workspace=workspace.resolve()
                            )
                            did_update = True
                        if workspace:
                            prev_wos = [(w.id, w.status) for w in state.work_orders]
                            state.sync_work_orders_from_disk(workspace.resolve())
                            new_wos = [(w.id, w.status) for w in state.work_orders]
                            if prev_wos != new_wos:
                                did_update = True
                        if did_update:
                            current_text = (
                                "".join(editor.buffer)
                                if (
                                    editor is not None
                                    and isinstance(getattr(editor, "buffer", None), list)
                                )
                                else None
                            )
                            term_lines = shutil.get_terminal_size(fallback=(80, current_lines)).lines
                            redraw_full_screen(
                                session,
                                state,
                                clear=False,
                                include_composer=True,
                                composer_is_active=True,
                                composer_content=current_text,
                                live_manager=live_ws,
                            )
                            cursor_col = _compute_cursor_col(editor)
                            cursor_line = getattr(editor, "current_visible_line_index", None) if editor else None
                            c_row = compute_composer_cursor_row(
                                term_lines,
                                state=state,
                                composer_content=current_text,
                                cursor_line=cursor_line,
                            )
                            sys.stdout.write(f"\x1b[{c_row};{cursor_col}H")
                            sys.stdout.flush()
                            if editor is not None and hasattr(editor, "redraw_line"):
                                editor.redraw_line()
                    except Exception:
                        pass

                text = prompt_composer_input(
                    width=comp_width,
                    terminal_width=term_cols,
                    state=state,
                    session=session,
                    on_ctrl_k=_handle_ctrl_k,
                    on_ctrl_l=_handle_ctrl_l,
                    on_ctrl_o=_handle_ctrl_o,
                    on_page_up=_handle_page_up,
                    on_page_down=_handle_page_down,
                    on_wheel_up=_handle_wheel_up,
                    on_wheel_down=_handle_wheel_down,
                    on_height_change=_handle_height_change,
                    on_idle=_handle_idle,
                    show_footer=True,
                    live_manager=live_ws,
                    full_screen=is_tty,
                )
            except (EOFError, KeyboardInterrupt):
                if live_ws is not None:
                    live_ws.stop()
                click.echo()
                break
            try:
                state.scroll_conversation_to_bottom()
                session, should_exit = dispatch_delivery_command(
                    adapter,
                    client,
                    session,
                    text,
                    state,
                    client_timeout=state.client_timeout,
                    live_manager=live_ws,
                )
                if should_exit:
                    if live_ws is not None:
                        live_ws.stop()
                    break
            except KeyboardInterrupt:
                click.echo()
                continue
            if is_tty:
                redraw_full_screen(
                    session,
                    state,
                    clear=False,
                    include_composer=True,
                    composer_is_active=True,
                    live_manager=live_ws,
                )
                current_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
                c_row = compute_composer_cursor_row(current_lines, state=state)
                sys.stdout.write(f"\x1b[{c_row};5H")
                sys.stdout.flush()
    finally:
        disable_mouse_reporting()
        if live_ws is not None:
            live_ws.stop()
        restore_terminal_state()
        if daemon is not None and not is_attached:
            daemon.stop()
            clear_daemon_pid(workspace.resolve(), os.getpid())
        if temporary_state is not None:
            temporary_state.cleanup()


__all__ = [
    "ActivityEntry",
    "AutonomousDeliveryState",
    "ChatMessage",
    "OperationNode",
    "PlanRevision",
    "ProjectPhase",
    "RoleStatus",
    "WorkOrderItem",
    "_build_in_progress_turn_content",
    "_compute_blocker",
    "_compute_cursor_col",
    "_compute_next_action",
    "_compute_progress",
    "_compute_run_state",
    "_default_contract",
    "_dispatch_command",
    "_ensure_utf8",
    "_get_version",
    "_run_demo",
    "_show_status",
    "_sigwinch_handler",
    "_wrap_text_to_width",
    "check_terminal_resize",
    "create_tui_adapter",
    "disable_mouse_reporting",
    "dispatch_delivery_command",
    "enable_mouse_reporting",
    "enter_alternate_screen",
    "exit_alternate_screen",
    "format_session_header",
    "get_help_str",
    "get_status_str",
    "install_resize_handler",
    "preserve_composer_buffer",
    "prompt_composer_input",
    "raw_prompt_input",
    "reconnect_and_sync",
    "redraw_full_screen",
    "render_activity_stream",
    "render_bottom_footer_bar",
    "render_bottom_footer_bar_str",
    "render_completion_surface",
    "render_composer_bottom_border",
    "render_composer_bottom_border_str",
    "render_composer_box",
    "render_composer_box_str",
    "render_composer_top_border",
    "render_composer_top_border_str",
    "render_operation_tree",
    "render_phase_banner",
    "render_plan_surface",
    "render_project_delivery_view",
    "render_roles_panel",
    "render_top_header_bar",
    "render_top_header_bar_str",
    "render_work_orders_panel",
    "resolve_tui_daemon",
    "restore_composer_focus",
    "restore_terminal_state",
    "tui",
]
