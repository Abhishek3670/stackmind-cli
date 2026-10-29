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


def clean_error_message(err: Any) -> str:
    """Extract a concise human-readable error string avoiding raw dicts or traces, stripping internal LLM prompts."""
    if not err:
        return ""
    if isinstance(err, Mapping):
        for key in ("message", "error", "reason", "summary", "detail"):
            val = err.get(key)
            if val and val != err:
                cleaned = clean_error_message(val)
                if cleaned and cleaned.lower() not in {"failed", "blocked", "error"}:
                    return cleaned
        for val in err.values():
            if isinstance(val, str) and val.strip() and val.strip().lower() not in {"failed", "blocked", "error"}:
                return clean_error_message(val)
        return "Operation failed"

    msg = str(err).strip()
    if msg.startswith("{") and msg.endswith("}") and ("'reason':" in msg or "'error':" in msg or "'message':" in msg):
        try:
            import ast
            parsed = ast.literal_eval(msg)
            if isinstance(parsed, Mapping):
                return clean_error_message(parsed)
        except Exception:
            pass
    if "Traceback (most recent call last):" in msg:
        lines = [line.strip() for line in msg.splitlines() if line.strip()]
        if lines:
            msg = lines[-1]

    # Strip LLM prompt instructions (e.g. CRITICAL: Do NOT retry...)
    import re
    if re.search(r"\bCRITICAL\s*:", msg, flags=re.IGNORECASE):
        msg = re.split(r"\bCRITICAL\s*:", msg, flags=re.IGNORECASE)[0].strip()

    # Normalize runtime policy / scope / contract permission denials
    lower_msg = msg.lower()
    if (
        "denied by runtime policy" in lower_msg
        or "target is explicitly denied" in lower_msg
        or "outside assigned scope" in lower_msg
        or ("permissionerror" in lower_msg and "denied" in lower_msg)
    ):
        return "Permission denied (outside assigned scope)"

    # Strip verbose file lists from FileNotFoundError / ModuleNotFoundError
    for cut_marker in ("Existing workspace files:", "Available workspace files:", "Available files:"):
        if cut_marker in msg:
            msg = msg[: msg.index(cut_marker)].rstrip(" .")
            break

    # Strip after first sentence boundary in *Error messages to avoid leaking implementation details
    if re.match(r"^\w*Error:", msg):
        sent_match = re.search(r"(?<=\.)\s", msg)
        if sent_match:
            msg = msg[: sent_match.start()].rstrip()

    # Hard-cap inline error to 60 chars max
    if len(msg) > 60:
        msg = msg[:57] + "..."

    return msg



def format_tool_name(tool_name: str, role: str = "") -> str:
    """Clean up and format tool name for terminal presentation."""
    cleaned = (
        tool_name.replace("tool_call.", "")
        .replace("tool.", "")
        .replace("event.", "")
        .replace("_", " ")
        .strip()
    )
    lower = cleaned.lower()
    if "graph" in lower:
        return "Graphing"
    if "harness" in lower:
        if role:
            cased = "QA" if role.lower() == "qa" else (role.title() if role.islower() else role)
            return f"Agent ({cased})"
        if "run_once" in tool_name.lower() or "run once" in lower or lower == "harness":
            return "Agent"
        return "Harnessing"
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
    if lower in {"exec command", "exec_command", "run command", "run_command", "bash", "sh", "command", "execute"}:
        return "Execute"
    return cleaned.title()


