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
        else:
            unfenced = rest.strip()

    def _safe_json_parse(s: str) -> dict[str, Any] | None:
        if not s or not isinstance(s, str):
            return None
        s_clean = s.strip()
        if not (s_clean.startswith("{") and s_clean.endswith("}")):
            return None
        try:
            import json
            obj = json.loads(s_clean, strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # Healing pass for invalid JSON escapes (regexes like \s, \d, \w, \.)
        try:
            import json
            healed = re.sub(r'\\(?![/\\bfnrtu"\'\n\r\t]|u[0-9a-fA-F]{4})', r'\\\\', s_clean)
            obj = json.loads(healed, strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # Healing pass for single-quoted dict strings
        try:
            import ast
            obj = ast.literal_eval(s_clean)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            import json
            obj, _ = json.JSONDecoder(strict=False).raw_decode(s_clean)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return None

    def _is_contract_dict(d: Any) -> bool:
        if not isinstance(d, dict):
            return False
        return (
            "status" in d
            or "verdict" in d
            or "modified_files" in d
            or "blockers" in d
            or "decision" in d
            or "review_notes" in d
            or "report_markdown" in d
            or "summary" in d
            or "response" in d
            or "reason" in d
            or "message" in d
        )

    def _format_dict(d: dict[str, Any]) -> str:
        verdict = d.get("verdict")
        review_notes = d.get("review_notes")
        if review_notes and isinstance(review_notes, str) and review_notes.strip():
            clean_rn = review_notes.strip()
            if verdict:
                return f"### QA Verdict: {verdict}\n\n{clean_rn}"
            return clean_rn

        summary = d.get("summary")
        if summary and isinstance(summary, str) and summary.strip() and summary.strip() != "Completed task via tools":
            stripped = summary.strip()
            if stripped.startswith("```") or (stripped.startswith("{") and stripped.endswith("}")):
                return format_contract_payload(stripped)
            return stripped

        report_md = d.get("report_markdown")
        if report_md and isinstance(report_md, str) and report_md.strip() and report_md.strip() != "Completed task via tools":
            stripped = report_md.strip()
            if stripped.startswith("```") or (stripped.startswith("{") and stripped.endswith("}")):
                return format_contract_payload(stripped)
            return stripped

        response = d.get("response")
        if response and isinstance(response, str) and response.strip():
            return response.strip()

        st = str(d.get("status", "")).lower()
        mods = d.get("modified_files") or []
        blockers = d.get("blockers") or []
        if st in {"completed", "success", "done"}:
            if mods:
                user_mods = [f for f in mods if not str(f).startswith((".sync", ".sync/"))]
                if user_mods:
                    files_str = ", ".join(f"`{f}`" for f in user_mods)
                    if any("PLAN.md" in str(f) for f in user_mods):
                        return f"Created implementation plan in {files_str}. Use `:plan` to review or `:approve` to proceed."
                    return f"Completed turn. Modified files: {files_str}."
                elif any("PLAN.md" in str(f) for f in mods):
                    return "Created implementation plan in `PLAN.md`. Use `:plan` to review or `:approve` to proceed."
                return "Completed turn via tools."
            if verdict:
                return f"### QA Verdict: {verdict}"
            return "Completed turn via tools."
        if st in {"blocked", "failed", "cancelled"}:
            if blockers:
                b_str = "; ".join(str(b) for b in blockers)
                return f"Turn {st}: {b_str}"
            reason = d.get("reason") or d.get("error") or "No details provided"
            return f"Turn {st}: {reason}"
        if verdict:
            return f"### QA Verdict: {verdict}"
        if "reason" in d or "error" in d:
            reason = d.get("reason") or d.get("error")
            return str(reason)
        return ""

    parsed = _safe_json_parse(unfenced)
    if parsed and _is_contract_dict(parsed):
        return _format_dict(parsed)

    # Check if text ends with a JSON block after conversational text
    match = re.search(r"\n*```(?:json)?\s*(\{[\s\S]*?\})\s*```\s*$", text)
    if not match:
        match = re.search(r"\n*(\{[\s\S]*?\})\s*$", text)
    if match:
        json_candidate = match.group(1).strip()
        parsed = _safe_json_parse(json_candidate)
        if parsed and _is_contract_dict(parsed):
            leading_text = text[:match.start()].strip()
            if leading_text:
                return leading_text
            return _format_dict(parsed)

    # Fallback: regex search for summary or key fields (handles single/double quotes, unclosed stream quotes)
    for key in ("summary", "report_markdown", "response", "review_notes"):
        m = re.search(rf'["\']{key}["\']\s*:\s*["\']((?:[^"\'\\]|\\.)*)["\']?', unfenced, re.DOTALL)
        if m:
            val = m.group(1)
            val = (
                val.replace('\\n', '\n')
                .replace('\\t', '\t')
                .replace('\\"', '"')
                .replace("\\'", "'")
                .replace('\\\\', '\\')
            )
            if val.strip() and val.strip() != "Completed task via tools":
                return val.strip()

    # Never dump raw JSON payloads or code fences
    if unfenced.startswith("{") or unfenced.startswith("```"):
        mods_m = re.search(r'["\']modified_files["\']\s*:\s*\[(.*?)\]', unfenced, re.DOTALL)
        if mods_m:
            files_raw = re.findall(r'["\']([^"\']+)["\']', mods_m.group(1))
            user_files = [f for f in files_raw if not f.startswith((".sync", ".sync/"))]
            if user_files:
                files_str = ", ".join(f"`{f}`" for f in user_files)
                if any("PLAN.md" in f for f in user_files):
                    return f"Created implementation plan in {files_str}. Use `:plan` to review or `:approve` to proceed."
                return f"Completed turn. Modified files: {files_str}."
            elif any("PLAN.md" in f for f in files_raw):
                return "Created implementation plan in `PLAN.md`. Use `:plan` to review or `:approve` to proceed."
        if unfenced.endswith("}"):
            return "Completed turn."
        # While JSON is streaming before summary key arrives, don't dump raw syntax
        return ""

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
    failed_count = getattr(actions_obj, "failed_count", 0)
    suffix = getattr(actions_obj, "header_suffix", f"{getattr(actions_obj, 'count', len(getattr(actions_obj, 'actions', [])))}")
    suffix_style = "bold red" if failed_count > 0 else "dim white"

    header = Text()
    header.append(f"{arrow} ", style="bold red" if failed_count > 0 else "bold cyan")
    header.append("Actions", style="bold white")
    header.append(" · ", style="dim")
    header.append(suffix, style=suffix_style)

    if not actions_obj.expanded:
        return header

    lines: list[RenderableType] = [header]
    vis_items = (
        actions_obj.get_visible_actions()
        if hasattr(actions_obj, "get_visible_actions")
        else [(a, 0) for a in getattr(actions_obj, "actions", [])]
    )
    for act, indent_lvl in vis_items:
        indent = "    " if indent_lvl > 0 else "  "
        line = Text(indent)
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

        desc = act.description
        if "(" in desc and desc.endswith(")"):
            action_part, _, target_part = desc.partition("(")
            target_part = target_part[:-1].strip()
            line.append(f" {action_part.strip()} ", style="white")
            line.append(f"({target_part})", style="cyan")
        else:
            line.append(f" {desc}", style="white")
        if getattr(act, "error", None) and str(getattr(act, "status", "")).upper() in {"FAILED", "FAILURE", "ERROR"}:
            from cli.tui.events import clean_error_message
            clean_err = clean_error_message(act.error)
            line.append(f" ({clean_err})", style="bold red")
        lines.append(line)

    prev_act = getattr(actions_obj, "latest_preview_action", None)
    if prev_act:
        prev_renderable = render_action_preview(prev_act)
        if prev_renderable:
            lines.append(Text(""))
            lines.append(prev_renderable)
    else:
        preview_label = getattr(actions_obj, "preview_label", None)
        preview_code = getattr(actions_obj, "preview_code", None)
        if preview_label and preview_code and preview_code.strip():
            lines.append(Text(""))
            lines.append(Text(f"  {preview_label}", style="white"))
            code_lines = preview_code.strip().splitlines()
            preview_lines = code_lines[:6]
            for cline in preview_lines:
                lines.append(Text(f"    │ {cline}", style="dim #cbd5e1"))
            if len(code_lines) > 6:
                lines.append(Text(f"    │ ... (+{len(code_lines) - 6} more lines)", style="dim #64748b"))

    return Group(*lines)


def render_action_preview(action: Any) -> RenderableType | None:
    """Render live or completed action preview: diff for edits, output for bash, code for writes."""
    if not action:
        return None
    t_name = str(getattr(action, "tool_name", "") or "").lower()
    target = getattr(action, "target", "") or ""
    status = str(getattr(action, "status", "")).upper()
    is_running = status == "RUNNING"
    is_failed = status in {"FAILED", "FAILURE", "ERROR"}

    diff = getattr(action, "diff", None)
    output = getattr(action, "output", None)
    code = getattr(action, "code", None)
    error = getattr(action, "error", None)

    # 1. Edit action -> Diff
    if diff or t_name in {"edit_file", "edit", "patch", "apply_patch"}:
        if is_running:
            lbl = Text("✍ Editing ", style="bold cyan").append(f"`{target}`...", style="cyan") if target else Text("✍ Editing file...", style="bold cyan")
        elif is_failed:
            lbl = Text("× Edit failed: ", style="bold red").append(f"`{target}`", style="red") if target else Text("× Edit failed", style="bold red")
        else:
            lbl = Text("✓ Edited ", style="bold green").append(f"`{target}`", style="white") if target else Text("✓ Edited file", style="bold green")

        lines: list[RenderableType] = [lbl]
        diff_text = diff or code or ""
        if diff_text.strip():
            d_lines = diff_text.strip().splitlines()
            preview = d_lines[:8]
            for dl in preview:
                t = Text("  │ ")
                if dl.startswith("+") and not dl.startswith("+++"):
                    t.append(dl, style="green")
                elif dl.startswith("-") and not dl.startswith("---"):
                    t.append(dl, style="red")
                elif dl.startswith("@@"):
                    t.append(dl, style="cyan dim")
                elif dl.startswith("---") or dl.startswith("+++"):
                    t.append(dl, style="dim white")
                else:
                    t.append(dl, style="white")
                lines.append(t)
            if len(d_lines) > 8:
                lines.append(Text(f"  │ ... (+{len(d_lines) - 8} more lines)", style="dim #64748b"))
        return Group(*lines)

    # 2. Bash / Command / Test action -> Output
    if t_name in {"exec_command", "run_command", "bash", "sh", "command", "run_tests", "pytest", "test", "harness"} or output:
        is_test = t_name in {"run_tests", "pytest", "test"}
        if is_running:
            if is_test:
                lbl = Text("⚡ Running tests: ", style="bold yellow").append(f"`{target}`...", style="yellow") if target else Text("⚡ Running tests...", style="bold yellow")
            else:
                lbl = Text("⚡ Running command: ", style="bold yellow").append(f"`{target}`...", style="yellow") if target else Text("⚡ Running command...", style="bold yellow")
        elif is_failed:
            if is_test:
                lbl = Text("× Tests failed: ", style="bold red").append(f"`{target}`", style="red") if target else Text("× Tests failed", style="bold red")
            else:
                lbl = Text("× Command failed: ", style="bold red").append(f"`{target}`", style="red") if target else Text("× Command failed", style="bold red")
        else:
            if is_test:
                lbl = Text("✓ Tests passed: ", style="bold green").append(f"`{target}`", style="white") if target else Text("✓ Tests passed", style="bold green")
            else:
                lbl = Text("✓ Ran ", style="bold green").append(f"`{target}`", style="white") if target else Text("✓ Ran command", style="bold green")

        lines = [lbl]
        body_text = output or error or ""
        if body_text.strip():
            b_lines = body_text.strip().splitlines()
            preview = b_lines[:8]
            for bl in preview:
                t = Text("  │ ").append(bl, style="dim #cbd5e1" if not is_failed else "dim red")
                lines.append(t)
            if len(b_lines) > 8:
                lines.append(Text(f"  │ ... (+{len(b_lines) - 8} more lines)", style="dim #64748b"))
        return Group(*lines)

    # 3. Write / Create action -> Code snippet
    if t_name in {"write_file", "write", "create_file", "create"} or code:
        if is_running:
            lbl = Text("✍ Writing ", style="bold cyan").append(f"`{target}`...", style="cyan") if target else Text("✍ Writing code...", style="bold cyan")
        elif is_failed:
            lbl = Text("× Write failed: ", style="bold red").append(f"`{target}`", style="red") if target else Text("× Write failed", style="bold red")
        else:
            lbl = Text("✓ Wrote ", style="bold green").append(f"`{target}`", style="white") if target else Text("✓ Wrote code", style="bold green")

        lines = [lbl]
        c_text = code or ""
        if c_text.strip():
            c_lines = c_text.strip().splitlines()
            preview = c_lines[:8]
            for cl in preview:
                t = Text("  │ ").append(cl, style="dim #cbd5e1")
                lines.append(t)
            if len(c_lines) > 8:
                lines.append(Text(f"  │ ... (+{len(c_lines) - 8} more lines)", style="dim #64748b"))
        return Group(*lines)

    # 4. Read action
    if t_name in {"read_file", "read", "view_file", "cat"}:
        if is_running:
            return Text("🔍 Reading ", style="bold cyan").append(f"`{target}`...", style="cyan") if target else Text("🔍 Reading file...", style="bold cyan")
        else:
            return Text("✓ Read ", style="bold green").append(f"`{target}`", style="white") if target else Text("✓ Read file", style="bold green")

    # 5. Generic action
    desc = getattr(action, "description", "") or t_name
    if is_running:
        return Text(f"⚡ {desc}...", style="bold yellow")
    elif is_failed:
        return Text(f"× {desc}", style="bold red")
    elif getattr(action, "output", None) or getattr(action, "error", None):
        out = action.output or action.error
        lines = [Text(f"✓ {desc}", style="bold green")]
        for bl in out.strip().splitlines()[:8]:
            lines.append(Text(f"  │ {bl}", style="dim #cbd5e1"))
        return Group(*lines)
    return None


def render_action_preview_str(
    action: Any,
    width: int = 80,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render action preview as plain or ANSI formatted string using in-memory capture."""
    elem = render_action_preview(action)
    if not elem:
        return ""
    console, use_ansi = _make_capture_console(
        width=width,
        force_color=force_color,
        no_color=no_color,
    )
    console.print(elem)
    return console.export_text(styles=use_ansi).rstrip()


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
    if cleaned:
        cleaned = format_contract_payload(cleaned)

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
    "render_action_preview",
    "render_action_preview_str",
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
