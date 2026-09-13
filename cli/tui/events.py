"""Operational Event Stream & Inline Tool Activity for StackMind TUI (WO-031 / Phase 3).

Ingests daemon events and renders operational state transitions, inline tool blocks,
and turn completion responses into the chat-first conversation flow.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import AutonomousDeliveryState


class ToolStatus:
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


STATUS_MARKERS: dict[str, tuple[str, str, str]] = {
    ToolStatus.RUNNING: ("⟳", "bold yellow", "RUNNING"),
    ToolStatus.COMPLETED: ("✓", "bold green", "COMPLETED"),
    ToolStatus.FAILED: ("✗", "bold red", "FAILED"),
    ToolStatus.BLOCKED: ("⊘", "bold yellow", "BLOCKED"),
    ToolStatus.CANCELLED: ("⊘", "dim white", "CANCELLED"),
}


def normalize_status(raw_status: str | None) -> str:
    """Normalize raw event status strings into standard ToolStatus."""
    if not raw_status:
        return ToolStatus.RUNNING
    val = str(raw_status).upper()
    if val in {"SUCCESS", "COMPLETED", "DONE", "PASSED"}:
        return ToolStatus.COMPLETED
    if val in {"FAILURE", "FAILED", "ERROR"}:
        return ToolStatus.FAILED
    if val in {"BLOCKED", "DENIED", "APPROVAL_REQUIRED"}:
        return ToolStatus.BLOCKED
    if val in {"CANCELLED", "CANCELED"}:
        return ToolStatus.CANCELLED
    if val in {"RUNNING", "IN_PROGRESS", "ACTIVE"}:
        return ToolStatus.RUNNING
    return val


def format_tool_name(tool_name: str) -> str:
    """Clean up and format tool name for terminal presentation."""
    cleaned = (
        tool_name.replace("tool_call.", "")
        .replace("tool.", "")
        .replace("event.", "")
        .replace("_", " ")
        .strip()
    )
    lower = cleaned.lower()
    if lower in {"read", "read file", "read_file"}:
        return "Read file"
    if lower in {"edit", "edit file", "edit_file"}:
        return "Edit file"
    if lower in {"write", "write file", "write_file"}:
        return "Write file"
    if lower in {"create", "create file", "create_file"}:
        return "Create file"
    if lower in {"test", "run test", "run tests"}:
        return "Run tests"
    if "harness" in lower:
        return "Run harness"
    return cleaned.title()


@dataclass
class ToolActivity:
    """Inline execution record for a tool call."""

    tool_name: str
    target: str = ""
    status: str = ToolStatus.RUNNING
    call_id: str | None = None
    operation_id: str | None = None
    duration_seconds: float | None = None
    error: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    start_time: float | None = None


def render_tool_activity(
    activity: ToolActivity | Mapping[str, Any], inline: bool = False
) -> RenderableType:
    """Render an inline tool execution block using Rich primitives."""
    if isinstance(activity, ToolActivity):
        tool_name = activity.tool_name
        target = activity.target
        status = normalize_status(activity.status)
        duration_seconds = activity.duration_seconds
        error = activity.error
    else:
        tool_name = str(activity.get("tool_name") or activity.get("name") or "tool")
        payload = activity.get("payload", {}) if isinstance(activity.get("payload"), Mapping) else {}
        args = activity.get("arguments") or payload.get("arguments") or payload
        target = str(
            activity.get("target")
            or args.get("path")
            or args.get("file")
            or args.get("command")
            or args.get("target")
            or ""
        )
        status = normalize_status(activity.get("status") or payload.get("status"))
        duration_seconds = activity.get("duration_seconds")
        error = activity.get("error") or payload.get("error")

    display_name = format_tool_name(tool_name)
    glyph, color, _ = STATUS_MARKERS.get(status, ("●", "white", status))

    if inline:
        line = Text("⚒ ", style="bold cyan")
        line.append(display_name, style="bold white")
        if target:
            line.append(f" {target}", style="white")
        if duration_seconds is not None:
            line.append(f" {duration_seconds:.1f}s", style="dim")
        line.append(" ")
        line.append(glyph, style=color)
        if status in {ToolStatus.RUNNING, ToolStatus.BLOCKED, ToolStatus.FAILED, ToolStatus.CANCELLED}:
            line.append(f" {status}", style=color)
        if error:
            line.append(f" ({error})", style="bold red")
        return line

    # Render as clean bordered rounded panel
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(justify="right", ratio=1)

    left = Text("⚯  ", style="bold #38bdf8")
    left.append(display_name, style="bold white")
    if target:
        left.append(f"   {target}", style="dim white")

    right = Text()
    if duration_seconds is not None:
        right.append(f"{duration_seconds:.1f}s  ", style="dim")
    right.append(glyph, style=color)
    if status in {ToolStatus.RUNNING, ToolStatus.BLOCKED, ToolStatus.FAILED, ToolStatus.CANCELLED}:
        right.append(f" {status}", style=color)

    grid.add_row(left, right)

    if error:
        err_msg = str(error)
        err_text = Text(f"  Error: {err_msg}", style="red")
        body: RenderableType = Group(grid, err_text)
    else:
        body = grid

    return Panel(
        body,
        box=box.ROUNDED,
        border_style="#334155",
        padding=(0, 1),
    )


def render_tool_activity_str(
    activity: ToolActivity | Mapping[str, Any], width: int = 80, inline: bool = False
) -> str:
    """Render tool activity as a plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_tool_activity(activity, inline=inline))
    return console.export_text().rstrip()


