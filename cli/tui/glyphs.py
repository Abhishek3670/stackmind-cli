"""Centralized glyph table and console encoding capability detection.

Provides deterministic glyph selection with clean ASCII fallbacks for legacy
Windows consoles (CP437, CP1252) while preserving rich Unicode glyphs on modern
terminals (UTF-8, Windows Terminal).

Reference: PLAN_TUI_BUGFIX_ROUND2.md §3.3 & TUI_BUGFIX_2.md §3.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

# Global override for testing and explicit CLI flags (--ascii / --unicode)
_ASCII_OVERRIDE: Optional[bool] = None


@dataclass(frozen=True)
class GlyphSet:
    """Immutable collection of terminal glyphs."""

    # Status indicators
    DOT: str
    DOT_HOLLOW: str
    DOT_ORCHESTRATING: str

    # Actions and tools
    TOOL: str
    SPARKLE: str
    AGENT: str
    HAMMER: str

    # Verifications and checkmarks
    CHECK: str
    CHECK_HEAVY: str
    CROSS: str
    CROSS_HEAVY: str

    # Execution states
    SPIN: str
    RUNNING: str
    ELLIPSIS: str
    PAUSE: str

    # Navigation and disclosures
    DISCLOSURE_CLOSED: str
    DISCLOSURE_OPEN: str
    ARROW_RIGHT: str
    ARROW_LEFT: str
    POINTER: str

    # Box-drawing and tree branches
    TREE_BRANCH: str
    TREE_LAST: str
    TREE_VERT: str
    BOX_TL: str
    BOX_TR: str
    BOX_BL: str
    BOX_BR: str
    BOX_HORIZ: str
    BOX_VERT: str
    BOX_TEE_RIGHT: str
    BOX_TEE_LEFT: str


UNICODE_GLYPHS = GlyphSet(
    DOT="●",
    DOT_HOLLOW="○",
    DOT_ORCHESTRATING="●",
    TOOL="⚒",
    SPARKLE="✦",
    AGENT="🤖",
    HAMMER="🔨",
    CHECK="✓",
    CHECK_HEAVY="✔",
    CROSS="✗",
    CROSS_HEAVY="✖",
    SPIN="↻",
    RUNNING="●",
    ELLIPSIS="…",
    PAUSE="⏸",
    DISCLOSURE_CLOSED="▸",
    DISCLOSURE_OPEN="▾",
    ARROW_RIGHT="→",
    ARROW_LEFT="←",
    POINTER=">",
    TREE_BRANCH="├─ ",
    TREE_LAST="└─ ",
    TREE_VERT="│ ",
    BOX_TL="╭",
    BOX_TR="╮",
    BOX_BL="╰",
    BOX_BR="╯",
    BOX_HORIZ="─",
    BOX_VERT="│",
    BOX_TEE_RIGHT="├",
    BOX_TEE_LEFT="┤",
)

ASCII_GLYPHS = GlyphSet(
    DOT="*",
    DOT_HOLLOW="o",
    DOT_ORCHESTRATING="@",
    TOOL="[TOOL]",
    SPARKLE="*",
    AGENT="[AGENT]",
    HAMMER="[ACT]",
    CHECK="[OK]",
    CHECK_HEAVY="[OK]",
    CROSS="[FAIL]",
    CROSS_HEAVY="[FAIL]",
    SPIN="[RUN]",
    RUNNING="[RUN]",
    ELLIPSIS="...",
    PAUSE="[PAUSE]",
    DISCLOSURE_CLOSED=">",
    DISCLOSURE_OPEN="v",
    ARROW_RIGHT="->",
    ARROW_LEFT="<-",
    POINTER=">",
    TREE_BRANCH="|- ",
    TREE_LAST="`- ",
    TREE_VERT="| ",
    BOX_TL="+",
    BOX_TR="+",
    BOX_BL="+",
    BOX_BR="+",
    BOX_HORIZ="-",
    BOX_VERT="|",
    BOX_TEE_RIGHT="+",
    BOX_TEE_LEFT="+",
)

# Map for bulk string sanitization when forcing fallback mode
_REPLACEMENT_PAIRS: list[tuple[str, str]] = [
    ("●", "*"),
    ("○", "o"),
    ("◉", "@"),
    ("⚒", "[TOOL]"),
    ("✦", "*"),
    ("✓", "[OK]"),
    ("✔", "[OK]"),
    ("✗", "[FAIL]"),
    ("✖", "[FAIL]"),
    ("↻", "[RUN]"),
    ("…", "..."),
    ("⏸", "[PAUSE]"),
    ("▸", ">"),
    ("▾", "v"),
    ("→", "->"),
    ("←", "<-"),
    ("├─ ", "|- "),
    ("└─ ", "`- "),
    ("│ ", "| "),
    ("╭", "+"),
    ("╮", "+"),
    ("╰", "+"),
    ("╯", "+"),
    ("─", "-"),
    ("│", "|"),
]


def set_glyph_mode(force_ascii: Optional[bool]) -> None:
    """Explicitly override glyph rendering mode (True=ASCII, False=Unicode, None=Auto)."""
    global _ASCII_OVERRIDE
    _ASCII_OVERRIDE = force_ascii


def is_ascii_mode() -> bool:
    """Detect if ASCII fallback should be used based on terminal capabilities.

    Returns True if:
    1. Explicitly overridden via set_glyph_mode(True).
    2. STACKMIND_ASCII_GLYPHS or NO_UNICODE environment variable is set.
    3. Output encoding is not UTF-8 (e.g. CP437, CP1252, ascii) and not running
       inside Windows Terminal (WT_SESSION).
    """
    if _ASCII_OVERRIDE is not None:
        return _ASCII_OVERRIDE

    if os.environ.get("STACKMIND_ASCII_GLYPHS", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if os.environ.get("NO_UNICODE", "").strip().lower() in {"1", "true", "yes"}:
        return True

    # Windows Terminal supports full UTF-8 emojis and box drawing
    if "WT_SESSION" in os.environ:
        return False

    # Check stdout encoding
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    if encoding and "utf" not in encoding:
        return True

    if os.name == "nt":
        # Check if PYTHONIOENCODING is explicitly UTF-8
        pyio = os.environ.get("PYTHONIOENCODING", "").lower()
        if "utf" in pyio:
            return False
        # If encoding is not UTF-8 (CP437, CP1252, etc.)
        if not encoding or encoding in {"cp437", "cp1252", "ascii"}:
            return True

    return False


def get_glyphs(fallback: Optional[bool] = None) -> GlyphSet:
    """Return active GlyphSet based on detection or explicit fallback flag."""
    use_ascii = fallback if fallback is not None else is_ascii_mode()
    return ASCII_GLYPHS if use_ascii else UNICODE_GLYPHS


def get_glyph(name: str, fallback: Optional[bool] = None) -> str:
    """Retrieve an individual glyph by name."""
    glyphs = get_glyphs(fallback=fallback)
    return getattr(glyphs, name, "")


def status_glyph(status: str, fallback: Optional[bool] = None) -> str:
    """Return appropriate status glyph (dot, check, cross, or run)."""
    glyphs = get_glyphs(fallback=fallback)
    st = status.strip().lower()
    if st in {"completed", "done", "passed", "ok", "success", "healthy"}:
        return glyphs.CHECK
    if st in {"running", "active", "implementing", "in_progress", "online"}:
        return glyphs.DOT
    if st in {"failed", "error", "blocked"}:
        return glyphs.CROSS
    if st in {"orchestrating", "planning"}:
        return glyphs.DOT_ORCHESTRATING
    if st in {"waiting", "idle", "pending"}:
        return glyphs.DOT_HOLLOW
    if st in {"paused"}:
        return glyphs.PAUSE
    return glyphs.DOT


def tree_branch(is_last: bool = False, fallback: Optional[bool] = None) -> str:
    """Return tree branch glyph (├─ or └─, with |- or `- fallback)."""
    glyphs = get_glyphs(fallback=fallback)
    return glyphs.TREE_LAST if is_last else glyphs.TREE_BRANCH


def disclosure_glyph(expanded: bool = False, fallback: Optional[bool] = None) -> str:
    """Return disclosure icon (▸/▾ or >/v fallback)."""
    glyphs = get_glyphs(fallback=fallback)
    return glyphs.DISCLOSURE_OPEN if expanded else glyphs.DISCLOSURE_CLOSED


def sanitize_text(text: str, fallback: Optional[bool] = None) -> str:
    """Replace Unicode glyphs with ASCII equivalents if in fallback mode."""
    use_ascii = fallback if fallback is not None else is_ascii_mode()
    if not use_ascii:
        return text
    result = text
    for uni, asc in _REPLACEMENT_PAIRS:
        result = result.replace(uni, asc)
    return result
