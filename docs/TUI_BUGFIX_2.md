# StackMind TUI — Bugfix Checklist (Round 2)

## Scope

Extends `TUI_BUGFIX.md` with findings from a second terminal transcript
(Windows PowerShell, `python -m cli.main tui`) plus source inspection of
`cli/tui/app.py`, `cli/tui/layout.py`, and `cli/tui/runtime_panel.py`.

Round 1 §7 listed several behaviors as "verify before declaring
defects". Where inspection was conclusive, this document records the
result. Findings are labeled:

- **(source-verified)** — confirmed by reading the implementation.
- **(transcript-only)** — visible in output; verify in source before
  changing code.

Preserve the Rich-first interface, existing command dispatch, and
daemon-owned execution, state, policy, and persistence.

## 1. Runtime Panel Is Rendered Once, Not Persistent

Priority: High (source-verified)

### Observed

`tui()` in `cli/tui/app.py` echoes the top header, landing block, and
workspace layout a single time, then enters a `while True` composer
loop. The right-hand "StackMind Runtime" panel (AGENTS, WORK ORDERS,
CURRENT OPERATION) is never re-rendered for the rest of the session and
scrolls away with transcript history. Agent statuses and work-order
states on screen are a frozen boot-time snapshot.

This contradicts the layout contract (IMPLEMENTATION_PLAN_TUI.md §6,
§12), which specifies a *persistent* runtime panel, and misleads the
operator about live system state.

### Fix

- Choose one rendering architecture and make it honest:
  - **Option A — live region**: anchor the layout with `rich.live.Live`
    and re-render on daemon state change. Note that plain `input()`
    cannot coexist with Live refresh without clobbering typed input;
    this option depends on raw-mode input (Issue 2) or a redraw-safe
    input strategy.
  - **Option B — snapshot REPL**: keep the print-once model and re-echo
    a compact runtime snapshot after each completed turn, explicitly
    labeled as a point-in-time snapshot with a refresh command.
- Do not simulate liveness. Only render state the daemon actually
  reported via existing event/session mechanisms.
- Document which option is implemented.

### Acceptance

- Under Option A: agent and work-order rows shown after turn N reflect
  daemon state after turn N; no stale "waiting/orchestrating" rows.
- Under Option B: snapshot is labeled as such and refreshable.
- Partially typed composer input survives any re-render (§30).
- Round 1 §7 item "Contextual status output does not become a permanent
  dashboard" remains satisfied.

## 2. Advertised Keyboard Shortcuts Are Not Implemented

Priority: High (source-verified)

### Observed

The composer renders "Ctrl+K commands | Ctrl+L clear" on every frame
(`render_composer_box`, `render_composer_top_border`). The input path is
plain `input()` inside `prompt_composer_input`; there is no raw-mode
keyboard handling anywhere in the codebase (no `msvcrt`, `termios`, or
`tty` usage). Round 1 §7 listed "Ctrl+K performs the advertised
supported action" as unverified — inspection now confirms it is
unimplemented.

### Fix

- Either implement the shortcuts or remove the hints:
  - Raw-mode input, platform-aware (`msvcrt` on Windows,
    `termios`/`tty` on POSIX), with Ctrl+K and Ctrl+L bound to real,
    tested actions; or
  - Remove the shortcut text from all composer chrome until a binding
    exists.
- Raw-mode input, if added, must not regress paste, line editing, IME,
  or history recall that `input()` currently provides.

### Acceptance

- No keyboard shortcut is advertised unless pressing it performs the
  advertised action in Windows PowerShell and a POSIX terminal.
- Ctrl+C cancellation semantics unchanged.
- Terminal state fully restored on exit and exceptions (§43, §44).
- Composer tests updated to match the shipped chrome.

## 3. Glyph Corruption on Windows Consoles

Priority: High

### Observed

Tool activity lines render with mojibake prefixes (e.g. broken
multi-byte artifacts before "Run harness ... RUNNING").
`_ensure_utf8()` does not guarantee glyph support: default Windows
console code pages (CP437/CP1252) cannot render many box-drawing and
status glyphs, and conhost may also ignore UTF-8 output modes.

### Fix

- Introduce a centralized glyph table with ASCII fallbacks
  (`[OK]`, `[RUN]`, `[FAIL]`, `[..]`, `|-`) selected at render time from
  detected console encoding/capabilities.
- Remove hardcoded Unicode literals from renderers that run on Windows;
  route all status/box glyphs through the table.
- Keep Unicode glyphs where the terminal supports them (Windows
  Terminal, UTF-8 code pages).

### Acceptance

- No mojibake under Windows PowerShell with default code pages.
- Fallback rendering covered by deterministic tests (force fallback
  mode in tests).
- Status glyphs remain visually distinguishable in fallback mode.
- All existing visual-fidelity tests pass in both glyph modes.

## 4. Transcript Hygiene: Mixed Conventions and Leaked Internals

Priority: Medium