def render_tool_activity_line(activity: ToolActivity | Mapping[str, Any]) -> RenderableType:
    """Render tool activity as a compact single line."""
    return render_tool_activity(activity, inline=True)


def render_tool_activity_line_str(
    activity: ToolActivity | Mapping[str, Any], width: int = 80
) -> str:
    """Render tool activity line as a plain string."""
    return render_tool_activity_str(activity, width=width, inline=True)


def render_operational_event(event: Mapping[str, Any]) -> RenderableType | None:
    """Render a daemon operational event cleanly."""
    name = str(event.get("name", ""))
    payload = event.get("payload", {}) if isinstance(event.get("payload"), Mapping) else {}

    # Filter out low-level internal kernel diagnostic events from chat display
    if name in {
        "session.started",
        "attempt.started",
        "contract.loaded",
        "operation.requested",
        "operation.authorized",
    }:
        return None

    if name == "event.toolCall":
        args = payload.get("arguments", {})
        target = str(args.get("path") or args.get("file") or args.get("command") or "")
        activity = ToolActivity(
            tool_name=payload.get("tool_name", "tool"),
            target=target,
            status=ToolStatus.RUNNING,
            call_id=payload.get("call_id"),
            operation_id=payload.get("operation_id"),
            arguments=args,
        )
        return render_tool_activity(activity)

    if name == "event.toolResult":
        activity = ToolActivity(
            tool_name=payload.get("tool_name", "tool"),
            status=normalize_status(payload.get("status")),
            call_id=payload.get("call_id"),
            operation_id=payload.get("operation_id"),
            error=str(payload.get("error")) if payload.get("error") else None,
        )
        return render_tool_activity(activity)

    if name == "operation.started":
        op_id = payload.get("operation_id") or "op"
        text = Text("● Operation ", style="cyan")
        text.append(str(op_id), style="bold cyan")
        text.append(" started", style="white")
        return text

    if name == "turn.started":
        op_id = payload.get("operation_id") or "turn"
        prompt = payload.get("prompt", "")
        text = Text("● Turn started ", style="cyan")
        text.append(f"({op_id})", style="dim")
        if prompt:
            text.append(f": {prompt}", style="white")
        return text

    if name == "operation.completed":
        op_id = payload.get("operation_id") or "op"
        status = payload.get("status", "COMPLETED")
        text = Text("✓ Operation ", style="green")
        text.append(str(op_id), style="bold green")
        text.append(f" completed ({status})", style="white")
        return text

    if name in {"operation.failed", "operation.error"}:
        op_id = payload.get("operation_id") or "op"
        err = payload.get("error", "FAILED")
        text = Text("✗ Operation ", style="red")
        text.append(str(op_id), style="bold red")
        text.append(f" failed ({err})", style="white")
        return text

    if name == "event.agentSpawned":
        role = payload.get("role", "Agent")
        backend = payload.get("backend", "Codex")
        text = Text("● Agent spawned: ", style="cyan")
        text.append(f"{role}", style="bold white")
        text.append(f" ({backend})", style="dim")
        return text

    if name == "plan.proposed":
        plan_id = payload.get("plan_id") or "PLAN"
        text = Text("● Plan proposed: ", style="yellow")
        text.append(str(plan_id), style="bold yellow")
        return text

    if name == "plan.approved":
        plan_id = payload.get("plan_id") or "PLAN"
        text = Text("✓ Plan approved: ", style="green")
        text.append(str(plan_id), style="bold green")
        return text

    if name == "plan.rejected":
        plan_id = payload.get("plan_id") or "PLAN"
        text = Text("✗ Plan rejected: ", style="red")
        text.append(str(plan_id), style="bold red")
        return text

    # Fallback to standard activity line
    from validators.kernel.tui.views import activity_line

    return Text(activity_line(event))


