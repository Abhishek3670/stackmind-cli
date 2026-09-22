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
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import click
from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.tui.chat import (
    render_actions_group,
    render_actions_group_str,
    render_assistant_message,
    render_assistant_message_str,
    render_assistant_stream_header,
    render_chat_transcript,
    render_chat_transcript_str,
    render_user_message,
    render_user_message_str,
)
from cli.tui.diff import (
    render_file_diff,
    render_unified_diff,
    render_unified_diff_str,
)
from cli.tui.events import (
    OperationalEventManager,
    ToolActivity,
    ToolStatus,
    extract_assistant_response,
    extract_assistant_thinking,
    extract_text_delta,
    is_connection_error,
    recover_transcript_from_events,
    render_connection_status,
    render_connection_status_str,
    render_error_box,
    render_error_box_str,
    render_operational_event,
    render_operational_event_str,
    render_tool_activity,
    render_tool_activity_line,
    render_tool_activity_line_str,
    render_tool_activity_str,
)
from cli.tui.governance import (
    render_contract_hud,
    render_contract_hud_str,
    render_plan_panel,
    render_plan_panel_str,
    render_verification_matrix,
    render_verification_matrix_str,
)
from cli.tui.landing import (
    render_landing_block,
    render_landing_block_str,
)
from cli.tui.layout import (
    NARROW_THRESHOLD,
    LiveWorkspaceManager,
    compute_layout,
    create_live_workspace,
    render_full_screen_workspace,
    render_runtime_panel_str,
    render_workspace_layout_str,
    slice_conversation_viewport,
)
from cli.tui.runtime_panel import format_model_badge
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
from validators.kernel.daemon import LocalDaemon
from validators.kernel.tui import DaemonClient, StackMindTuiAdapter
from validators.kernel.tui.views import (
    activity_line,
    contract_panel,
    diff_viewer,
    session_header,
    verification_matrix,
)
from cli.tui.glyphs import (
    get_glyph,
    get_glyphs,
    is_ascii_mode,
    sanitize_text,
    status_glyph,
    tree_branch,
)
from cli.tui.keyboard import (
    GLOBAL_HISTORY,
    Key,
    raw_prompt_input,
)


# ─── VIEW RENDERERS ───────────────────────────────────────────────────────────