def format_action_description(tool_name: str, target: str = "", role: str = "") -> str:
    """Format an action description string for disclosure display per §19-§21."""
    cleaned_tool = (
        tool_name.replace("tool_call.", "")
        .replace("tool.", "")
        .replace("event.", "")
        .strip()
    )
    t = str(target).strip()
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    lower = cleaned_tool.lower()
    if "graph" in lower:
        return f"Graphing ({t})" if t else "Graphing"
    if "harness" in lower:
        raw_agent = role or (t if t and t not in ("long_running_task", "None", "none", "null") else "")
        if str(raw_agent).lower() in ("none", "null"):
            raw_agent = ""
        agent_name = str(raw_agent).strip()
        if agent_name:
            cased = "QA" if agent_name.lower() == "qa" else (agent_name.title() if agent_name.islower() else agent_name)
            return f"Agent ({cased})"
        if "run_once" in tool_name.lower() or "run once" in lower or lower == "harness":
            return "Agent"
        return "Harnessing"
    if lower in {"test", "run_tests", "run_test", "run tests"}:
        return f"Test ({t})" if t else "Test"
    if lower in {"read", "read_file", "read file", "view_file"}:
        return f"Read ({t})" if t else "Read"
    if lower in {"edit", "edit_file", "edit file", "patch"}:
        return f"Edit ({t})" if t else "Edit"
    if lower in {"write", "write_file", "write file", "create", "create_file", "create file"}:
        return f"Write ({t})" if t else "Write"
    if lower in {"exec_command", "run_command", "bash", "sh", "command", "execute"}:
        return f"Execute ({t})" if t else "Execute"
    if lower in {"list_dir", "list_directory", "ls"}:
        return f"List ({t})" if t else "List"
    if "verify" in lower:
        return f"Verify ({t})" if t else "Verify"
    if lower in {"trigger_goal", "trigger goal"}:
        g = t if len(t) <= 30 else t[:27] + "..."
        return f"Trigger Goal ({g})" if g else "Trigger Goal"
    if lower in {"delegate_task", "delegate task"}:
        return f"Delegate ({t})" if t else "Delegate"
    base = cleaned_tool.replace("_", " ").title()
    return f"{base} ({t})" if t else base


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
        "reasoning.delta",
        "token.delta",
        "stream.delta",
        "content.delta",
        "message.delta",
        "text.delta",
        "turn.delta",
        "assistant.delta",
        "agent.thinking",
        "turn.thinking",
        "cycle.telemetry",
        "harness.telemetry",
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
        return render_tool_activity_line(activity)

    if name == "event.toolResult":
        activity = ToolActivity(
            tool_name=payload.get("tool_name", "tool"),
            status=normalize_status(payload.get("status")),
            call_id=payload.get("call_id"),
            operation_id=payload.get("operation_id"),
            error=str(payload.get("error")) if payload.get("error") else None,
        )
        return render_tool_activity_line(activity)

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
        err = clean_error_message(payload.get("error", "FAILED"))
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

    from cli.tui.chat import format_contract_payload, strip_internal_reasoning

    def clean_response(value: str) -> str | None:
        if not value:
            return None
        # Never leak raw harness audit reports or internal harness metadata into chat
        if value.strip().startswith("# Harness Report") or "## Meta\n```yaml" in value:
            return None
        clean = strip_internal_reasoning(value)
        if clean:
            clean = format_contract_payload(clean)
        return clean or None

    # 1. Inspect direct result dict
    result = payload.get("result")
    if isinstance(result, Mapping):
        review_notes = result.get("review_notes")
        verdict = result.get("verdict")
        if review_notes and isinstance(review_notes, str) and review_notes.strip():
            clean_rn = review_notes.strip()
            if verdict:
                return clean_response(f"### QA Verdict: {verdict}\n\n{clean_rn}")
            return clean_response(clean_rn)

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
                        if clean and clean != "Completed task via tools" and not clean.startswith("# Harness Report"):
                            return clean_response(clean)
                except Exception:
                    pass

        summary = result.get("summary")
        if summary and isinstance(summary, str) and summary.strip() and summary.strip() != "Completed task via tools":
            return clean_response(summary)

        report_md = result.get("report_markdown")
        if report_md and isinstance(report_md, str) and report_md.strip() and report_md.strip() != "Completed task via tools":
            return clean_response(report_md)

        response = result.get("response")
        if response and isinstance(response, str) and response.strip():
            return clean_response(response)

        reason = result.get("reason") or result.get("error")
        if reason and isinstance(reason, str) and reason.strip():
            st = str(result.get("status", "")).lower()
            if st in {"blocked", "failed", "cancelled"}:
                return clean_response(f"Turn {st}: {reason.strip()}")
            return clean_response(reason)

        message = result.get("message")
        if message and isinstance(message, str) and message.strip():
            return clean_response(message)

        text = result.get("text")
        if text and isinstance(text, str) and text.strip():
            return clean_response(text)

        output = result.get("output")
        if output and isinstance(output, str) and output.strip():
            return clean_response(output)

        mods = result.get("modified_files")
        if isinstance(mods, list) and mods:
            user_mods = [f for f in mods if not str(f).startswith((".sync", ".sync/"))]
            if user_mods:
                files_str = ", ".join(f"`{f}`" for f in user_mods)
                if any("PLAN.md" in str(f) for f in user_mods):
                    return clean_response(
                        f"Created implementation plan in {files_str}. Use `:plan` to review or `:approve` to proceed."
                    )
                return clean_response(f"Completed turn. Modified files: {files_str}.")
            elif any("PLAN.md" in str(f) for f in mods):
                return clean_response(
                    "Created implementation plan in `PLAN.md`. Use `:plan` to review or `:approve` to proceed."
                )
            return clean_response("Completed turn.")

        blockers = result.get("blockers")
        if isinstance(blockers, list) and blockers:
            b_str = "; ".join(str(b) for b in blockers)
            st = str(result.get("status", "blocked")).lower()
            return clean_response(f"Turn {st}: {b_str}")

        if verdict and isinstance(verdict, str) and verdict.strip():
            return clean_response(f"### QA Verdict: {verdict.strip()}")

        st_val = result.get("status")
        if isinstance(st_val, str) and st_val.strip():
            s_low = st_val.lower().strip()
            if s_low in {"completed", "success", "done"}:
                return clean_response("Completed turn via tools.")
            return clean_response(f"Turn {s_low}.")

    # 2. Inspect direct payload keys
    if isinstance(payload.get("response"), str) and payload["response"].strip():
        return clean_response(payload["response"])

    if isinstance(payload.get("summary"), str) and payload["summary"].strip():
        return clean_response(payload["summary"])

    if isinstance(payload.get("message"), str) and payload["message"].strip():
        return clean_response(payload["message"])

    if isinstance(payload.get("text"), str) and payload["text"].strip():
        return clean_response(payload["text"])

    if isinstance(payload.get("output"), str) and payload["output"].strip():
        return clean_response(payload["output"])

    return None