def render_operational_event_str(event: Mapping[str, Any], width: int = 80) -> str:
    """Render a daemon operational event as a string using in-memory capture."""
    rendered = render_operational_event(event)
    if rendered is None:
        return ""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(rendered)
    return console.export_text().rstrip()


def extract_assistant_response(
    payload: Mapping[str, Any] | Any, workspace: Path | None = None
) -> str | None:
    """Extract assistant markdown text from event tool result or outbox report."""
    if not isinstance(payload, Mapping):
        return None

    # 1. Inspect direct result dict
    result = payload.get("result")
    if isinstance(result, Mapping):
        report_path_raw = result.get("report_path")
        if report_path_raw:
            report_path = Path(report_path_raw)
            if not report_path.is_absolute() and workspace:
                report_path = workspace / report_path
            if report_path.exists():
                try:
                    content = report_path.read_text(encoding="utf-8")
                    # Extract ## Report section
                    if "## Report" in content:
                        _, _, body = content.partition("## Report")
                        report_body, _, _ = body.partition("## Meta")
                        clean = report_body.strip()
                        if clean:
                            return clean
                    return content.strip()
                except Exception:
                    pass

        summary = result.get("summary")
        if summary and isinstance(summary, str) and summary.strip():
            return summary.strip()

        response = result.get("response")
        if response and isinstance(response, str) and response.strip():
            return response.strip()

        reason = result.get("reason")
        if reason and isinstance(reason, str) and reason.strip():
            return reason.strip()

    # 2. Inspect direct payload keys
    if isinstance(payload.get("response"), str) and payload["response"].strip():
        return payload["response"].strip()

    if isinstance(payload.get("summary"), str) and payload["summary"].strip():
        return payload["summary"].strip()

    return None


