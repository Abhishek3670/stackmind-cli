"""Unit tests for TUI ANSI styling pipeline, turn separation, and word-boundary truncation (WO-004)."""

from __future__ import annotations

import os
import sys
from rich.text import Text

from cli.tui.chat import (
    render_assistant_message,
    render_assistant_message_str,
    render_assistant_stream_header,
    render_user_message,
    render_user_message_str,
    should_render_ansi,
)
from cli.tui.runtime_panel import (
    format_agent_tree,
    format_work_orders,
)


def test_should_render_ansi_flags(monkeypatch):
    """Verify should_render_ansi logic for NO_COLOR, FORCE_COLOR, and non-TTY environments."""
    # Explicit override flags
    assert should_render_ansi(force_color=True) is True
    assert should_render_ansi(force_color=False) is False
    assert should_render_ansi(no_color=True) is False

    # NO_COLOR environment variable
    monkeypatch.setenv("NO_COLOR", "1")
    assert should_render_ansi() is False
    assert should_render_ansi(force_color=True) is True  # explicit parameter takes precedence
    monkeypatch.delenv("NO_COLOR", raising=False)

    # STACKMIND_NO_COLOR environment variable
    monkeypatch.setenv("STACKMIND_NO_COLOR", "true")
    assert should_render_ansi() is False
    monkeypatch.delenv("STACKMIND_NO_COLOR", raising=False)

    # FORCE_COLOR environment variable
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert should_render_ansi() is True
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def test_user_message_str_ansi_preservation():
    """Verify render_user_message_str preserves ANSI styles when force_color is True."""
    output = render_user_message_str("Inspect database schema", force_color=True)
    # Must contain ANSI escape sequence
    assert "\x1b[" in output
    assert "You" in output
    assert "Inspect database schema" in output


def test_assistant_message_str_ansi_preservation():
    """Verify render_assistant_message_str preserves ANSI styles when force_color is True."""
    output = render_assistant_message_str(
        "Here is the plan.",
        model="claude-3-5-sonnet",
        force_color=True,
    )
    # Must contain ANSI escape sequences for header and styling
    assert "\x1b[" in output
    assert "✦" in output
    assert "StackMind" in output
    assert "claude-3-5-sonnet" in output


def test_no_color_environment_strips_ansi(monkeypatch):
    """Verify NO_COLOR environment suppresses all ANSI escape sequences."""
    monkeypatch.setenv("NO_COLOR", "1")

    u_out = render_user_message_str("Plain user prompt")
    assert "\x1b[" not in u_out
    assert "You" in u_out
    assert "Plain user prompt" in u_out

    a_out = render_assistant_message_str("Plain assistant reply", model="qwen2.5-coder")
    assert "\x1b[" not in a_out
    assert "✦ StackMind" in a_out
    assert "Plain assistant reply" in a_out


def test_explicit_no_color_parameter():
    """Verify no_color parameter explicitly suppresses ANSI codes."""
    u_out = render_user_message_str("User message", no_color=True)
    assert "\x1b[" not in u_out

    a_out = render_assistant_message_str("Assistant message", no_color=True)
    assert "\x1b[" not in a_out


def test_user_turn_separation_and_body_styling():
    """Verify vertical blank line before user header and 'white on grey19' background styling."""
    renderable = render_user_message("First line\nSecond line", accent=True)
    elements = renderable.renderables

    # 1. Blank line precedes the header for vertical turn spacing
    assert isinstance(elements[0], Text)
    assert elements[0].plain == ""

    # 2. Header line is second
    assert "You" in elements[1].plain

    # 3. Body lines render with 'white on grey19' under accent=True
    body_line_1 = elements[2]
    body_line_2 = elements[3]
    assert isinstance(body_line_1, Text)
    assert "First line" in body_line_1.plain
    # Verify the text span has style 'white on grey19'
    spans_1 = [s for s in body_line_1.spans if "First line" in body_line_1.plain[s.start:s.end]]
    assert any(s.style == "white on grey19" for s in spans_1)

    assert isinstance(body_line_2, Text)
    assert "Second line" in body_line_2.plain
    spans_2 = [s for s in body_line_2.spans if "Second line" in body_line_2.plain[s.start:s.end]]
    assert any(s.style == "white on grey19" for s in spans_2)


def test_user_turn_unaccented_body_styling():
    """Verify accent=False keeps body lines unshaded with plain 'white' styling."""
    renderable = render_user_message("Unaccented line", accent=False)
    elements = renderable.renderables

    # Blank line precedes header
    assert isinstance(elements[0], Text)
    assert elements[0].plain == ""

    # Body line has style 'white' without grey19 background
    body_line = elements[2]
    assert "Unaccented line" in body_line.plain
    assert body_line.style == "white" or any(s.style == "white" for s in body_line.spans)
    assert "grey19" not in str(body_line.style)
    assert not any("grey19" in str(s.style) for s in body_line.spans)


def test_assistant_model_badge_italic_styling():
    """Verify assistant model badge is styled with 'italic dim #94a3b8'."""
    renderable = render_assistant_message("Body content", model="qwen2.5-coder:7b")
    hdr = renderable.renderables[0]
    assert isinstance(hdr, Text)
    assert "(qwen2.5-coder:7b)" in hdr.plain

    # Check for italic dim #94a3b8 style on the model badge
    badge_spans = [s for s in hdr.spans if "qwen2.5-coder:7b" in hdr.plain[s.start:s.end]]
    assert len(badge_spans) > 0
    style_str = str(badge_spans[0].style)
    assert "italic" in style_str
    assert "dim" in style_str
    assert "#94a3b8" in style_str


def test_runtime_panel_work_order_word_boundary_truncation():
    """Verify work order title truncation breaks cleanly on word boundaries via textwrap.shorten."""
    long_title = "Implement user authentication and session management"
    work_orders = [
        {"id": "WO-042", "title": long_title, "status": "RUNNING"},
    ]

    # With width=30, max_title_len is max(14, 30 - 18) = 14
    # textwrap.shorten("Implement user authentication and session management", width=14, placeholder="…")
    # -> "Implement…" (breaks at word boundary after 'Implement', NOT cutting into 'user' mid-word)
    lines = format_work_orders(work_orders, width=30)
    plain_text = "".join(l.plain for l in lines)
    assert "WO-042" in plain_text
    assert "…" in plain_text
    # Mid-word cutting would produce e.g. "Implement us…"
    assert "Implement…" in plain_text
    assert "Implement us…" not in plain_text


def test_runtime_panel_agent_badge_word_boundary_truncation():
    """Verify agent hierarchy model badges truncate on word boundaries without mid-word splits."""
    agents = [
        {
            "name": "Architecture",
            "role": "Architect",
            "status": "orchestrating",
            "backend": "ollama",
            "model": "qwen2.5-coder:32b-instruct-q4_K_M",
        },
        {
            "name": "Backend",
            "role": "Worker",
            "status": "running",
            "backend": "ollama",
            "model": "deepseek-coder:33b",
        },
    ]

    # Format agent tree with narrow width
    lines = format_agent_tree(agents, width=28)
    plain_text = "\n".join(l.plain for l in lines)
    assert "Architecture" in plain_text
    assert "Backend" in plain_text
    # Badges must contain placeholder '…' and not cut mid-word
    assert "…" in plain_text
