# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.4.0] - 2026-09-23

### Added
- **Native Win32 Console Input Reader (Windows)**:
  - Low-level console input reader (`Win32ConsoleReader`) utilizing `kernel32.dll` (`ReadConsoleInputW`, `GetNumberOfConsoleInputEvents`) to bypass `msvcrt` limitations.
  - Recombination of UTF-16 surrogate pairs (`0xD800`–`0xDFFF`) into complete Unicode code points.
  - Multi-record VT escape sequence assembly buffer (`_read_escape_sequence`) for parsing escape sequences generated under `ENABLE_VIRTUAL_TERMINAL_INPUT`.
- **SGR Extended Mouse Scrolling**:
  - Full support for SGR mouse reporting mode 1006 (`\x1b[<64;...M` / `\x1b[<65;...M`) mapped to `<WHEEL_UP>` and `<WHEEL_DOWN>` across Windows and POSIX.
  - Conversation viewport vertical scrolling (`scroll_up`, `scroll_down`, Page Up, Page Down) in alternate screen buffer mode.
  - Non-wheel mouse clicks (`<MOUSE_0_...M>`) filtered to prevent composer buffer corruption.
- **Escape Sequence Navigation**:
  - Direct assembly and decoding of Page Up (`\x1b[5~`), Page Down (`\x1b[6~`), Up Arrow (`\x1b[A`), and Down Arrow (`\x1b[B`) VT sequences.

### Changed
- **Default Windows Console Input Routing**:
  - Promoted `_read_raw_key_windows_v2` (`Win32ConsoleReader`) to the default input reader on Windows platforms.
  - Preserved legacy `msvcrt.getwch()` reading path as an explicit fallback via `STACKMIND_LEGACY_INPUT=1`.

### Fixed
- **Windows Terminal Mouse Wheel Collapse**:
  - Resolved issue where mouse wheel events collapsed into Up/Down Arrow scan codes (`0xE0 0x48` / `0x50` via `msvcrt.getwch()`), causing stray navigation jumps.
- **Composer Escape Character Leakage**:
  - Fixed stray ANSI escape sequence characters (`[A`, `[B`) leaking into the composer prompt buffer by buffering multi-part escape sequences and retaining mouse reporting throughout the TUI lifecycle.

## [3.3.0] - 2026-09-22

### Added
- **Ollama Error Transparency (WO-007)**:
  - In-stream NDJSON error chunk detection for runtime issues such as Out of Memory (OOM) / VRAM exhaustion during token streaming.
  - HTTP error body extraction and propagation for backend and network failures.
  - Connection failure and timeout banners with actionable diagnostic messaging.
  - Dedicated error alert box rendered directly in conversation chat history.

### Changed
- **TUI Stream & Status Bar Layout (WO-007)**:
  - Relocated Status Bar from the top header to the bottom of the screen.
  - In-viewport streaming lifecycle: composer is cleared and hidden immediately upon prompt submission, allowing streaming tokens to appear directly inside the conversation viewport without visual jumping.
  - Suppressed generic "Turn operation completed." message on failed or cancelled operations.

### Improved
- **Composer Layout Symmetry (WO-008)**:
  - Aligned composer text area width precisely with the conversation viewport, terminating neatly at the vertical divider of the runtime panel.
- **Status Bar Layout Symmetry (WO-009)**:
  - Aligned bottom status bar width with the conversation viewport and composer box, establishing seamless visual symmetry across left and right panels.
- **Panel-Aware Rate-Limited Streaming (WO-011)**:
  - Replaced raw stdout token streaming with rate-limited (100ms / 8–10 Hz) full-frame redraws in the conversation viewport.
  - Constrained in-progress streaming output to conversation column width, avoiding panel spillover.
  - Eliminated status tick and streamed text racing corruption by suppressing ticks during active streaming and routing them through redraw.
- **Busy-State Composer During Active Generation (WO-013)**:
  - Rendered rounded composer container visible throughout token generation with inactive border styling (`#475569`) and busy placeholder (`Generating response... (Ctrl+C to cancel)`).
  - Restores active composer (`Type a message...`, highlighted blue border, active cursor) seamlessly once turn finishes.

### Fixed
- **ANSI Escape Styling in Workspace Layout (WO-012)**:
  - Preserved full ANSI styling (user message backgrounds, assistant model badges, markdown syntax highlighting) across the entire conversation viewport by enabling truecolor capture console in `render_workspace_layout_str`.

## [3.5.0] - 2026-09-26

### Added
- **5-Tier Responsive TUI Layout Engine (WO-019)**:
  - Constraint-based responsive architecture with 5 breakpoints: Very Narrow (<80 cols), Narrow (80-99), Normal (100-139), Wide (140-179), Very Wide (>=180).
  - Single-column full-width modes with compact runtime badges (Very Narrow/Narrow).
  - Proportional 2-column layout with adaptive conversation/runtime panel widths (Normal/Wide).
  - Centered conversation deck with balanced gutters and max-width constraint (Very Wide).
  - Adaptive text handling: middle-truncation for paths (`format_responsive_path`), word-aware soft truncation for titles and status messages.

### Changed
- **Layout Computation (`cli/tui/layout.py`)**:
  - Replaced single binary `NARROW_THRESHOLD=100` with 5-tier `ColumnLayout` tier system.
  - Dynamic `render_full_screen_workspace` adapting composer, status bar, and panel rendering per tier.
- **Testing Coverage**:
  - Comprehensive regression tests for exact row/column bounds across 5 resolutions (70x20, 90x25, 120x30, 160x40, 200x50).
  - Dynamic resize transition tests (120→90, 90→160) verifying no geometry drift or border clipping.

## [Unreleased]
- Initial repository scaffolding.

## [3.5.1] - 2026-09-26

### Fixed
- **Full-stretched TUI Layout Restoration (WO-022)**:
  - Eliminated centered conversation deck side gutters on wide screens, restoring full-stretched 2-column layout.
  - Normalized composer input positioning to column 0 with proper cursor repositioning.
  - Closed box borders and maintained chrome integrity across all resolution breakpoints.