def extract_text_delta(event: Mapping[str, Any] | Any) -> str | None:
    """Extract incremental token or text delta from streaming event."""
    if not isinstance(event, Mapping):
        return None
    name = str(event.get("name", "")).lower()
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    if name in {
        "stream.delta",
        "token.delta",
        "content.delta",
        "message.delta",
        "text.delta",
        "turn.delta",
        "assistant.delta",
    }:
        for key in ("delta", "token", "text", "content", "chunk"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        if isinstance(event.get("delta"), str) and event["delta"]:
            return str(event["delta"])
    # Also check if payload has an explicit delta field
    if "delta" in payload and isinstance(payload["delta"], str) and payload["delta"]:
        return payload["delta"]
    return None


def extract_reasoning_delta(event: Mapping[str, Any] | Any) -> str | None:
    """Extract normalized reasoning/thinking delta from streaming event."""
    if not isinstance(event, Mapping):
        return None
    name = str(event.get("name", "")).lower()
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    if name in {"reasoning.delta", "agent.thinking", "turn.thinking"}:
        for key in ("text", "reasoning", "thinking", "delta"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def extract_tool_output(payload: Mapping[str, Any] | Any) -> str | None:
    """Extract terminal stdout, output, or execution error from toolResult payload."""
    if not isinstance(payload, Mapping):
        return None
    res = payload.get("result")
    if isinstance(res, str) and res.strip():
        return res.strip()
    if isinstance(res, Mapping):
        for k in ("output", "stdout", "result", "response", "message", "text", "stderr"):
            v = res.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        if "error" in res and res["error"]:
            return str(res["error"]).strip()
    for k in ("output", "stdout", "stderr", "error", "response"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def extract_action_target(args: Mapping[str, Any] | None) -> str:
    """Extract primary target path, command, or selector from tool arguments."""
    if not isinstance(args, Mapping):
        return ""
    for k in ("path", "file", "test_path", "test_name", "selector", "command", "target", "directory", "dir_path", "name", "goal", "target_agent", "agent"):
        v = args.get(k)
        if v:
            return str(v)
    return ""


def extract_tool_diff(payload: Mapping[str, Any] | Any, args: Mapping[str, Any] | None = None) -> str | None:
    """Extract unified diff text or synthetic diff summary from edit/write tool call or result."""
    if isinstance(payload, Mapping):
        res = payload.get("result")
        if isinstance(res, str):
            if "@@" in res and ("--- " in res or "+++ " in res or "diff " in res):
                lines = res.splitlines()
                start_idx = 0
                for idx, line in enumerate(lines):
                    if line.startswith("--- ") or line.startswith("@@") or line.startswith("diff "):
                        start_idx = idx
                        break
                return "\n".join(lines[start_idx:]).strip()
            if "--- " in res or "@@" in res or res.startswith("diff "):
                return res.strip()
        if isinstance(res, Mapping):
            for k in ("diff", "patch", "unified_diff"):
                v = res.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            summary = res.get("diffSummary")
            if isinstance(summary, Mapping):
                f = summary.get("file", "file")
                adds = summary.get("additions", 0)
                dels = summary.get("deletions", 0)
                return f"--- a/{f}\n+++ b/{f}\n@@ -1,{dels} +1,{adds} @@\n- ({dels} deletions)\n+ ({adds} additions)"
        for k in ("diff", "patch", "unified_diff"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

    if args and isinstance(args, Mapping):
        for k in ("diff", "patch", "patch_content", "unified_diff"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        old_str = args.get("target_content") or args.get("old_str") or args.get("old_string") or args.get("find")
        new_str = args.get("replacement") or args.get("replacement_content") or args.get("new_str") or args.get("new_string") or args.get("replace")
        target_f = extract_action_target(args) or "file"
        rel_target = target_f.replace("\\", "/").strip().lstrip("/")
        if old_str is not None or new_str is not None:
            lines = [f"--- a/{rel_target}", f"+++ b/{rel_target}"]
            lines.append("@@ -1,6 +1,6 @@")
            if old_str:
                for l in str(old_str).splitlines()[:6]:
                    lines.append(f"- {l}")
            if new_str:
                for l in str(new_str).splitlines()[:6]:
                    lines.append(f"+ {l}")
            return "\n".join(lines)
        content = args.get("content") or args.get("code") or args.get("text")
        if content and isinstance(content, str) and content.strip():
            c_lines = content.splitlines()
            lines = [f"--- a/{rel_target}", f"+++ b/{rel_target}", f"@@ -0,0 +1,{len(c_lines)} @@"]
            for cl in c_lines:
                lines.append(f"+{cl}")
            return "\n".join(lines)
    return None




def extract_assistant_thinking(
    payload: Mapping[str, Any] | Any, workspace: Path | None = None
) -> str | None:
    """Extract genuine provider thinking/reasoning text from event payload or outbox report."""
    if not isinstance(payload, Mapping):
        return None

    from cli.tui.chat import extract_internal_reasoning

    # 1. Direct thinking keys in payload
    for key in ("thinking", "reasoning", "reasoning_content", "thought"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 2. Inspect direct result dict
    result = payload.get("result")
    if isinstance(result, Mapping):
        for key in ("thinking", "reasoning", "reasoning_content", "thought"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        report_path_raw = result.get("report_path")
        if report_path_raw:
            report_path = Path(report_path_raw)
            if not report_path.is_absolute() and workspace:
                report_path = workspace / report_path
            if report_path.exists():
                try:
                    content = report_path.read_text(encoding="utf-8")
                    if "## Thinking" in content:
                        _, _, think_part = content.partition("## Thinking")
                        think_body, _, _ = think_part.partition("## ")
                        clean = think_body.strip()
                        if clean:
                            return clean
                    extracted = extract_internal_reasoning(content)
                    if extracted:
                        return extracted
                except Exception:
                    pass

        # Check inside result string fields for <think>...</think>
        for field in ("response", "summary", "reason"):
            val = result.get(field)
            if isinstance(val, str):
                extracted = extract_internal_reasoning(val)
                if extracted:
                    return extracted

    # 3. Check inside payload string fields for <think>...</think>
    for field in ("response", "summary"):
        val = payload.get(field)
        if isinstance(val, str):
            extracted = extract_internal_reasoning(val)
            if extracted:
                return extracted

    return None


class OperationalEventManager:
    """Stateful tracker for operational event stream and inline tool execution."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace
        self.active_tools: dict[str, ToolActivity] = {}
        self.tool_history: list[ToolActivity] = []
        self._tool_start_times: dict[str, float] = {}
        from cli.tui.state import ActionsGroup
        self.current_actions: ActionsGroup = ActionsGroup(active=True)

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
            args = payload.get("arguments", {}) if isinstance(payload.get("arguments"), Mapping) else {}
            target = extract_action_target(args)
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
            role = str(payload.get("role") or payload.get("agent_id") or payload.get("agent") or "").strip()
            if not role and state is not None:
                op_id = payload.get("operation_id")
                if op_id and hasattr(state, "operations") and op_id in state.operations:
                    role = getattr(state.operations[op_id], "role", "")
                if not role and hasattr(state, "operations"):
                    for op in state.operations.values():
                        if getattr(op, "status", "") == "RUNNING":
                            role = getattr(op, "role", "")
                            break
            desc = format_action_description(tool_name, target, role=role)
            tool_code = None
            tool_diff = None
            if tool_name in {"write_file", "write", "create_file", "create"}:
                tool_code = str(args.get("content") or args.get("code") or "")
                tool_diff = extract_tool_diff(payload, args)
            elif tool_name in {"edit_file", "edit", "patch", "apply_patch"}:
                tool_diff = extract_tool_diff(payload, args)
                if not tool_diff:
                    tool_code = str(args.get("content") or args.get("code") or "")
            self.current_actions.add_or_update(
                call_id,
                desc,
                status=ToolStatus.RUNNING,
                tool_name=tool_name,
                target=target,
                code=tool_code,
                diff=tool_diff,
                role=role,
            )
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
                    activity.error = clean_error_message(payload["error"])
            else:
                activity = ToolActivity(
                    tool_name=str(payload.get("tool_name", "tool")),
                    status=status,
                    call_id=call_id,
                    duration_seconds=duration,
                    error=clean_error_message(payload.get("error")) if payload.get("error") else None,
                )
            self.tool_history.append(activity)
            tool_output = extract_tool_output(payload)
            call_args = getattr(activity, "arguments", None) or payload.get("arguments") or {}
            tool_diff = extract_tool_diff(payload, call_args)
            if not tool_diff and getattr(activity, "code", None):
                target_f = getattr(activity, "target", "") or "file"
                rel_target = target_f.replace("\\", "/").strip().lstrip("/")
                c_lines = str(activity.code).splitlines()
                tool_diff = f"--- a/{rel_target}\n+++ b/{rel_target}\n@@ -0,0 +1,{len(c_lines)} @@\n" + "\n".join(f"+{l}" for l in c_lines)
            self.current_actions.complete_action(
                call_id,
                status=status,
                duration_seconds=duration,
                error=activity.error,
                tool_name=activity.tool_name,
                output=tool_output,
                diff=tool_diff,
            )

            # Check if this toolResult yields an assistant response
            assistant_response = extract_assistant_response(payload, workspace=self.workspace)
            assistant_thinking = extract_assistant_thinking(payload, workspace=self.workspace)
            if assistant_response and state is not None:
                acts = self.current_actions if not self.current_actions.is_empty else None
                from cli.tui.commands import resolve_turn_model_attribution
                ev_model = resolve_turn_model_attribution(state, op_rec=payload, role=payload.get("role"))
                state.add_message("assistant", assistant_response, deduplicate=True, actions=acts, thinking=assistant_thinking, model=ev_model)

            return render_tool_activity(activity), assistant_response

        # Check if operation.completed has response
        if name in {"operation.completed", "turn.completed"}:
            self.current_actions.active = False
            assistant_response = extract_assistant_response(payload, workspace=self.workspace)
            assistant_thinking = extract_assistant_thinking(payload, workspace=self.workspace)
            if assistant_response and state is not None:
                acts = self.current_actions if not self.current_actions.is_empty else None
                from cli.tui.commands import resolve_turn_model_attribution
                ev_model = resolve_turn_model_attribution(state, op_rec=payload, role=payload.get("role"))
                state.add_message("assistant", assistant_response, deduplicate=True, actions=acts, thinking=assistant_thinking, model=ev_model)

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
            role = str(payload.get("role") or "").lower().strip()
            agent_id = str(payload.get("agent_id") or "").lower().strip()
            delegated_from = payload.get("delegated_from") or payload.get("metadata", {}).get("delegated_from")
            work_order_id = payload.get("work_order_id")
            is_subagent_task = bool(
                delegated_from
                or work_order_id
                or (role and role not in ("architecture", "claude", "user"))
                or (agent_id and agent_id not in ("architecture", "claude"))
            )
            if not is_subagent_task and prompt and isinstance(prompt, str) and prompt.strip():
                clean_prompt = prompt.strip()
                msg_key = ("user", clean_prompt)
                if msg_key not in state.seen_message_keys:
                    state.seen_message_keys.add(msg_key)
                    msg = state.add_message("user", clean_prompt)
                    recovered.append(msg)

        # 2. Recover assistant response from toolResult, completed, or report
        elif name in {"event.toolResult", "operation.completed", "turn.completed"}:
            res = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
            rpt_raw = res.get("report_path")
            if rpt_raw:
                try:
                    rp = Path(rpt_raw)
                    if not rp.is_absolute() and workspace:
                        rp = workspace / rp
                    if hasattr(state, "seen_harness_reports"):
                        state.seen_harness_reports.add(str(rp.resolve()))
                except Exception:
                    pass

            resp = extract_assistant_response(payload, workspace=workspace)
            thinking = extract_assistant_thinking(payload, workspace=workspace)
            if resp and isinstance(resp, str) and resp.strip():
                clean_resp = resp.strip()
                msg_key = ("assistant", clean_resp)
                if msg_key not in state.seen_message_keys:
                    state.seen_message_keys.add(msg_key)
                    msg = state.add_message("assistant", clean_resp, thinking=thinking)
                    recovered.append(msg)

    # 3. Check outbox for any unattached harness reports if workspace provided
    if workspace:
        outbox_dir = workspace / ".sync" / "outbox"
        if outbox_dir.exists():
            for rpt in sorted(outbox_dir.glob("**/harness-*.md")):
                rpt_key = str(rpt.resolve())
                if hasattr(state, "seen_harness_reports"):
                    if rpt_key in state.seen_harness_reports:
                        continue
                    state.seen_harness_reports.add(rpt_key)

                try:
                    content = rpt.read_text(encoding="utf-8")
                    thinking = None
                    if "## Thinking" in content:
                        _, _, think_part = content.partition("## Thinking")
                        think_body, _, _ = think_part.partition("## ")
                        thinking = think_body.strip() or None
                    if "## Report" in content:
                        _, _, body = content.partition("## Report")
                        report_body, _, _ = body.partition("## Meta")
                        clean = report_body.strip()
                        if clean and clean != "Completed task via tools" and not clean.startswith("# Harness Report"):
                            msg_key = ("assistant", clean)
                            if msg_key not in state.seen_message_keys:
                                state.seen_message_keys.add(msg_key)
                                msg = state.add_message("assistant", clean, thinking=thinking)
                                recovered.append(msg)
                except Exception:
                    pass

    return recovered


__all__ = [
    "OperationalEventManager",
    "STATUS_MARKERS",
    "ToolActivity",
    "ToolStatus",
    "clean_error_message",
    "extract_assistant_response",
    "extract_assistant_thinking",
    "extract_tool_diff",
    "extract_tool_output",
    "format_action_description",
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