class OperationalEventManager:
    """Stateful tracker for operational event stream and inline tool execution."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace
        self.active_tools: dict[str, ToolActivity] = {}
        self.tool_history: list[ToolActivity] = []
        self._tool_start_times: dict[str, float] = {}

    def process_event(
        self,
        event: Mapping[str, Any],
        state: AutonomousDeliveryState | None = None,
    ) -> tuple[RenderableType | None, str | None]:
        """Ingest event, update state, and return (renderable, assistant_response)."""
        name = str(event.get("name", ""))
        payload = event.get("payload", {}) if isinstance(event.get("payload"), Mapping) else {}

        if state is not None:
            state.process_event(event)

        assistant_response: str | None = None

        if name == "event.toolCall":
            call_id = str(payload.get("call_id") or payload.get("operation_id") or f"call-{len(self.tool_history)}")
            tool_name = str(payload.get("tool_name", "tool"))
            args = payload.get("arguments", {})
            target = str(args.get("path") or args.get("file") or args.get("command") or "")
            start_t = time.time()
            self._tool_start_times[call_id] = start_t
            activity = ToolActivity(
                tool_name=tool_name,
                target=target,
                status=ToolStatus.RUNNING,
                call_id=call_id,
                operation_id=payload.get("operation_id"),
                arguments=args,
                start_time=start_t,
            )
            self.active_tools[call_id] = activity
            return render_tool_activity(activity), None

        if name == "event.toolResult":
            call_id = str(payload.get("call_id") or payload.get("operation_id") or "")
            start_t = self._tool_start_times.pop(call_id, None)
            duration = (time.time() - start_t) if start_t else None

            activity = self.active_tools.pop(call_id, None)
            status = normalize_status(payload.get("status"))
            if activity:
                activity.status = status
                activity.duration_seconds = duration
                if payload.get("error"):
                    activity.error = str(payload["error"])
            else:
                activity = ToolActivity(
                    tool_name=str(payload.get("tool_name", "tool")),
                    status=status,
                    call_id=call_id,
                    duration_seconds=duration,
                    error=str(payload.get("error")) if payload.get("error") else None,
                )
            self.tool_history.append(activity)

            # Check if this toolResult yields an assistant response
            assistant_response = extract_assistant_response(payload, workspace=self.workspace)
            if assistant_response and state is not None:
                state.add_message("assistant", assistant_response)

            return render_tool_activity(activity), assistant_response

        # Check if operation.completed has response
        if name in {"operation.completed", "turn.completed"}:
            assistant_response = extract_assistant_response(payload, workspace=self.workspace)
            if assistant_response and state is not None:
                state.add_message("assistant", assistant_response)

        return render_operational_event(event), assistant_response


def render_connection_status(status: str = "online") -> RenderableType:
    """Render connection status with standard visual indicator."""
    s = str(status).lower()
    if s == "online":
        return Text("● online", style="bold green")
    elif s in {"reconnecting", "reconnect"}:
        return Text("○ reconnecting", style="bold yellow")
    else:
        return Text("✗ offline", style="bold red")


def render_connection_status_str(status: str = "online") -> str:
    """Render connection status indicator as string."""
    s = str(status).lower()
    if s == "online":
        return "● online"
    elif s in {"reconnecting", "reconnect"}:
        return "○ reconnecting"
    else:
        return "✗ offline"


def is_connection_error(err: Exception) -> bool:
    """Check if exception represents a network or transport disconnection."""
    if isinstance(err, (ConnectionError, OSError, TimeoutError)):
        return True
    msg = str(err).lower()
    return any(phrase in msg for phrase in (
        "connection refused",
        "connection reset",
        "connection error",
        "remotedisconnected",
        "broken pipe",
        "timed out",
        "unreachable",
        "failed to establish a new connection",
        "target machine actively refused",
        "network",
    ))


def render_error_box(
    message: str,
    title: str = "ERROR",
    hint: str | None = None,
    width: int | None = None,
) -> RenderableType:
    """Render a concise inline error panel avoiding raw Python stack traces."""
    content = Text()
    clean_msg = str(message)
    if "Traceback (most recent call last):" in clean_msg:
        lines = clean_msg.strip().splitlines()
        clean_msg = lines[-1] if lines else clean_msg

    content.append(clean_msg, style="bold red")
    if hint:
        content.append("\n")
        content.append(hint, style="dim white")

    panel_title = Text(" ! ", style="bold red").append(title, style="bold white").append(" ")
    return Panel(
        content,
        title=panel_title,
        title_align="left",
        box=box.ROUNDED,
        border_style="red dim",
        padding=(0, 1),
    )


def render_error_box_str(
    message: str,
    title: str = "ERROR",
    hint: str | None = None,
    width: int = 80,
) -> str:
    """Render an inline error box as plain formatted string using in-memory capture."""
    buf = io.StringIO()
    console = Console(file=buf, record=True, width=width, force_terminal=False, color_system=None)
    console.print(render_error_box(message, title=title, hint=hint, width=width))
    return console.export_text().rstrip()


def recover_transcript_from_events(
    events: list[Mapping[str, Any]],
    state: AutonomousDeliveryState,
    workspace: Path | None = None,
) -> list[ChatMessage]:
    """Recover visible conversation transcript and delivery state from event.list.

    Reconstructs user prompts and assistant responses while preventing
    duplicate messages or duplicate activity entries.
    """
    from cli.tui.state import ChatMessage

    recovered: list[ChatMessage] = []
    sorted_events = sorted(events, key=lambda ev: ev.get("sequence", 0))

    for ev in sorted_events:
        seq = ev.get("sequence")
        if seq is not None and isinstance(seq, int) and seq in state.seen_sequences:
            continue

        name = str(ev.get("name", ""))
        payload = ev.get("payload", {}) if isinstance(ev.get("payload"), Mapping) else {}

        # Update core delivery state (operations, roles, WOs, verifications)
        state.process_event(ev)

        # 1. Recover user prompt from turn.started or operation.started
        if name in {"turn.started", "operation.started"}:
            prompt = payload.get("prompt") or payload.get("input")
            if prompt and isinstance(prompt, str) and prompt.strip():
                clean_prompt = prompt.strip()
                msg_key = ("user", clean_prompt)
                if msg_key not in state.seen_message_keys:
                    state.seen_message_keys.add(msg_key)
                    msg = state.add_message("user", clean_prompt)
                    recovered.append(msg)

        # 2. Recover assistant response from toolResult, completed, or report
        elif name in {"event.toolResult", "operation.completed", "turn.completed"}:
            resp = extract_assistant_response(payload, workspace=workspace)
            if resp and isinstance(resp, str) and resp.strip():
                clean_resp = resp.strip()
                msg_key = ("assistant", clean_resp)
                if msg_key not in state.seen_message_keys:
                    state.seen_message_keys.add(msg_key)
                    msg = state.add_message("assistant", clean_resp)
                    recovered.append(msg)

    # 3. Check outbox for any unattached harness reports if workspace provided
    if workspace:
        outbox_dir = workspace / ".sync" / "outbox"
        if outbox_dir.exists():
            for rpt in sorted(outbox_dir.glob("**/harness-*.md")):
                try:
                    content = rpt.read_text(encoding="utf-8")
                    if "## Report" in content:
                        _, _, body = content.partition("## Report")
                        report_body, _, _ = body.partition("## Meta")
                        clean = report_body.strip()
                        if clean:
                            msg_key = ("assistant", clean)
                            if msg_key not in state.seen_message_keys:
                                state.seen_message_keys.add(msg_key)
                                msg = state.add_message("assistant", clean)
                                recovered.append(msg)
                except Exception:
                    pass

    return recovered


__all__ = [
    "OperationalEventManager",
    "STATUS_MARKERS",
    "ToolActivity",
    "ToolStatus",
    "extract_assistant_response",
    "format_tool_name",
    "is_connection_error",
    "normalize_status",
    "recover_transcript_from_events",
    "render_connection_status",
    "render_connection_status_str",
    "render_error_box",
    "render_error_box_str",
    "render_operational_event",
    "render_operational_event_str",
    "render_tool_activity",
    "render_tool_activity_line",
    "render_tool_activity_line_str",
    "render_tool_activity_str",
]
