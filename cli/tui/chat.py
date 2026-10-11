"""Chat-first message and transcript renderers for the StackMind TUI (Phases 2, 4 & 5 per §13–§17, §32, §33)."""

from __future__ import annotations

import datetime
import hashlib
import io
import os
import re
import sys
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from cli.tui.state import ChatMessage


# ─── Transcript Render Cache ─────────────────────────────────────────────────

# Cache for rendered transcript strings keyed by (transcript_hash, width, options)
# Uses string output rather than Rich renderables to avoid console/mutable-state issues
_TRANSCRIPT_CACHE: dict[str, str] = {}
_TRANSCRIPT_CACHE_MAX_SIZE = 500  # Prevent unbounded growth in long sessions
_TRANSCRIPT_CACHE_VERSION = "v1"  # Schema version for cache invalidation on rendering changes


def _compute_transcript_hash(messages: list[ChatMessage]) -> str:
    """Compute a deterministic hash for the complete ordered transcript.

    Includes ALL messages to detect:
    - Earlier-message content changes
    - Last-message content changes
    - Message additions, removals, or reordering
    - Role or metadata changes
    - Full content (no truncation)
    """
    key_parts = [
        _TRANSCRIPT_CACHE_VERSION,
        str(len(messages)),  # Detect additions/removals
    ]
    for msg in messages:
        # Include full content (no truncation) to avoid collisions
        # Use hashlib.md5 for each message to handle arbitrarily long content
        msg_content = getattr(msg, "content", "")
        msg_role = getattr(msg, "role", "")
        msg_actions = str(getattr(msg, "actions", None))
        msg_thinking = getattr(msg, "thinking", None) or ""
        msg_model = getattr(msg, "model", None) or ""

        # Hash individual message to handle long content efficiently
        msg_hash = hashlib.md5(
            "{}|{}|{}|{}|{}".format(
                msg_role, msg_content, msg_actions, msg_thinking, msg_model
            ).encode("utf-8")
        ).hexdigest()
        key_parts.append(msg_hash)

    return hashlib.md5("|".join(key_parts).encode("utf-8")).hexdigest()


def _compute_cache_key(transcript_hash: str, width: int,
                       inline_diffs: bool = True, show_timestamps: bool = False,
                       accent: bool = True) -> str:
    """Compute cache key including all rendering-relevant parameters."""
    return "{}|{}|{}|{}|{}".format(
        transcript_hash, width, inline_diffs, show_timestamps, accent
    )


def _clear_cache_if_needed() -> None:
    """Evict oldest entries if cache exceeds max size (FIFO eviction)."""
    if len(_TRANSCRIPT_CACHE) > _TRANSCRIPT_CACHE_MAX_SIZE:
        # Clear entire cache when over limit (simplest FIFO approximation)
        _TRANSCRIPT_CACHE.clear()


def _get_cached_render(cache_key: str) -> str | None:
    """Get cached render if exists."""
    return _TRANSCRIPT_CACHE.get(cache_key)


def _set_cached_render(cache_key: str, rendered: str) -> None:
    """Store render in cache, evicting if necessary."""
    _clear_cache_if_needed()
    _TRANSCRIPT_CACHE[cache_key] = rendered


# ─── Message Rendering Helpers ──────────────────────────────────────────────