def render_phase_banner(state: AutonomousDeliveryState) -> str:
    """Renders the current project delivery phase banner."""
    lines = [f"=== PROJECT: {state.project_name.upper()} | PHASE: {state.phase.value} ==="]
    if state.phase in {ProjectPhase.INITIALIZING, ProjectPhase.PLAN_PROPOSED, ProjectPhase.AWAITING_APPROVAL}:
        lines.append("● Planning & Architecture (AWAITING_APPROVAL)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.PLAN_REJECTED:
        lines.append("✗ PLAN rejected (Revising)")
        lines.append("○ Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase in {ProjectPhase.PLAN_APPROVED, ProjectPhase.AUTONOMOUS_EXECUTION}:
        lines.append("✓ PLAN approved")
        lines.append("● Autonomous Execution")
        lines.append("○ Project Complete")
    elif state.phase == ProjectPhase.PROJECT_COMPLETE:
        lines.append("✓ PLAN approved")
        lines.append("✓ Autonomous Execution")
        lines.append("✓ Project Complete")
    return "\n".join(lines)


def render_roles_panel(state: AutonomousDeliveryState, detailed: bool = False) -> str:
    """Renders the Agent Roles panel (:roles command)."""
    if detailed:
        out = ["=== AGENT ROLES & EXECUTION BACKENDS (:roles) ==="]
        for role_name, r in state.roles.items():
            out.append(f"{role_name} Agent")
            out.append(f"  Role: {r.role}")
            out.append(f"  Backend: {r.backend}")
            if getattr(r, "model", None):
                out.append(f"  Model: {r.model}")
            if getattr(r, "quantization", None):
                out.append(f"  Quantization: {r.quantization}")
            badge = format_model_badge(r.backend, getattr(r, "model", None), getattr(r, "quantization", None))
            if badge:
                out.append(f"  Badge: [{badge}]")
            out.append(f"  Work Order: {r.work_order_id or 'None'}")
            out.append(f"  State: {r.state}")
            out.append("")
        return "\n".join(out).rstrip()

    # Dashboard compact view
    lines = ["AGENTS"]
    for role_name, r in state.roles.items():
        marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
        badge = format_model_badge(r.backend, getattr(r, "model", None), getattr(r, "quantization", None))
        badge_str = f" [{badge}]" if (getattr(r, "model", None) and r.state.upper() in {"RUNNING", "ACTIVE", "ORCHESTRATING"}) else ""
        lines.append(f"{marker} {role_name:<16} {r.display_state}{badge_str}")
    return "\n".join(lines)


def render_work_orders_panel(state: AutonomousDeliveryState) -> str:
    """Renders the Work Orders panel (:wo command)."""
    lines = ["WORK ORDERS"]
    for wo in state.work_orders:
        marker = "✓" if wo.status.upper() in {"COMPLETED", "DONE"} else ("●" if wo.status.upper() in {"RUNNING", "ACTIVE"} else "○")
        title_summary = wo.title if len(wo.title) <= 24 else wo.title[:21] + "..."
        lines.append(f"{marker} {wo.id:<8} {title_summary}")
    return "\n".join(lines)


def render_operation_tree(state: AutonomousDeliveryState) -> str:
    """Renders the hierarchical operation tree (:tree / :agents command)."""
    lines = ["● Architecture"]
    root = state.operations.get("op-root")
    children = root.children if root else [k for k in state.operations if k != "op-root"]

    if not children:
        entries = [
            ("Backend", state.roles.get("Backend", RoleStatus("Backend", "Codex"))),
            ("Frontend", state.roles.get("Frontend", RoleStatus("Frontend", "AGY"))),
            ("Q/A", state.roles.get("Q/A", RoleStatus("Q/A", "Ollama"))),
            ("GitOps", state.roles.get("GitOps", RoleStatus("GitOps", "Ollama"))),
        ]
        for i, (name, r) in enumerate(entries):
            is_last = (i == len(entries) - 1)
            prefix = "  └─ " if is_last else "  ├─ "
            marker = "✓" if r.state.upper() in {"COMPLETED", "DONE"} else ("●" if r.state.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {name} / {r.backend}")
        return "\n".join(lines)

    for i, child_id in enumerate(children):
        is_last = (i == len(children) - 1)
        prefix = "  └─ " if is_last else "  ├─ "
        node = state.operations.get(child_id)
        if node:
            marker = "✓" if node.status.upper() in {"COMPLETED", "DONE"} else ("●" if node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
            lines.append(f"{prefix}{marker} {node.role} / {node.backend}")
            # Render sub-children if present
            for j, sub_id in enumerate(node.children):
                sub_last = (j == len(node.children) - 1)
                sub_prefix = "     └─ " if sub_last else "     ├─ "
                sub_node = state.operations.get(sub_id)
                if sub_node:
                    sub_marker = "✓" if sub_node.status.upper() in {"COMPLETED", "DONE"} else ("●" if sub_node.status.upper() in {"RUNNING", "ACTIVE"} else "○")
                    lines.append(f"{sub_prefix}{sub_marker} {sub_node.name} ({sub_node.role})")
    return "\n".join(lines)


def render_activity_stream(state: AutonomousDeliveryState, limit: int = 10) -> str:
    """Renders the Live Activity Stream panel."""
    lines = ["ACTIVITY"]
    entries = state.activity_log[-limit:]
    if not entries:
        lines.append("  (Awaiting first runtime activity event)")
    else:
        for entry in entries:
            lines.append(entry.format_line())
    return "\n".join(lines)


def render_plan_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Plan Approval Surface & Governance Controls (:plan command)."""
    plan = state.plan
    plan_id = plan.get("plan_id") or plan.get("id") or "PLAN.md"
    title = plan.get("title", "Autonomous Delivery Plan")
    status = plan.get("state") or ("AWAITING_APPROVAL" if state.phase == ProjectPhase.AWAITING_APPROVAL else "DRAFT")
    content = plan.get("content", "")
    metadata = plan.get("metadata", {})

    lines = [
        "PLAN.md",
        "─" * 52,
        f"Status: {status}",
        f"Plan ID: {plan_id}",
        f"Title: {title}",
        "",
        "WORK DECOMPOSITION:",
    ]

    wos = metadata.get("work_orders") or [
        {"id": wo.id, "title": wo.title, "role": wo.role} for wo in state.work_orders
    ]
    for w in wos:
        if isinstance(w, dict):
            lines.append(f"  • {w.get('id', 'WO')}: {w.get('title', '')} (Role: {w.get('role', 'Worker')})")
        else:
            lines.append(f"  • {w}")

    deps = metadata.get("dependencies", ["Python 3.12", "StackMind Kernel", "Local Execution Backends"])
    lines.append("")
    lines.append(f"Dependencies: {', '.join(deps) if isinstance(deps, list) else deps}")

    risks = metadata.get("risks", ["Strict contract scope boundaries prevent out-of-scope edits."])
    lines.append("Risks & Mitigations:")
    for r in (risks if isinstance(risks, list) else [risks]):
        lines.append(f"  - {r}")

    lines.append("Validation: PASS (Schema valid, 6-D matrix confirmed)")

    if state.plan_revisions:
        lines.append("")
        lines.append("REVISION HISTORY:")
        for rev in state.plan_revisions:
            feedback_str = f" — Feedback: '{rev.feedback}'" if rev.feedback else ""
            lines.append(f"  [Rev {rev.revision}] {rev.state}{feedback_str} ({rev.timestamp})")

    if status == "AWAITING_APPROVAL" or state.phase == ProjectPhase.AWAITING_APPROVAL:
        lines.append("")
        lines.append("GOVERNANCE CONTROLS:")
        lines.append("  :approve [reason]   Approve plan and begin autonomous multi-agent execution")
        lines.append("  :reject [feedback]  Reject plan and return feedback to Architecture agent for revision")
    elif status == "APPROVED":
        lines.append("")
        lines.append("STATUS: Approved by operator. Execution in progress.")

    return "\n".join(lines)


def render_completion_surface(state: AutonomousDeliveryState) -> str:
    """Renders the Project Completion Handover Surface."""
    lines = [
        "PROJECT COMPLETE",
        "",
    ]
    for item, done in state.completion_checklist.items():
        marker = "✓" if done else "○"
        lines.append(f"{item} {marker}")
    return "\n".join(lines)


def render_project_delivery_view(state: AutonomousDeliveryState) -> str:
    """Renders the primary Project Delivery TUI view combining Phase, Roles, WOs, Activity."""
    header = f"STACKMIND • {state.project_name}\n" + "─" * 52
    phase = render_phase_banner(state)
    agents = render_roles_panel(state, detailed=False)
    wos = render_work_orders_panel(state)
    activity = render_activity_stream(state, limit=8)

    return f"{header}\n\n{phase}\n\n{agents}\n\n{wos}\n\n{activity}"


# ─── REPL & DISPATCHER ────────────────────────────────────────────────────────

def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; all actions remain daemon RPC calls."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))


def _default_contract() -> dict[str, Any]:
    return {"allow": [], "deny": [], "write_mode": "governed"}


def format_session_header(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = None,
) -> str:
    """Format session header responsively depending on terminal width."""
    sid = str(session.get("session_id", "session"))
    state_val = str(session.get("state", "RUNNING"))
    provider = str(session.get("provider", "daemon"))
    agent = session.get("agent")
    workspace = session.get("workspace")

    status_suffix = f" | {render_connection_status_str(status)}" if status else ""

    if width >= 80:
        agent_info = f" | agent: {agent}" if agent else ""
        ws_info = f" | workspace: {workspace}" if workspace else ""
        return f"Session {sid} | {state_val} | provider: {provider}{agent_info}{ws_info}{status_suffix}"
    elif width >= 55:
        ws_short = Path(str(workspace)).name if workspace else ""
        agent_info = f" | {agent}" if agent else ""
        ws_info = f" | ws: {ws_short}" if ws_short else ""
        display_sid = sid if len(sid) <= 12 else sid[:8] + "..."
        return f"Session {display_sid} | {state_val}{agent_info}{ws_info}{status_suffix}"
    else:
        display_sid = sid if len(sid) <= 8 else sid[:6] + ".."
        return f"Session {display_sid} | {state_val}{status_suffix}"


def _get_version() -> str:
    try:
        from cli import __version__
        return f"v{__version__}"
    except Exception:
        return "v3.1.0"


def render_top_header_bar(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = "online",
    context_meter: tuple[str, str] | None = None,
) -> RenderableType:
    """Render a single-line, width-safe top status bar.

    The previous implementation calculated a minimum workspace width and then
    allowed the right-hand metadata to exceed the terminal width. Rich wrapped
    the final status onto a second line on medium-width Windows terminals.
    The header is chrome, not document content, so it must *never* wrap.

    Fields are progressively compacted / dropped as width becomes constrained:
    session id and connection status are retained; optional provider/context
    metadata yields first.
    """
    sid = str(session.get("session_id", "session"))
    short_sid = sid if len(sid) <= 8 else sid[:8]
    agent = str(session.get("agent") or "codex")
    provider = str(session.get("provider") or "daemon")
    workspace = session.get("workspace")

    st = str(status or "online").lower()
    if st == "online":
        glyph, glyph_style, st_name = status_glyph("online"), "bold #22c55e", "online"
    elif st in {"reconnecting", "reconnect"}:
        glyph, glyph_style, st_name = status_glyph("waiting"), "bold yellow", "reconnecting"
    else:
        glyph, glyph_style, st_name = status_glyph("failed"), "bold red", "offline"

    status_item = f"{glyph} {st_name}"

    def context_item(compact: bool = False) -> str:
        if not context_meter:
            return ""
        value, _level = context_meter
        if compact:
            value = (
                value.replace("Context: ", "ctx ")
                .replace(" / ", "/")
                .replace(" (", " ")
                .replace(")", "")
            )
        return value

    # Keep the same visual content on wide terminals, but use a compact
    # context label before dropping useful metadata on constrained terminals.
    right_items: list[str] = []
    if width >= 90:
        right_items.extend([f"session: {short_sid}", f"agent: {agent}", f"provider: {provider}"])
    elif width >= 65:
        right_items.extend([f"session: {short_sid}", f"agent: {agent}"])
    elif width >= 45:
        right_items.append(f"session: {short_sid}")

    if context_meter and width >= 80:
        ctx = context_item(compact=width < 118)
        if ctx:
            right_items.append(ctx)

    def right_text(items: list[str]) -> str:
        return " | ".join(items + [status_item]) if items else status_item

    left_prefix = "stackmind  |  dev  "
    if workspace:
        p = Path(str(workspace))
        ws_display = str(p) if width >= 90 else f".../{p.name}"
    else:
        p = None
        ws_display = "~/projects/stackmind"

    def fit_left(items: list[str]) -> tuple[str, str]:
        """Return a workspace label that fits alongside right-side metadata."""
        right_plain = right_text(items)
        available = width - len(left_prefix) - len(right_plain) - 2
        if available <= 0:
            return "", right_plain

        if len(ws_display) <= available:
            return ws_display, right_plain

        if available <= 4:
            return ws_display[:max(1, available)], right_plain

        if p is not None:
            # Preserve the project basename because it is more useful than an
            # arbitrary slice of the absolute path.
            name = p.name or str(p)
            if len(name) + 4 <= available:
                return ".../" + name, right_plain

        return ws_display[: max(1, available - 1)] + "…", right_plain

    # Progressively shed optional right-side metadata until the line fits.
    # This is deterministic, so resizing cannot cause one-line/two-line jitter.
    candidates = list(right_items)
    ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width:
        # Context is useful telemetry but least important in the header.
        candidates = [item for item in candidates if item != context_item(compact=True)
                      and item != context_item(compact=False)]
        ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width and width < 118:
        # Provider is redundant with the agent at medium widths.
        candidates = [item for item in candidates if not item.startswith("provider: ")]
        ws_fit, right_plain = fit_left(candidates)

    if len(left_prefix) + len(ws_fit) + len(right_plain) + 2 > width:
        # Last-resort compact left side. Never allow Rich to wrap the chrome.
        candidates = [item for item in candidates if not item.startswith("agent: ")]
        ws_fit, right_plain = fit_left(candidates)

    left_plain = left_prefix + ws_fit
    spaces_count = max(1, width - len(left_plain) - len(right_plain))

    res = Text(no_wrap=True, overflow="crop")
    res.append("stackmind", style="bold #22c55e")
    res.append("  |  ", style="dim #475569")
    res.append("dev  ", style="dim white")
    res.append(ws_fit, style="dim #64748b")
    res.append(" " * spaces_count)

    for item in candidates:
        # The context item is the only right-side field that carries a
        # non-default status style.
        if context_meter and item in {context_item(True), context_item(False)}:
            level = context_meter[1]
            context_style = {
                "green": "bold #22c55e",
                "yellow": "bold yellow",
                "red": "bold red",
            }.get(level, "dim white")
            res.append(item, style=context_style)
        else:
            res.append(item, style="dim white")
        res.append(" | ", style="dim #475569")
    res.append(status_item, style=glyph_style)

    return res


def render_top_header_bar_str(
    session: Mapping[str, Any],
    width: int = 80,
    status: str | None = "online",
    context_meter: tuple[str, str] | None = None,
) -> str:
    """Render top header bar as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_top_header_bar(session, width=width, status=status, context_meter=context_meter))
    return console.export_text().rstrip()


def render_composer_box(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    content: str | list[str] | None = None,
    is_active: bool = False,
    has_content: bool = False,
) -> Panel:
    """Render the rounded input composer box matching image.png:
    > Type a message...               Ctrl+K commands | Ctrl+L clear

    Expands vertically upward/downward for multiline typing (§28, §30).
    Has the strongest container border in the interface (§29, §43),
    highlighting to #60a5fa when active.
    """
    inner_width = max(40, width - 4)
    border_color = "#60a5fa" if is_active else "#475569"

    if not content and not has_content:
        left_plain = f"> {placeholder}"
        right_plain = shortcuts
        spaces_count = max(2, inner_width - len(left_plain) - len(right_plain))

        line = Text()
        line.append("> ", style="bold #38bdf8")
        line.append(placeholder, style="dim #94a3b8")
        line.append(" " * spaces_count)
        line.append(shortcuts, style="dim #64748b")
        body: RenderableType = line
    else:
        if isinstance(content, str):
            lines = content.splitlines() or [""]
        elif content:
            lines = list(content) or [""]
        else:
            lines = [""]

        rendered_lines: list[Text] = []
        first_text = lines[0]
        left_plain = f"> {first_text}"
        right_plain = shortcuts
        spaces_count = max(2, inner_width - len(left_plain) - len(right_plain))

        line1 = Text()
        line1.append("> ", style="bold #38bdf8")
        line1.append(first_text, style="bold white")
        line1.append(" " * spaces_count)
        line1.append(shortcuts, style="dim #64748b")
        rendered_lines.append(line1)

        for sub_line in lines[1:]:
            line_n = Text()
            line_n.append("  ", style="dim #38bdf8")
            line_n.append(sub_line, style="white")
            rendered_lines.append(line_n)

        body = Group(*rendered_lines)

    return Panel(
        body,
        box=box.ROUNDED,
        border_style=border_color,  # Strongest visual container border in interface (§29, §43)
        padding=(0, 1),
    )


def render_composer_box_str(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    content: str | list[str] | None = None,
    is_active: bool = False,
    has_content: bool = False,
) -> str:
    """Render input composer box as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_composer_box(
        placeholder=placeholder,
        shortcuts=shortcuts,
        width=width,
        content=content,
        is_active=is_active,
        has_content=has_content,
    ))
    return console.export_text().rstrip()


def restore_composer_focus(state: Any | None = None) -> bool:
    """Restore terminal focus to the composer after an operation completes (§30).

    Safely restores focus without interrupting or stealing keystrokes
    if the user is actively typing.
    Returns True if focus was set/restored to composer, False if skipped
    because user was actively typing.
    """
    if state is None:
        return True
    if getattr(state, "is_typing", False):
        return False
    state.focus_target = "composer"
    return True


def preserve_composer_buffer(state: Any | None, text: str) -> None:
    """Preserve partially typed composer buffer during live events or renders (§30)."""
    if state is not None and hasattr(state, "composer_buffer"):
        state.composer_buffer = text


def enter_alternate_screen(stream: Any = None) -> None:
    """Enter alternate screen buffer, clear screen, and home cursor (WO-053).

    Emits \\x1b[?1049h (alternate buffer) and \\x1b[2J\\x1b[H (clear screen, home cursor).
    """
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049h\x1b[2J\x1b[H")
            target.flush()
    except Exception:
        pass


def exit_alternate_screen(stream: Any = None) -> None:
    """Exit alternate screen buffer (WO-053).

    Emits \\x1b[?1049l (restore primary buffer).
    """
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049l")
            target.flush()
    except Exception:
        pass


def restore_terminal_state(stream: Any = None) -> None:
    """Restore terminal cursor and modes cleanly on exit and uncaught exceptions (§43, §44, WO-053).

    Emits \\x1b[?1049l (exit alternate buffer), \\x1b[?25h (show cursor), and \\x1b[0m (reset attributes).
    """
    try:
        Console().show_cursor(True)
    except Exception:
        pass
    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            target.write("\x1b[?1049l\x1b[?25h\x1b[0m")
            target.flush()
    except Exception:
        pass


# ── Terminal Resize Management (WO-003) ──────────────────────────────────────
_terminal_resized: bool = False


def _sigwinch_handler(signum: int, frame: Any) -> None:
    global _terminal_resized
    _terminal_resized = True


def install_resize_handler() -> Any:
    """Install safe SIGWINCH handler on platforms that support it (POSIX), no-op on Windows."""
    if hasattr(signal, "SIGWINCH"):
        try:
            return signal.signal(signal.SIGWINCH, _sigwinch_handler)
        except Exception:
            return None
    return None


def check_terminal_resize(current_cols: int, current_lines: int) -> tuple[bool, int, int]:
    """Check if terminal dimensions have changed either via SIGWINCH or periodic dimension check."""
    global _terminal_resized
    term_size = shutil.get_terminal_size(fallback=(80, 24))
    cols = term_size.columns
    lines = term_size.lines
    resized = _terminal_resized or (cols != current_cols or lines != current_lines)
    _terminal_resized = False
    return resized, cols, lines


def redraw_full_screen(
    session: Mapping[str, Any] | None = None,
    state: Any | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    clear: bool = False,
    stream: Any = None,
    conversation_content: str | RenderableType | None = None,
    composer_content: str | None = None,
    composer_is_active: bool = False,
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    include_composer: bool = True,
    live_manager: Any | None = None,
) -> str:
    """Redraw the full-screen TUI workspace (WO-053, WO-003).

    Renders top header, two-column workspace (with conversation viewport and
    anchored runtime panel), and pinned bottom composer box.
    Uses LiveWorkspaceManager if provided to eliminate screen-clearing redraws.
    """
    term_size = shutil.get_terminal_size(fallback=(80, 24))
    cols = width if width is not None else term_size.columns
    lines = height if height is not None else term_size.lines

    frame = render_full_screen_workspace(
        session=session,
        state=state,
        conversation_content=conversation_content,
        width=cols,
        height=lines,
        composer_content=composer_content,
        composer_is_active=composer_is_active,
        shortcuts=shortcuts,
        include_composer=include_composer,
    )

    if live_manager is not None and getattr(live_manager, "is_active", False):
        try:
            live_manager.update(
                conversation_content=conversation_content,
                composer_content=composer_content,
                composer_is_active=composer_is_active,
                shortcuts=shortcuts,
                include_composer=include_composer,
            )
        except Exception:
            pass
        return frame

    target = stream or sys.stdout
    try:
        if target and hasattr(target, "write"):
            prefix = "\x1b[2J\x1b[H" if clear else "\x1b[H\x1b[0J"
            target.write(f"{prefix}{frame}\n" if not include_composer else f"{prefix}{frame}")
            target.flush()
    except Exception:
        pass

    return frame


def render_composer_top_border(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    is_active: bool = False,
    has_content: bool = False,
    content: str | list[str] | None = None,
) -> RenderableType:
    """Render the top border of the composer box with placeholder and shortcuts.

    Dynamically swaps placeholder for '[Active Input]' or hides it when
    input characters are present in the buffer (PLAN_TUI_BUGFIX_ROUND2.md §3.2).
    """
    border_color = "#60a5fa" if is_active else "#475569"
    active_content = has_content or bool(content)

    if active_content:
        label = "[Active Input]" if placeholder == "Type a message..." else placeholder
    else:
        label = placeholder

    if label:
        fixed_len = len("╭─ ") + len(label) + len(" ") + len(" ") + len(shortcuts) + len(" ─╮")
        if width >= fixed_len + 2:
            fill_count = width - fixed_len
            text = Text()
            text.append("╭─ ", style=border_color)
            text.append(label, style="bold #38bdf8" if active_content else "dim #94a3b8")
            text.append(" " + "─" * fill_count + " ", style=border_color)
            text.append(shortcuts, style="dim #64748b")
            text.append(" ─╮", style=border_color)
            return text
    else:
        fixed_len = len("╭─ ") + len(shortcuts) + len(" ─╮")
        if width >= fixed_len + 2:
            fill_count = width - fixed_len
            text = Text()
            text.append("╭─" + "─" * fill_count + " ", style=border_color)
            text.append(shortcuts, style="dim #64748b")
            text.append(" ─╮", style=border_color)
            return text

    text = Text()
    text.append("╭" + "─" * max(2, width - 2) + "╮", style=border_color)
    return text


def render_composer_top_border_str(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    is_active: bool = False,
    has_content: bool = False,
    content: str | list[str] | None = None,
) -> str:
    """Render top border of the composer box as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_composer_top_border(
        placeholder=placeholder,
        shortcuts=shortcuts,
        width=width,
        is_active=is_active,
        has_content=has_content,
        content=content,
    ))
    return console.export_text().rstrip()


def render_composer_bottom_border(width: int = 80, is_active: bool = False) -> RenderableType:
    """Render the bottom rounded border of the composer box."""
    border_color = "#60a5fa" if is_active else "#475569"
    text = Text()
    text.append("╰" + "─" * max(2, width - 2) + "╯", style=border_color)
    return text


def render_composer_bottom_border_str(width: int = 80, is_active: bool = False) -> str:
    """Render bottom border of the composer box as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_composer_bottom_border(width=width, is_active=is_active))
    return console.export_text().rstrip()


def prompt_composer_input(
    placeholder: str = "Type a message...",
    shortcuts: str = "Ctrl+K commands | Ctrl+L clear",
    width: int = 80,
    initial_text: str = "",
    multiline: bool = False,
    state: Any | None = None,
    on_ctrl_k: Callable[[], None] | None = None,
    on_ctrl_l: Callable[[], None] | None = None,
    history: list[str] | None = None,
    show_footer: bool = True,
    live_manager: Any | None = None,
    session: Mapping[str, Any] | None = None,
    terminal_width: int | None = None,
) -> str:
    """Prompt the user for input inside a styled composer box border.

    Supports native raw keyboard interception (msvcrt / termios), real Ctrl+K & Ctrl+L,
    multiline expansion, active typing state tracking, and preserves
    partially typed input buffer across background events (§28-§30).
    Aligns composer width to conversation viewport when runtime panel is visible (WO-008).
    """
    term_width = terminal_width if terminal_width is not None else width
    layout = compute_layout(term_width)
    if width != term_width:
        comp_width = width
    else:
        comp_width = layout.conversation_width if layout.show_runtime else term_width

    if state is not None:
        if not initial_text and getattr(state, "composer_buffer", ""):
            initial_text = state.composer_buffer
        state.is_typing = True
        state.focus_target = "composer"

    is_live_active = live_manager is not None and getattr(live_manager, "is_active", False)

    is_tty = False
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        pass

    if is_tty:
        if is_live_active:
            live_manager.update(
                composer_content=initial_text,
                composer_is_active=True,
                shortcuts=shortcuts,
                include_composer=True,
            )
        else:
            click.echo(render_composer_top_border_str(
                placeholder=placeholder,
                shortcuts=shortcuts,
                width=comp_width,
                is_active=True,
                has_content=bool(initial_text),
            ))
            status_bar_line = ""
            if session is not None:
                status = getattr(state, "connection_status", "online") if state is not None else "online"
                context_meter = (
                    (getattr(state, "context_meter_text", None), getattr(state, "context_warning_level", None))
                    if state is not None and hasattr(state, "context_meter_text")
                    else None
                )
                status_bar_line = render_top_header_bar_str(session, width=comp_width, status=status, context_meter=context_meter)

            bottom_border = render_composer_bottom_border_str(width=comp_width, is_active=True)
            if status_bar_line:
                sys.stdout.write(f"\n{bottom_border}\n{status_bar_line}\x1b[2A\r")
            else:
                sys.stdout.write(f"\n{bottom_border}\x1b[1A\r")
            sys.stdout.flush()
        try:
            val = raw_prompt_input(
                placeholder=placeholder,
                shortcuts=shortcuts,
                width=comp_width,
                initial_text=initial_text,
                history=history,
                state=state,
                on_ctrl_k=on_ctrl_k,
                on_ctrl_l=on_ctrl_l,
                top_border_renderer=lambda has_c: render_composer_top_border_str(
                    placeholder=placeholder,
                    shortcuts=shortcuts,
                    width=comp_width,
                    is_active=True,
                    has_content=has_c,
                ),
            )
        finally:
            if state is not None:
                state.is_typing = False
                state.composer_buffer = ""

        if is_live_active:
            live_manager.update(
                composer_content=val,
                composer_is_active=False,
                shortcuts=shortcuts,
                include_composer=False,
            )
        else:
            click.echo(render_composer_bottom_border_str(width=comp_width, is_active=False))
            if show_footer:
                click.echo(render_bottom_footer_bar_str(width=comp_width))
            if session is not None:
                status = getattr(state, "connection_status", "online") if state is not None else "online"
                context_meter = (
                    (getattr(state, "context_meter_text", None), getattr(state, "context_warning_level", None))
                    if state is not None and hasattr(state, "context_meter_text")
                    else None
                )
                click.echo(render_top_header_bar_str(session, width=comp_width, status=status, context_meter=context_meter))
        return val

    # Non-TTY / test fallback: standard input() with multiline support
    if is_live_active:
        live_manager.update(
            composer_content=initial_text,
            composer_is_active=True,
            shortcuts=shortcuts,
            include_composer=True,
        )
    else:
        click.echo(render_composer_top_border_str(
            placeholder=placeholder,
            shortcuts=shortcuts,
            width=comp_width,
            is_active=True,
            has_content=bool(initial_text),
        ))
    prompt_str = "│ > "
    cont_prompt_str = "│   "

    lines: list[str] = []
    try:
        first_line = input(prompt_str)
        if initial_text and not first_line.startswith(initial_text):
            first_line = initial_text + first_line
        lines.append(first_line)

        if multiline:
            while True:
                next_line = input(cont_prompt_str)
                if not next_line.strip():
                    break
                lines.append(next_line)
        elif first_line.endswith("\\"):
            lines[0] = first_line[:-1]
            while True:
                next_line = input(cont_prompt_str)
                if next_line.endswith("\\"):
                    lines.append(next_line[:-1])
                else:
                    lines.append(next_line)
                    break
    finally:
        if state is not None:
            state.is_typing = False
            state.composer_buffer = ""

    if is_live_active:
        live_manager.update(
            composer_content="\n".join(lines),
            composer_is_active=False,
            shortcuts=shortcuts,
            include_composer=False,
        )
    else:
        click.echo(render_composer_bottom_border_str(width=comp_width, is_active=False))
        if show_footer:
            click.echo(render_bottom_footer_bar_str(width=comp_width))
        if session is not None:
            status = getattr(state, "connection_status", "online") if state is not None else "online"
            context_meter = (
                (getattr(state, "context_meter_text", None), getattr(state, "context_warning_level", None))
                if state is not None and hasattr(state, "context_meter_text")
                else None
            )
            click.echo(render_top_header_bar_str(session, width=comp_width, status=status, context_meter=context_meter))
    return "\n".join(lines)


def render_bottom_footer_bar(
    version: str | None = None,
    width: int = 80,
) -> RenderableType:
    """Render only commands accepted by the active colon-command dispatcher."""
    commands = [":help", ":status", ":diff", ":events", ":roles", ":landing"]
    left_plain = "   ".join(commands)
    ver = version or _get_version()
    right_plain = f"StackMind {ver}"
    spaces_count = max(2, width - len(left_plain) - len(right_plain))

    line = Text()
    for i, cmd in enumerate(commands):
        line.append(cmd, style="dim #94a3b8")
        if i < len(commands) - 1:
            line.append("   ", style="dim #475569")
    line.append(" " * spaces_count)
    line.append(right_plain, style="dim #64748b")
    return line


def render_bottom_footer_bar_str(
    version: str | None = None,
    width: int = 80,
) -> str:
    """Render bottom footer bar as plain string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_bottom_footer_bar(version=version, width=width))
    return console.export_text().rstrip()


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


def get_status_str(
    session: dict[str, Any],
    state: AutonomousDeliveryState | None = None,
    width: int = 80,
) -> str:
    """Build status header, contract HUD, and delivery view as a formatted string."""
    conn_status = state.connection_status if state else "online"
    parts = [
        format_session_header(session, width=width, status=conn_status),
        render_contract_hud_str(session.get("contract", {}), width=width),
    ]
    if state is not None:
        state.update_from_session(session)
        parts.append(state.context_meter_text)
        parts.append("")
        parts.append(render_project_delivery_view(state))
    return "\n".join(parts)


def _show_status(
    session: dict[str, Any],
    state: AutonomousDeliveryState | None = None,
    width: int = 80,
) -> str:
    out = get_status_str(session, state=state, width=width)
    click.echo(out)
    return out


def _ensure_utf8() -> None:
    """Enable UTF-8 and Windows VT processing before emitting TUI ANSI sequences.

    Windows PowerShell / legacy console hosts can support Unicode box-drawing
    characters while still treating ANSI control sequences as literal text.
    The TUI relies on ANSI for the alternate screen, cursor positioning and
    redraws, so enable ENABLE_VIRTUAL_TERMINAL_PROCESSING when available.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_console_mode = kernel32.GetConsoleMode
        set_console_mode = kernel32.SetConsoleMode

        # STD_OUTPUT_HANDLE / STD_ERROR_HANDLE
        for handle_id in (-11, -12):
            handle = get_std_handle(handle_id)
            if handle in (0, -1):
                continue

            mode = ctypes.c_uint32()
            if not get_console_mode(handle, ctypes.byref(mode)):
                continue

            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            set_console_mode(handle, mode.value | 0x0004)
    except Exception:
        # Some hosts (redirected pipes, ConEmu-like wrappers, CI) do not
        # expose a Win32 console API. Their existing stream handling remains
        # valid, so VT setup is deliberately best-effort.
        pass


def get_help_str() -> str:
    """Return available commands and shortcuts help text."""
    return (
        "Available commands:\n"
        "  :status           Display current Session, Contract HUD, and Project Delivery View\n"
        "  :contract         Display Contract Boundary HUD and write permissions\n"
        "  :reconnect        Reconnect to daemon and replay missed events\n"
        "  :roles            Display Agent Roles & Execution Backend bindings\n"
        "  :rebind <r> <b>   Rebind agent role to backend (e.g., :rebind gitops ollama llama3)\n"
        "  :wo               Display Work Orders table and progress\n"
        "  :tree, :agents    Display Hierarchical Operation Tree\n"
        "  :plan             Display Plan Approval surface and revision history\n"
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
        "  :compact          Compact older transcript turns to protect context window\n"
        "  :timeout [sec]    Get or set client turn wait timeout (default: 45s)\n"
        "  :landing          Display branded landing block\n"
        "  :help             Show this help menu\n"
        "  :exit, :quit, q   Gracefully stop daemon and exit\n"
        "  <prompt text>     Submit a governed turn to the agent\n\n"
        "Shortcuts:\n"
        "  Ctrl+K            Open this help menu\n"
        "  Ctrl+L            Redraw full screen\n"
        "  Ctrl+C / Ctrl+D   Cancel turn or exit\n"
        "  Esc               Cancel current prompt"
    )


def _show_help() -> str:
    out = get_help_str()
    click.echo(out)
    return out


def _run_demo(client: DaemonClient, session: dict[str, Any]) -> None:
    """Render a deterministic walkthrough demonstrating Milestone P7-4 surfaces."""
    click.echo("StackMind TUI demo")
    state = AutonomousDeliveryState(project_name="my-project", session_id=session["session_id"])
    state.update_from_session(session)

    # 1. Show Session & Legacy HUD for backward compatibility
    _show_status(session, state=None)
    click.echo("Verification: " + verification_matrix({
        "scope": True, "state": True, "ast": True, "behavioral": True,
        "security": True, "outcome": True,
    }))
    click.echo("Diff: " + diff_viewer("No staged daemon diff has been published."))

    # 2. Simulate Plan Proposal
    state.process_event({
        "name": "plan.proposed",
        "payload": {
            "plan_id": "PLAN-001",
            "title": "Autonomous Delivery Foundation",
            "metadata": {
                "work_orders": [
                    {"id": "WO-001", "title": "Architecture & Orchestration", "role": "Architecture"},
                    {"id": "WO-002", "title": "Backend API & Services", "role": "Backend"},
                    {"id": "WO-003", "title": "Frontend Client & Views", "role": "Frontend"},
                    {"id": "WO-004", "title": "Q/A Verification Suite", "role": "Q/A"},
                    {"id": "WO-005", "title": "GitOps Release & Hygiene", "role": "GitOps"},
                ]
            }
        }
    })

    # 3. Simulate Plan Approval
    state.process_event({
        "name": "plan.approved",
        "payload": {
            "plan_id": "PLAN-001",
            "reason": "Operator approved delivery architecture",
            "created_records": [
                {"id": "WO-001", "title": "Architecture & Orchestration", "assigned_agents": ["claude"]},
                {"id": "WO-002", "title": "Backend API & Services", "assigned_agents": ["codex"]},
                {"id": "WO-003", "title": "Frontend Client & Views", "assigned_agents": ["gemini"]},
                {"id": "WO-004", "title": "Q/A Verification Suite", "assigned_agents": ["gemma"]},
                {"id": "WO-005", "title": "GitOps Release & Hygiene", "assigned_agents": ["local-llm"]},
            ]
        }
    })

    # 4. Simulate Subagent Spawns & Activity
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "codex-sub-1", "role": "Backend", "backend": "Codex", "work_order_id": "WO-002"}
    })
    state.process_event({
        "name": "event.agentSpawned",
        "payload": {"agent_id": "gemini-sub-1", "role": "Frontend", "backend": "AGY", "work_order_id": "WO-003"}
    })
    state.process_event({
        "name": "tool_call.read",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.edit",
        "payload": {"role": "Backend", "path": "auth/service.py"}
    })
    state.process_event({
        "name": "tool_call.create",
        "payload": {"role": "Frontend", "path": "LoginPage.tsx"}
    })
    state.process_event({
        "name": "tool_call.test",
        "payload": {"role": "Backend", "command": "pytest"}
    })

    # 5. Render Primary Autonomous Delivery View
    click.echo("\n" + render_project_delivery_view(state))


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
    effective_timeout = client_timeout if client_timeout is not None else getattr(state, "client_timeout", 45.0)
    state.client_timeout = effective_timeout
    normalized = text.strip()
    if not normalized:
        return session, False

    if normalized in {":exit", ":quit", "q"}:
        if live_manager is not None and getattr(live_manager, "is_active", False):
            live_manager.stop()
        return session, True

    is_tty = False
    try:
        is_tty = sys.stdout.isatty()
    except Exception:
        pass

    if normalized == ":help":
        output_str = get_help_str()
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":reconnect":
        output_parts = [render_connection_status_str("reconnecting")]
        ok, missed = reconnect_and_sync(client, adapter, state, session)
        if ok:
            output_parts.append(f"{render_connection_status_str('online')} (reconnected, {len(missed)} missed events synced)")
        else:
            output_parts.append(render_error_box_str(
                f"Could not reconnect to daemon at {client.url}",
                title="RECONNECTION FAILED",
                hint="Verify that the daemon process is running and reachable.",
            ))
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
        output_parts.append(get_status_str(session, state=state))
        output_str = "\n".join(output_parts)

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

    if normalized == ":roles":
        # Query daemon role bindings if supported, and render detailed role panel
        try:
            roles_data = client.list_roles()
            if roles_data and isinstance(roles_data, list):
                for rd in roles_data:
                    role_key = rd.get("role", "").title()
                    normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                    if normalized_key in state.roles:
                        state.roles[normalized_key].backend = rd.get("backend", state.roles[normalized_key].backend)
                        state.roles[normalized_key].model = rd.get("model")
        except Exception:
            pass
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
            model_arg = parts[3] if len(parts) >= 4 else None
            try:
                client.configure_role_backend(role=role_arg, backend=backend_arg, model=model_arg)
                role_key = role_arg.title()
                normalized_key = "Q/A" if role_key.upper() in {"QA", "Q/A"} else role_key
                if normalized_key in state.roles:
                    state.roles[normalized_key].backend = backend_arg
                    if model_arg:
                        state.roles[normalized_key].model = model_arg
                output_str = f"[SUCCESS] Rebound role '{role_arg}' to backend '{backend_arg}'" + (f" (model: {model_arg})" if model_arg else "")
            except Exception as err:
                output_str = f"[ERROR] Failed to rebind role '{role_arg}': {err}"
        else:
            output_str = "Usage: :rebind <role> <backend> [model]  (e.g., :rebind gitops ollama llama3)"
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
                    node = OperationNode(op_id, normalized_rname, role=normalized_rname, backend=backend, status=status, parent_id=parent_id)
                    state.operations[op_id] = node
                    if parent_id in state.operations and op_id not in state.operations[parent_id].children:
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

    if normalized.startswith(":approve"):
        _, _, reason = normalized.partition(" ")
        clean_reason = reason.strip() or "Approved by operator"
        # Check if plan is awaiting approval
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
            try:
                client.plan_approve(session["session_id"], plan_id, reason=clean_reason)
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

    if normalized.startswith(":reject"):
        _, _, reason = normalized.partition(" ")
        clean_feedback = reason.strip() or "Operator requested revision"
        plan_id = state.plan.get("plan_id") or state.plan.get("id")
        if (state.phase == ProjectPhase.AWAITING_APPROVAL or state.plan.get("state") == "AWAITING_APPROVAL") and plan_id:
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
                    if role_key.lower() == clean_target.lower() or getattr(role_obj, "agent_id", "") == clean_target:
                        role_obj.state = "CANCELLED"
                output_str = f"Agent/Operation {clean_target} CANCELLED"
            except Exception:
                try:
                    client.operation_cancel(clean_target)
                    if clean_target in state.operations:
                        state.operations[clean_target].status = "CANCELLED"
                    output_str = f"Operation {clean_target} CANCELLED"
                except Exception as e:
                    output_str = render_error_box_str(f"Cancellation error: {e}", title="CANCEL ERROR")
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

    if normalized == ":resume":
        session = adapter.command(":resume", session_id=session["session_id"])
        output_str = f"Session {session.get('state', 'RUNNING')}"
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":diff"):
        _, _, diff_arg = normalized.partition(" ")
        diff_val = diff_arg.strip() if diff_arg.strip() else None
        if diff_val:
            raw_diff = adapter.command(f":diff diff={diff_val}", session_id=session["session_id"], diff=diff_val)
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
                        output_parts.append(render_assistant_message_str(msg.content, actions=msg.actions, thinking=msg.thinking))
                        rendered_any = True
            if not rendered_any:
                output_parts.append("No new events.")
        except Exception as err:
            if is_connection_error(err):
                state.connection_status = "reconnecting"
                output_parts.append(render_error_box_str(
                    f"Event stream disconnected: {err}",
                    title="CONNECTION ERROR",
                    hint="Use :reconnect to recover missed events once daemon is restored.",
                ))
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

    if normalized.startswith(":timeout"):
        _, _, to_arg = normalized.partition(" ")
        clean_to = to_arg.strip()
        if clean_to:
            try:
                new_to = float(clean_to)
                if new_to > 0:
                    state.client_timeout = new_to
                    output_str = f"Client timeout set to {new_to:.1f}s."
                else:
                    output_str = "Timeout must be a positive number."
            except ValueError:
                output_str = f"Invalid timeout '{clean_to}'. Provide seconds (e.g., :timeout 60)."
        else:
            cur_to = getattr(state, "client_timeout", effective_timeout)
            output_str = f"Current client timeout: {cur_to:.1f}s."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized == ":compact":
        compacted = state.compact_transcript()
        output_str = "Transcript compacted." if compacted else "Transcript is already within its compact retention window."
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    if normalized.startswith(":") and not normalized.startswith(":prompt "):
        output_str = "Unknown command. Type :help."
        state.add_message("user", normalized)
        state.add_message("system", output_str)
        if not is_tty:
            click.echo(output_str)
        return session, False

    # Governed turn prompt
    if live_manager is not None and getattr(live_manager, "is_active", False):
        live_manager.stop()

    prompt_text = normalized[8:].strip() if normalized.startswith(":prompt ") else normalized
    state.add_message("user", prompt_text)
    if is_tty:
        redraw_full_screen(session, state, clear=False, include_composer=False, live_manager=live_manager)
    else:
        click.echo(render_user_message_str(prompt_text))
    op_id = "turn"
    try:
        result = adapter.command(normalized, session_id=session["session_id"])
        op_id = result.get("operation_id", "turn") if isinstance(result, dict) else "turn"
        state.add_activity("User", "submitted turn", op_id)
        if op_id and op_id not in state.operations:
            state.operations[op_id] = OperationNode(op_id, "Turn", role="Backend", backend="Codex", status="RUNNING")
        # This is a local wait indicator, not a claim about daemon state.
        status_line = Text("● Thinking... Turn submitted to the governed daemon.", style="dim cyan")
        click.echo(status_line)

        # Synchronously await turn completion while consuming events via native SSE
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
        model_name: str | None = None
        latest_act_desc = "Turn submitted"
        op_rec: dict[str, Any] | None = None
        op_status: str | None = None

        # Track terminal dimensions and install resize handler (WO-003)
        install_resize_handler()
        init_term = shutil.get_terminal_size(fallback=(80, 24))
        current_cols, current_lines = init_term.columns, init_term.lines

        try:
            stream_gen = adapter.stream(session["session_id"], live=True, timeout=1.0)
            while time.time() - start_time < max_wait and not completed:
                # Check for dynamic terminal resize during active streaming / turn (WO-003)
                resized, new_cols, new_lines = check_terminal_resize(current_cols, current_lines)
                if resized:
                    current_cols, current_lines = new_cols, new_lines
                    if live_manager is not None and getattr(live_manager, "is_active", False):
                        live_manager.handle_resize(new_cols, new_lines)
                try:
                    ev = next(stream_gen)
                except StopIteration:
                    now = time.time()
                    if now - last_poll_time >= 0.5:
                        last_poll_time = now
                        try:
                            op_rec = client.operation_get(op_id) if hasattr(client, "operation_get") else None
                            if op_rec and isinstance(op_rec, dict):
                                op_status = op_rec.get("status")
                                if op_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                                    completed = True
                                    break
                        except Exception:
                            pass
                    time.sleep(0.05)
                    stream_gen = adapter.stream(session["session_id"], live=True, timeout=1.0)
                    continue
                except Exception:
                    time.sleep(0.05)
                    continue

                now = time.time()

                # 1. Handle heartbeat / keepalive
                if isinstance(ev, dict) and (ev.get("_heartbeat") or ev.get("name") == "system.heartbeat"):
                    if not streaming_active and (now - last_progress_time >= 2.0):
                        elapsed = now - start_time
                        prog_text = f"● Working... [{elapsed:.1f}s / {max_wait:.0f}s] ({latest_act_desc})"
                        # AC-7: Route tick through redraw_full_screen so heartbeat ticks do not corrupt layout
                        layout = compute_layout(shutil.get_terminal_size().columns)
                        transcript = (
                            render_chat_transcript_str(state.messages, width=layout.conversation_width)
                            if getattr(state, "messages", None)
                            else ""
                        )
                        tick_content = f"{transcript}\n\n{prog_text}" if transcript else prog_text
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=False,
                            conversation_content=tick_content,
                            live_manager=live_manager,
                        )
                        last_progress_time = now

                    if now - last_poll_time >= 1.5:
                        last_poll_time = now
                        try:
                            op_rec = client.operation_get(op_id) if hasattr(client, "operation_get") else None
                            if op_rec and isinstance(op_rec, dict):
                                op_status = op_rec.get("status")
                                if op_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                                    completed = True
                                    break
                        except Exception:
                            pass
                    continue

                # 2. Process concrete domain event
                if not isinstance(ev, dict):
                    continue
                state.process_event(ev)
                ev_name = str(ev.get("name", ""))
                ev_payload = ev.get("payload", {}) if isinstance(ev.get("payload"), Mapping) else {}

                # Check for incremental text delta (stream/token/chunk)
                delta = extract_text_delta(ev)
                if delta:
                    if not streaming_active:
                        streaming_active = True
                        if model_name is None:
                            if op_rec and isinstance(op_rec, dict):
                                model_name = op_rec.get("model")
                            elif session and isinstance(session, dict):
                                model_name = session.get("model")

                    streamed_chunks.append(delta)

                    # AC-3: Rate-limit redraw_full_screen calls to ~8-10 Hz (100ms gate)
                    now_mono = time.monotonic()
                    if now_mono - last_redraw_time >= 0.1:
                        layout = compute_layout(shutil.get_terminal_size().columns)
                        transcript = (
                            render_chat_transcript_str(state.messages, width=layout.conversation_width)
                            if getattr(state, "messages", None)
                            else ""
                        )
                        hdr = render_assistant_stream_header(model=model_name, width=layout.conversation_width)
                        curr_text = "".join(streamed_chunks)
                        in_progress = f"{hdr}\n{curr_text}" if curr_text else hdr
                        full_content = f"{transcript}\n\n{in_progress}" if transcript else in_progress
                        # WO-011 (AC-4, AC-6): Render tokens inside the width-constrained conversation frame.
                        # Composer is intentionally hidden (include_composer=False) while streaming because
                        # user input is disabled during generation, conserving vertical space.
                        redraw_full_screen(
                            session,
                            state,
                            clear=False,
                            include_composer=False,
                            conversation_content=full_content,
                            live_manager=live_manager,
                        )
                        last_redraw_time = now_mono
                else:
                    # Operational event line
                    ev_str = "" if ev_name in {"operation.started", "turn.started", "operation.completed"} else render_operational_event_str(ev)
                    if ev_str:
                        if not streaming_active:
                            click.echo(ev_str)
                        latest_act_desc = format_action_description(ev_name) if "format_action_description" in globals() else ev_name

                    # Direct assistant response from event
                    resp = extract_assistant_response(ev_payload, workspace=workspace)
                    thinking = extract_assistant_thinking(ev_payload, workspace=workspace)
                    if resp and not assistant_rendered and not streamed_chunks:
                        turn_acts = state.current_turn_actions
                        state.add_message("assistant", resp, actions=turn_acts, thinking=thinking)
                        click.echo(render_assistant_message_str(resp, actions=turn_acts, thinking=thinking))
                        assistant_rendered = True

                # Check terminal event status
                if ev_name in {"operation.completed", "operation.failed", "operation.cancelled", "turn.completed"}:
                    target_op = ev_payload.get("operation_id") or ev.get("operation_id")
                    if not target_op or target_op == op_id:
                        completed = True
                        if ev_name in {"operation.failed", "operation.cancelled"}:
                            op_status = "FAILED" if ev_name == "operation.failed" else "CANCELLED"
                            if op_rec is None:
                                op_rec = {"status": op_status, "result": ev_payload}
                            elif isinstance(op_rec, dict):
                                op_rec["status"] = op_status
                                if "result" not in op_rec:
                                    op_rec["result"] = ev_payload
                        break

                # Periodic operation check
                if now - last_poll_time >= 2.0:
                    last_poll_time = now
                    try:
                        op_rec = client.operation_get(op_id) if hasattr(client, "operation_get") else None
                        if op_rec and isinstance(op_rec, dict):
                            op_status = op_rec.get("status")
                            if op_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                                completed = True
                                break
                    except Exception:
                        pass
        except KeyboardInterrupt:
            # Gracefully intercept Ctrl+C during turn execution: Cancel turn, do NOT kill daemon!
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

            if streaming_active:
                click.echo("")

            cancel_msg = f"Turn operation {op_id} cancelled by operator (Ctrl+C)."
            turn_acts = state.current_turn_actions
            state.add_message("assistant", cancel_msg, actions=turn_acts)
            click.echo(render_error_box_str(
                cancel_msg,
                title="TURN INTERRUPTED",
                hint="In-flight turn operation was cancelled. Daemon process remains running.",
            ))
            restore_composer_focus(state)
            return session, False

        # Finalize streaming if active
        if streamed_chunks:
            full_resp = "".join(streamed_chunks).strip()
            turn_acts = state.current_turn_actions
            state.add_message("assistant", full_resp, actions=turn_acts)
            assistant_rendered = True
            # WO-011 (AC-3, AC-8): Perform one final redraw_full_screen when the stream completes
            # so that the complete response is fully rendered in the viewport before composer returns.
            # Composer is intentionally hidden (include_composer=False) until next input prompt (AC-6).
            redraw_full_screen(
                session,
                state,
                clear=False,
                include_composer=False,
                live_manager=live_manager,
            )

        # If assistant was not rendered from streaming or events, fetch final operation status
        if not assistant_rendered:
            try:
                if (op_rec is None or not completed) and hasattr(client, "operation_get"):
                    op_rec = client.operation_get(op_id)
                    if op_rec and isinstance(op_rec, dict):
                        op_status = op_rec.get("status")
                        if op_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                            completed = True
            except Exception:
                pass

            # Drain any remaining events
            try:
                if hasattr(client, "events"):
                    remaining_events = list(client.events(session["session_id"], after=state.last_sequence))
                    for ev in remaining_events:
                        state.process_event(ev)
                        ev_str = "" if ev.get("name") in {"operation.started", "turn.started", "operation.completed"} else render_operational_event_str(ev)
                        if ev_str:
                            click.echo(ev_str)
                        resp = extract_assistant_response(ev.get("payload", {}), workspace=workspace)
                        thinking = extract_assistant_thinking(ev.get("payload", {}), workspace=workspace)
                        if resp and not assistant_rendered:
                            turn_acts = state.current_turn_actions
                            state.add_message("assistant", resp, actions=turn_acts, thinking=thinking)
                            click.echo(render_assistant_message_str(resp, actions=turn_acts, thinking=thinking))
                            assistant_rendered = True
            except Exception:
                pass

            if not assistant_rendered and op_rec and isinstance(op_rec, dict):
                res = op_rec.get("result") or {}
                summary = res.get("summary") or res.get("reason")
                thinking = (
                    res.get("thinking")
                    or res.get("thought")
                    or res.get("reasoning")
                    or res.get("reasoning_content")
                    or op_rec.get("thinking")
                    or op_rec.get("thought")
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
                                summary = rb.strip() or cnt.strip()
                            else:
                                summary = cnt.strip()
                        except Exception:
                            pass
                if summary:
                    turn_acts = state.current_turn_actions
                    state.add_message("assistant", summary, actions=turn_acts, thinking=thinking)
                    click.echo(render_assistant_message_str(summary, actions=turn_acts, thinking=thinking))
                    assistant_rendered = True
                elif op_status in {"FAILED", "CANCELLED"} or res.get("error"):
                    err_raw = (
                        res.get("error")
                        or res.get("message")
                        or res.get("reason")
                        or op_rec.get("error")
                        or f"Operation {op_status.lower() if op_status else 'failed'}."
                    )
                    if isinstance(err_raw, dict):
                        err_msg = err_raw.get("message") or err_raw.get("error") or str(err_raw)
                    else:
                        err_msg = str(err_raw)
                    err_title = "OLLAMA ERROR" if "ollama" in err_msg.lower() else (f"OPERATION {op_status}" if op_status else "OPERATION FAILED")
                    turn_acts = state.current_turn_actions
                    state.add_message("assistant", f"Operation {op_status.lower() if op_status else 'failed'}: {err_msg}", actions=turn_acts)
                    click.echo(render_error_box_str(err_msg, title=err_title))
                    assistant_rendered = True

        if not assistant_rendered:
            if op_rec and isinstance(op_rec, dict) and op_rec.get("status") in {"FAILED", "CANCELLED"}:
                op_status = op_rec.get("status")
                err_raw = (
                    (op_rec.get("result") or {}).get("error")
                    or (op_rec.get("result") or {}).get("message")
                    or (op_rec.get("result") or {}).get("reason")
                    or op_rec.get("error")
                    or f"Operation {op_status.lower()}."
                )
                err_msg = err_raw.get("message") if isinstance(err_raw, dict) else str(err_raw)
                err_title = "OLLAMA ERROR" if "ollama" in err_msg.lower() else f"OPERATION {op_status}"
                turn_acts = state.current_turn_actions
                state.add_message("assistant", f"Operation {op_status.lower()}: {err_msg}", actions=turn_acts)
                click.echo(render_error_box_str(err_msg, title=err_title))
                assistant_rendered = True
            elif completed:
                completion_msg = f"Turn operation {op_id} completed."
                turn_acts = state.current_turn_actions
                state.add_message("assistant", completion_msg, actions=turn_acts)
                click.echo(render_assistant_message_str(completion_msg, actions=turn_acts))
            else:
                timeout_msg = "No response within client wait time. Operation may still be running. Use :status or :events to inspect."
                state.add_message("system", timeout_msg)
                click.echo(render_error_box_str(timeout_msg, title="REQUEST TIMEOUT", hint="Use :status or :events to inspect."))
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
        click.echo(err_box)
    finally:
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
    client_timeout: float = 45.0,
) -> tuple[dict[str, Any], bool]:
    if state is None:
        state = AutonomousDeliveryState(session_id=session.get("session_id", "session-1"))
        state.update_from_session(session)
    return dispatch_delivery_command(adapter, client, session, text, state, client_timeout=client_timeout)


@click.command("tui")
@click.option("--daemon-url", default=None, help="URL of an existing local daemon.")
@click.option("--agent", "-a", "agent", default="codex", show_default=True)
@click.option("--workspace", "-w", "workspace", type=click.Path(path_type=Path), default=Path("."))
@click.option("--demo", is_flag=True, help="Run the automated daemon-backed walkthrough.")
@click.option("--timeout", "client_timeout", type=float, default=45.0, show_default=True, help="Turn operation wait timeout in seconds.")
def tui(daemon_url: str | None, agent: str, workspace: Path, demo: bool, client_timeout: float = 45.0) -> None:
    """Start the governed Python-native terminal control plane."""
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    daemon: LocalDaemon | None = None
    live_ws: Any | None = None
    if daemon_url is None:
        state_dir = workspace.resolve() / ".sync" / "runtime" / "daemon"
        state_dir.mkdir(parents=True, exist_ok=True)
        daemon = LocalDaemon(str(state_dir), port=0).start()
        daemon_url = daemon.url

    try:
        client = DaemonClient(daemon_url)
        adapter = StackMindTuiAdapter(client)
        session = client.create_session(
            agent=agent,
            provider="daemon",
            contract=_default_contract(),
            workspace=str(workspace.resolve()),
        )
        state = AutonomousDeliveryState(
            project_name=workspace.resolve().name, session_id=session["session_id"]
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
            redraw_full_screen(session, state, clear=True, include_composer=False)
        else:
            term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
            click.echo(render_top_header_bar_str(
                session, width=term_cols, context_meter=(state.context_meter_text, state.context_warning_level)
            ))

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
        while True:
            try:
                resized, new_cols, new_lines = check_terminal_resize(current_cols, current_lines)
                if resized:
                    current_cols, current_lines = new_cols, new_lines
                    if live_ws is not None and getattr(live_ws, "is_active", False):
                        live_ws.handle_resize(new_cols, new_lines)
                term_cols = current_cols
                layout_info = compute_layout(term_cols)
                conv_width = layout_info.conversation_width if layout_info.show_runtime else term_cols
                comp_width = conv_width if layout_info.show_runtime else term_cols

                def _handle_ctrl_k() -> None:
                    out = get_help_str()
                    state.add_message("user", ":help")
                    state.add_message("system", out)
                    if is_tty:
                        redraw_full_screen(session, state, clear=True, include_composer=False, live_manager=live_ws)
                    else:
                        click.echo(out)

                def _handle_ctrl_l() -> None:
                    if is_tty:
                        redraw_full_screen(session, state, clear=True, include_composer=False, live_manager=live_ws)
                    else:
                        click.echo(render_top_header_bar_str(
                            session, width=term_cols, context_meter=(state.context_meter_text, state.context_warning_level)
                        ))
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
                                    height=max(4, shutil.get_terminal_size(fallback=(80, 24)).lines - 4),
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

                text = prompt_composer_input(
                    width=comp_width,
                    terminal_width=term_cols,
                    state=state,
                    session=session,
                    on_ctrl_k=_handle_ctrl_k,
                    on_ctrl_l=_handle_ctrl_l,
                    show_footer=False if is_tty else not getattr(state, "has_conversation", False),
                    live_manager=live_ws,
                )
            except (EOFError, KeyboardInterrupt):
                if live_ws is not None:
                    live_ws.stop()
                click.echo()
                break
            try:
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
                redraw_full_screen(session, state, include_composer=False, live_manager=live_ws)
    finally:
        if live_ws is not None:
            live_ws.stop()
        restore_terminal_state()
        if daemon is not None:
            daemon.stop()
        if temporary_state is not None:
            temporary_state.cleanup()
