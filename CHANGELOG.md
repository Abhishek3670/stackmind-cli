# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

## [Unreleased]
- Initial repository scaffolding.