def format_contract_payload(content: str) -> str:
    """Detect and format raw task contract completion JSON into clean assistant text.

    If content is pure contract JSON (e.g. {"status": "completed", "modified_files": ["PLAN.md"]}),
    converts it into a human-readable summary. If conversational commentary exists before the
    contract JSON, the trailing technical JSON block is cleanly stripped.
    """
    if not content or not isinstance(content, str):
        return content

    text = content.strip()

    # Check for markdown code fences wrapping JSON
    unfenced = text
    if unfenced.startswith("```"):
        _, _, rest = unfenced.partition("\n")
        if "```" in rest:
            body, _, _ = rest.rpartition("```")
            unfenced = body.strip()

    def _is_contract_dict(d: Any) -> bool:
        if not isinstance(d, dict):
            return False
        st = d.get("status")
        return isinstance(st, str) and (
            "modified_files" in d or "blockers" in d or "decision" in d
        )

    def _format_dict(d: dict[str, Any]) -> str:
        st = str(d.get("status", "")).lower()
        mods = d.get("modified_files") or []
        blockers = d.get("blockers") or []
        if st in {"completed", "success", "done"}:
            if mods:
                files_str = ", ".join(f"`{f}`" for f in mods)
                if any("PLAN.md" in str(f) for f in mods):
                    return f"Created implementation plan in {files_str}. Use `:plan` to review or `:approve` to proceed."
                return f"Completed turn. Modified files: {files_str}."
            return "Completed turn via tools."
        if st in {"blocked", "failed", "cancelled"}:
            if blockers:
                b_str = "; ".join(str(b) for b in blockers)
                return f"Turn {st}: {b_str}"
            reason = d.get("reason") or d.get("error") or "No details provided"
            return f"Turn {st}: {reason}"
        return text

    if unfenced.startswith("{") and unfenced.endswith("}"):
        try:
            import json
            parsed = json.loads(unfenced)
            if _is_contract_dict(parsed):
                return _format_dict(parsed)
        except Exception:
            pass

    # Check if text ends with a JSON block after conversational text
    match = re.search(r"\n*```(?:json)?\s*(\{[\s\S]*?\})\s*```\s*$", text)
    if not match:
        match = re.search(r"\n*(\{[\s\S]*?\})\s*$", text)
    if match:
        json_candidate = match.group(1).strip()
        try:
            import json
            parsed = json.loads(json_candidate)
            if _is_contract_dict(parsed):
                leading_text = text[:match.start()].strip()
                if leading_text:
                    return leading_text
                return _format_dict(parsed)
        except Exception:
            pass

    return text


def strip_internal_reasoning(content: str) -> str:
    """Remove provider-delimited private reasoning without altering final Markdown."""
    clean = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", content, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<think\b[^>]*>.*$", "", clean, flags=re.IGNORECASE | re.DOTALL).strip()
    return format_contract_payload(clean)


def extract_internal_reasoning(content: str) -> str | None:
    """Extract provider-delimited private reasoning text without <think> tags."""
    if not content or not isinstance(content, str):
        return None
    matches = re.findall(r"<think\b[^>]*>(.*?)(?:</think\s*>|$)", content, flags=re.IGNORECASE | re.DOTALL)
    if not matches:
        return None
    joined = "\n\n".join(m.strip() for m in matches if m.strip())
    return joined.strip() or None