### Observed

A single screen mixes at least three message conventions:

- "Response from qwen2.5-coder:7b" renders centered while its body
  renders at column 0.
- User messages render as stacked `You` / `hey` lines with no shared
  header style.
- Operational text appears as transcript rows: "Thinking.. Turn
  submitted to the governed daemon." and "Knowledge revision: 0".

### Fix

- Define one header format per role (user, assistant, system, error)
  and apply it in every renderer (`cli/tui/chat.py` and callers).
- Route telemetry (knowledge revision, operation ids, backend names) to
  `:events`, `:status`, or a dim system line style — not the
  conversational stream. Round 1 §2 already covers non-final content;
  this extends it to telemetry lines.
- Replace internal jargon ("governed daemon") in user-facing text with
  plain language, or gate it behind an existing diagnostics view.
- Do not use streaming-flavored language ("Thinking..") when the
  transport is request/response; Round 1 §4 guardrail applies: progress
  must reflect actual daemon events, not simulated streaming.

### Acceptance

- All messages of a role share one visual convention, including
  alignment.
- A normal turn emits zero telemetry lines into the chat stream.
- A fresh reader can distinguish user/assistant/system rows without
  reading prefixes.
- Detailed lifecycle information remains available via `:events`.

## 5. Landing Panel: Dead Space and Duplicate Metadata

Priority: Medium

### Observed

- The landing block reserves a large empty region below the session
  metadata (visible dead space in the bordered panel).
- Session id, status, and project path appear both in the top header
  bar and again inside the landing panel, including the full session
  UUID twice on one screen. Round 1 §6 ("Status Metadata Needs
  Responsive Layout") flagged the general problem; this is a specific
  instance. (transcript-only)

### Fix

- Size the landing panel to its content or fill the region with
  purposeful content (e.g. quick-start hints already defined in
  `cli/tui/landing.py`).
- Show full session identifiers once. Prefer the top header for
  context; keep the landing to identity, pillars, and actions.
- Offer full values through an existing details view rather than
  repeating them.

### Acceptance

- No large empty region inside the landing border.
- Session id appears at most once as a full UUID; abbreviated forms
  elsewhere.
- Landing remains readable at narrow, medium, and wide widths.

## 6. Composer Placeholder Persists While Typing

Priority: Low (transcript-only)

### Observed

The placeholder "Type a message..." stays embedded in the composer's
top border even after the user submits text. With content present, the
border text and the body text disagree about the box's state.

### Fix

- When the composer has content (or after first submit), swap the
  border text to a neutral label or the active shortcuts only.
- Preserve the placeholder for the empty state.

### Acceptance

- Border text reflects composer state (empty vs. filled).
- `prompt_composer_input` round-trips content without duplicating the
  placeholder into the message.

## 7. Conversation Readability on Very Wide Terminals

Priority: Low

### Observed

At large terminal widths the conversation column spans the full
remaining width (runtime panel is capped at 26–42 cols by
`compute_layout`, but conversation is not capped), producing very long
line lengths for chat text.

### Fix

- Cap the conversation column at a readable measure (e.g. ~120 cols)
  and center or left-anchor it consistently with the runtime column.
- Keep the composer matching the conversation width, not the terminal
  width.

### Acceptance

- Chat line length stays within the chosen cap at any terminal width.
- No horizontal clipping at narrow widths (Round 1 §6 acceptance
  holds).
- Layout math remains deterministic and tested.

## Implementation Guardrails

- Refactor the active `cli/tui/app.py` package only; do not resurrect
  superseded modules.
- Reuse StackMindTuiAdapter and DaemonClient; do not add a second event
  transport or persistence store.
- Use existing Rich dependencies; do not add Textual or prompt_toolkit
  without an architectural decision record.
- Do not invent operations, statuses, or token streaming to make the
  panel look live.
- Centralize glyphs and message headers; avoid per-renderer literals.
- Every advertised affordance (shortcut, hint, indicator) must map to
  implemented, tested behavior.
- Preserve §28–§30 composer buffer/focus behavior.

## Validation

- [ ] Record baseline test results and pre-existing failures.
- [ ] Add deterministic rendering tests for: runtime snapshot (Option
      B) or live refresh (Option A), glyph fallback mode, message
      header conventions, and composer state swap.
- [ ] Manually verify Ctrl+K/Ctrl+L behavior (or absence of hints) in
      Windows PowerShell and one POSIX terminal.
- [ ] Manually verify typing during live updates and terminal resizing.
- [ ] Verify no mojibake under default Windows code pages and under
      Windows Terminal.
- [ ] Run the full TUI suite (`tests/test_tui_*.py`) and report
      regressions separately from baseline failures.

## Completion Criteria

The runtime panel reflects real daemon state (or is honestly labeled),
no affordance is advertised that does not exist, Windows terminals
render without glyph corruption, and the transcript follows one
consistent message convention — without changing the governed runtime
or hiding access to operational details.