def should_render_ansi(
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> bool:
    """Determine whether TUI string rendering should preserve ANSI escape styling."""
    if force_color is True:
        return True
    if force_color is False or no_color is True:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("STACKMIND_NO_COLOR") in {"1", "true", "yes"}:
        return False
    if os.environ.get("FORCE_COLOR") in {"1", "true", "yes"}:
        return True
    # Non-TTY environments (redirected / piped stdout) should not force ANSI
    if hasattr(sys.stdout, "isatty") and not sys.stdout.isatty():
        return False
    return True


def _make_capture_console(
    width: int,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> tuple[Console, bool]:
    buf = io.StringIO()
    use_ansi = should_render_ansi(force_color=force_color, no_color=no_color)
    if use_ansi:
        console = Console(
            file=buf,
            record=True,
            width=width,
            force_terminal=True,
            color_system="truecolor",
        )
    else:
        console = Console(
            file=buf,
            record=True,
            width=width,
            force_terminal=False,
            color_system=None,
        )
    return console, use_ansi


def render_user_message(
    content: str,
    timestamp: str | None = None,
    *,
    accent: bool = True,
    show_timestamp: bool | None = None,
) -> RenderableType:
    """Render a user message per §14 (visually quiet, 'You' header, unboxed, compact)."""
    if accent:
        hdr = Text("│ ", style="bold #38bdf8").append("You", style="bold #38bdf8")
    else:
        hdr = Text("You", style="bold #38bdf8")

    # Per §17: Suppress timestamps from normal chat rows unless explicitly enabled
    show_ts = show_timestamp if show_timestamp is not None else (timestamp is not None and show_timestamp is not False)
    if show_timestamp is False:
        show_ts = False

    if show_ts and timestamp:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(hdr, Text(timestamp, style="dim #64748b"))
        top_elem: RenderableType = grid
    else:
        top_elem = hdr

    lines: list[RenderableType] = [Text(""), top_elem]
    for line in content.splitlines():
        if accent:
            msg_line = Text("│ ", style="bold #38bdf8")
            msg_line.append(line, style="white on grey19")
        else:
            msg_line = Text(line, style="white")
        lines.append(msg_line)
    return Group(*lines)


def render_user_message_str(
    content: str,
    width: int = 80,
    timestamp: str | None = None,
    *,
    accent: bool = True,
    show_timestamp: bool | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render a user message as plain formatted string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_user_message(
            content,
            timestamp=timestamp,
            accent=accent,
            show_timestamp=show_timestamp,
        )
    )
    return console.export_text(styles=use_ansi).rstrip()


def render_actions_group(
    group: Any,
) -> RenderableType:
    """Render compact Actions disclosure group per §20, §21."""
    if isinstance(group, list):
        from cli.tui.state import ActionsGroup
        actions_obj = ActionsGroup(actions=group)
    else:
        actions_obj = group

    if not actions_obj or getattr(actions_obj, "is_empty", False):
        return Text("")

    arrow = "▾" if actions_obj.expanded else "▸"
    count = len(actions_obj.actions)
    failed_count = sum(1 for a in actions_obj.actions if str(getattr(a, "status", "")).upper() in {"FAILED", "FAILURE", "ERROR"})
    if getattr(actions_obj, "active", False) or getattr(actions_obj, "has_running", False):
        suffix = f"{count}"
        suffix_style = "dim white"
    elif failed_count > 0:
        if getattr(actions_obj, "completed_count", 0) > 0:
            suffix = f"{actions_obj.completed_count} completed, {failed_count} failed"
        else:
            suffix = f"{failed_count} failed"
        suffix_style = "bold red"
    else:
        suffix = f"{count} completed"
        suffix_style = "dim white"

    header = Text()
    header.append(f"{arrow} ", style="bold red" if failed_count > 0 else "bold cyan")
    header.append("Actions", style="bold white")
    header.append(" · ", style="dim")
    header.append(suffix, style=suffix_style)

    if not actions_obj.expanded:
        return header

    lines: list[RenderableType] = [header]
    for act in actions_obj.actions:
        line = Text("  ")
        sym = act.symbol
        if sym == "✓":
            line.append(sym, style="bold green")
        elif sym == "●":
            line.append(sym, style="bold yellow")
        elif sym == "×":
            line.append(sym, style="bold red")
        elif sym == "⊘":
            line.append(sym, style="dim white")
        else:
            line.append(sym, style="white")

        line.append(f" {act.description}", style="white")
        if getattr(act, "error", None) and str(getattr(act, "status", "")).upper() in {"FAILED", "FAILURE", "ERROR"}:
            from cli.tui.events import clean_error_message
            clean_err = clean_error_message(act.error)
            line.append(f" ({clean_err})", style="bold red")
        lines.append(line)

    return Group(*lines)


def render_actions_group_str(
    group: Any,
    width: int = 80,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render actions disclosure group as plain or ANSI formatted string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(render_actions_group(group))
    return console.export_text(styles=use_ansi).rstrip()


def is_stray_command_bar(content: str) -> bool:
    """Detect unformatted command bar / footer remnants that bled into messages."""
    stripped = content.strip()
    if stripped.startswith("Available commands:"):
        return False
    if ":help" in stripped and ":status" in stripped and (":events" in stripped or ":landing" in stripped or ":diff" in stripped):
        if len(stripped.splitlines()) <= 2 or "StackMind v" in stripped:
            return True
    if "Ctrl+K commands" in stripped and "Ctrl+L clear" in stripped:
        return True
    return False


def render_assistant_message(
    content: str,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
    inline_diffs: bool = True,
    actions: Any | None = None,
    thinking: str | None = None,
    model: str | None = None,
) -> RenderableType:
    """Render an assistant message with '✦ StackMind' header (§15), dynamic turn regions (§18, §20, §24), left-aligned model attribution, and rich Markdown (§32)."""
    model_val = model
    working_content = content
    if not model_val and working_content:
        m = re.match(r"^\s*#*\s*Response from\s+([^\n\r]+?)\s*:?\s*(?:\n+|$)", working_content, flags=re.IGNORECASE)
        if m:
            model_val = m.group(1).strip()
            working_content = working_content[m.end():]

    hdr = Text("✦ ", style="bold #a855f7").append("StackMind", style="bold white")
    if model_val:
        hdr.append(f" ({model_val})", style="italic dim #94a3b8")

    # Per §17: Suppress timestamps from normal chat rows unless explicitly enabled
    show_ts = show_timestamp if show_timestamp is not None else (timestamp is not None and show_timestamp is not False)
    if show_timestamp is False:
        show_ts = False

    if show_ts and timestamp:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(hdr, Text(timestamp, style="dim #64748b"))
        top_elem: RenderableType = grid
    else:
        top_elem = hdr

    elements: list[RenderableType] = [Text(""), top_elem]

    # Dynamic Region 1: Turn Actions disclosure group (§18, §20, §21)
    if actions is not None:
        if isinstance(actions, list):
            from cli.tui.state import ActionsGroup
            actions_obj = ActionsGroup(actions=actions)
        else:
            actions_obj = actions
        if not getattr(actions_obj, "is_empty", False):
            elements.append(Text(""))
            elements.append(render_actions_group(actions_obj))

    # Dynamic Region 2: Provider-generated thinking/reasoning (§18, §24, §27)
    if thinking and thinking.strip():
        elements.append(Text(""))
        clean_think = thinking.strip()
        if clean_think == "✓":
            think_text = Text("Thinking... ✓", style="dim italic #94a3b8")
        else:
            think_text = Text("Thinking...\n", style="dim italic #94a3b8")
            for tline in clean_think.splitlines():
                think_text.append(f"  {tline}\n", style="dim #cbd5e1")
        elements.append(think_text)

    cleaned = strip_internal_reasoning(working_content)

    # Dynamic Region 3: Final assistant response (§18, §32) with operational telemetry dimming
    if cleaned:
        telemetry_lines: list[str] = []
        body_lines: list[str] = []
        for line in cleaned.splitlines():
            if re.match(r"^\s*Knowledge revision:\s*.*$", line, flags=re.IGNORECASE):
                telemetry_lines.append(line.strip())
            else:
                body_lines.append(line)
        cleaned_body = "\n".join(body_lines).strip()

        if cleaned_body:
            elements.append(Text(""))
            if inline_diffs and (
                cleaned_body.startswith("diff --git ")
                or (cleaned_body.startswith("--- ") and "\n+++ " in cleaned_body)
            ):
                from cli.tui.diff import render_unified_diff
                body: RenderableType = render_unified_diff(cleaned_body)
            else:
                body = Markdown(cleaned_body, code_theme="monokai")
            elements.append(body)

        for tline in telemetry_lines:
            elements.append(Text(""))
            elements.append(Text(tline, style="dim #64748b"))

    return Group(*elements)


def render_assistant_message_str(
    content: str,
    width: int = 80,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
    inline_diffs: bool = True,
    actions: Any | None = None,
    thinking: str | None = None,
    model: str | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render an assistant message as plain formatted string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_assistant_message(
            content,
            timestamp=timestamp,
            show_timestamp=show_timestamp,
            inline_diffs=inline_diffs,
            actions=actions,
            thinking=thinking,
            model=model,
        )
    )
    return console.export_text(styles=use_ansi).strip()


def render_assistant_stream_header(
    model: str | None = None,
    width: int = 80,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render the opening header for progressive assistant response streaming."""
    hdr = Text("✦ ", style="bold #a855f7").append("StackMind", style="bold white")
    if model:
        hdr.append(f" ({model})", style="italic dim #94a3b8")
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(hdr)
    return console.export_text(styles=use_ansi).rstrip()



def render_system_message(
    content: str,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
) -> RenderableType:
    """Render a system message with '✦ System' header and dimmed secondary styling."""
    hdr = Text("✦ ", style="dim cyan").append("System", style="dim #94a3b8")
    show_ts = show_timestamp if show_timestamp is not None else (timestamp is not None and show_timestamp is not False)
    if show_timestamp is False:
        show_ts = False

    if show_ts and timestamp:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(hdr, Text(timestamp, style="dim #64748b"))
        top_elem: RenderableType = grid
    else:
        top_elem = hdr

    lines: list[RenderableType] = [top_elem]
    for line in content.splitlines():
        if "\x1b[" in line:
            lines.append(Text.from_ansi(line))
        elif re.match(r"^\s*Knowledge revision:\s*.*$", line, flags=re.IGNORECASE):
            lines.append(Text(line, style="dim #64748b"))
        elif "thinking" in line.lower() or "turn submitted" in line.lower():
            lines.append(Text(line, style="dim cyan"))
        else:
            lines.append(Text(line, style="dim #64748b"))
    return Group(*lines)


def render_system_message_str(
    content: str,
    width: int = 80,
    timestamp: str | None = None,
    *,
    show_timestamp: bool | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render a system message as plain or ANSI formatted string using in-memory capture."""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_system_message(
            content,
            timestamp=timestamp,
            show_timestamp=show_timestamp,
        )
    )
    return console.export_text(styles=use_ansi).rstrip()


def render_chat_transcript(
    messages: list[ChatMessage],
    *,
    width: int | None = None,
    padding: tuple[int, int] = (0, 1),
    show_timestamps: bool = False,
    accent: bool = True,
    inline_diffs: bool = True,
) -> RenderableType:
    """Render the full conversation transcript with left alignment, small horizontal padding (§13), and inline diffs (§33).

    Caches rendered message strings to avoid repeated Markdown parsing for stable messages.
    """
    if not messages:
        empty_text = Text("(No messages in conversation yet.)", style="dim italic")
        return Padding(empty_text, padding) if padding else empty_text

    elements: list[RenderableType] = []
    for msg in messages:
        if is_stray_command_bar(msg.content):
            continue
        role = msg.role.lower()
        if role == "user":
            elements.append(
                render_user_message(
                    msg.content,
                    timestamp=msg.timestamp if show_timestamps else None,
                    accent=accent,
                    show_timestamp=show_timestamps,
                )
            )
        elif role == "assistant":
            elements.append(
                render_assistant_message(
                    msg.content,
                    timestamp=msg.timestamp if show_timestamps else None,
                    show_timestamp=show_timestamps,
                    actions=getattr(msg, "actions", None),
                    thinking=getattr(msg, "thinking", None),
                    model=getattr(msg, "model", None),
                    inline_diffs=inline_diffs,
                )
            )
        elif role == "system":
            elements.append(
                render_system_message(
                    msg.content,
                    timestamp=msg.timestamp if show_timestamps else None,
                    show_timestamp=show_timestamps,
                )
            )
        elif role == "tool":
            from cli.tui.events import ToolActivity, render_tool_activity
            parts = msg.content.split(None, 1)
            t_name = parts[0] if parts else "tool"
            t_target = parts[1] if len(parts) > 1 else ""
            activity = ToolActivity(tool_name=t_name, target=t_target, status="COMPLETED")
            elements.append(render_tool_activity(activity))
        elif role == "diff":
            from cli.tui.diff import render_unified_diff
            elements.append(render_unified_diff(msg.content))
        else:
            header = Text(f"✦ {msg.role.title()}", style="dim cyan")
            elements.append(Group(header, Markdown(msg.content, code_theme="monokai")))
        elements.append(Text(""))

    # Drop trailing blank separator
    if elements and isinstance(elements[-1], Text) and not elements[-1].plain:
        elements.pop()

    body_group = Group(*elements)
    return Padding(body_group, padding) if padding else body_group


def render_chat_transcript_str(
    messages: list[ChatMessage],
    width: int = 80,
    *,
    padding: tuple[int, int] = (0, 1),
    show_timestamps: bool = False,
    accent: bool = True,
    force_color: bool | None = None,
    no_color: bool | None = None,
    inline_diffs: bool = True,
) -> str:
    """Render the full conversation transcript as plain or ANSI formatted string using in-memory capture.

    Caches rendered output to avoid repeated Markdown parsing for unchanged transcripts.
    Cache key includes ALL messages to prevent stale output when earlier messages change.
    """
    # Generate cache key from complete transcript and render parameters
    cache_key = None
    if messages:
        transcript_hash = _compute_transcript_hash(messages)
        cache_key = _compute_cache_key(
            transcript_hash, width,
            inline_diffs=inline_diffs,
            show_timestamps=show_timestamps,
            accent=accent,
        )
        cached = _get_cached_render(cache_key)
        if cached is not None:
            return cached

    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(
        render_chat_transcript(
            messages,
            width=width,
            padding=padding,
            show_timestamps=show_timestamps,
            accent=accent,
            inline_diffs=inline_diffs,
        )
    )
    result = console.export_text(styles=use_ansi).rstrip()
    if cache_key is not None:
        _set_cached_render(cache_key, result)
    return result


__all__ = [
    "_make_capture_console",
    "extract_internal_reasoning",
    "is_stray_command_bar",
    "render_actions_group",
    "render_actions_group_str",
    "render_assistant_message",
    "render_assistant_message_str",
    "render_chat_transcript",
    "render_chat_transcript_str",
    "render_system_message",
    "render_system_message_str",
    "strip_internal_reasoning",
    "render_user_message",
    "render_user_message_str",
    "should_render_ansi",
    "_TRANSCRIPT_CACHE",
    "_TRANSCRIPT_CACHE_MAX_SIZE",
    "_clear_cache_if_needed",
]
