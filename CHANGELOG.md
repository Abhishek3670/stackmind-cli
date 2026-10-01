# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Live Autonomous Lifecycle & Integration Review Hardening**:
  - Dedicated read-only integration review contract (`WO-005`) binding Claude's review turn strictly to declared deliverables and tests without write permissions.
  - Integration review harness schema validation with structured JSON failure feedback and bounded retries.
  - Supervisor auto-recovery on daemon restart supporting both `Phase.BLOCKED` and `Phase.FAILED` runs with pre-failure phase restoration.
  - GitOps turn retry mechanism in `_advance_gitops` to absorb transient network hiccups before transitioning to `Phase.FAILED`.
  - TUI `:resume` unblocking integration calling `run_resume` to re-drive supervisor runs.
  - Section 7 in `STACKMIND-CLI.md` documenting verified tools and lifecycle test results.

## [3.7.0] - 2026-09-29

### Added
- **Lifecycle Supervisor & Deterministic Multi-Agent Orchestration**:
  - Deterministic state machine (`INIT` → `PLANNING` → `AWAITING_APPROVAL` → `AUTHORING` → `DISPATCHING` → `EXECUTING` → `INTEGRATION_REVIEW` → `PRODUCT_READY` → `GITOPS` → `COMPLETE`).
  - S1 TUI adapter and daemon integration: `:goal` dispatches supervised runs, `:status` renders active run phase, and `:approve`/`:reject` route through supervisor plan resolution.
  - S2 Human approval gate: halts execution in `AWAITING_APPROVAL`, resuming on operator approval or re-planning on rejection with operator feedback.
  - S3 Real deliverable disk verification: checks declared deliverable on disk before marking work orders completed.
  - S4 Blocked turn escalation: transitions to `Phase.BLOCKED` on unrecoverable gate failures without infinite retry loops.
  - S5 Multi-session run isolation: complete separation of run states and daemon storage per session.
  - S6 Real SessionManager concurrent worker dispatch: introduces supervisor batch operations (`parent_operation_id`) allowing sibling worker operations (`WO-002`, `WO-003`) to run concurrently in daemon operation tracking.
  - S7 Governed QA retry loop: automatic retry with structured error feedback up to `max_retries` before transitioning to `Phase.FAILED`.
  - S8 Crash recovery and resumability: recovers in-flight operations upon daemon restart without duplicate turn dispatch.
- **Driver Reliability Hardening**:
  - Dedicated `OperationContentionError` for mutual exclusion collisions in `SessionManager.begin_operation`.
  - Double-bounded contention handling: bounded consecutive contention attempts (`max_contention_retries=50`) and continuous operation timeout (`max_wait_seconds=300`) escalating cleanly to `Phase.BLOCKED`.
- **Narrowed Architecture Default Contract**:
  - Narrowed Claude default contract to minimal legitimate authoring boundary: `PLAN.md`, `.sync/work-orders/**`, `.sync/contracts/**`, and assigned inboxes (`local-llm`, `claude`), with explicit denial of `src/**`, `tests/**`, runtime state, and other agents' inboxes.
- **GitOps Release Provenance Trailers**:
  - Standardized commit message trailers for release commits (`Work-Order`, `Released-By`, `Approved-By`, `Architect`, `Target-Work-Orders`).
- **Import / Dependency Satisfiability Gate**:
  - Fail-closed import satisfiability verification examining agent deliverables using Python AST.
  - Manifest parsing (`pyproject.toml`, `requirements*.txt`) with import-to-distribution name mappings.
  - Detection and explicit diagnostics for undeclared external third-party imports without environment mutation.
- **Phase B: Governed Tool & Context Hardening**:
  - Cumulative and per-turn tool-call limit enforcement with deterministic termination.
  - No-progress and pathological loop detection (identical repetition, ping-pong cycles, signature tracking).
  - Bounded context output truncation and structured recovery warnings before failure limits.
  - Single-turn tool-less graceful finalization upon loop guard trip preserving valid deliverable salvage.
- **No-Op Deliverable Completion Prevention Gate**:
  - Hardened `outcome_verified` verification dimension to require observable deliverable addition or modification in the staged diff.
  - Disentangled harness-owned bookkeeping writes from task staged diffs, preventing vacuous completion without deliverable writeback.
  - Enriched diagnostic blockers when declared deliverables are missing from the turn's staged diff.

## [3.6.0] - 2026-09-28

### Added
- **Governed Multi-Agent Runtime & Tool Loop (WO-026, Phases A & B)**:
  - Connected `AgentRunner` to `ProviderGateway` and `OllamaAdapter` for model tool loop execution (`read_file`, `write_file`).
  - Pre-turn `ScratchWorkspace` instantiation and `ToolGateway` with fail-closed contract boundary checks and path traversal containment.
  - In-memory turn tool loop with budget exhaustion tracking and robust tool call argument parsing and repair.
- **Autonomous Architecture Planning & Turn 1 Synthesizer (Phase C)**:
  - Integrated `:goal` and user request synthesis in `AgentRunner.discover_next_task` for Architecture role (`claude`).
  - Implemented `synthesize_bootstrap_planning` generating structured, actionable `PLAN.md` documents grounded in repository knowledge graph context.
  - Added structural schema validation ensuring required sections (`Objectives`, `Milestones`, `Implementation Scope`, `Risk Analysis`) exist before plan approval.
- **Governed Work Order & Contract Authoring (Phase D)**:
  - Implemented Turn 2 architecture authoring gate enabling `claude` to autonomously author `.sync/work-orders/ACTIVE/<WO-ID>.yaml` and `.sync/contracts/<WO-ID>.yaml`.
  - Added overwrite conflict detection preventing uncoordinated overwrites of existing active work orders and contracts.
  - Implemented advisory file locking (`AcquireAdvisoryLock`) during authoring to enforce concurrency control.
  - Automated asynchronous inbox dispatch notice generation for assigned worker agents.
- **D024 Mandatory QA Gate & Pre-Flight Hardening (Phase E)**:
  - Enforced programmatic gating in `D024Gate` requiring verified approval verdicts before closing work orders.
  - Added companion test file presence verification for code deliverables (`deliverable.type == "code"`).
  - Integrated AST-based hardcoded credential comparison detection (`_InsecureAuthASTVisitor`) in `validators/kernel/security.py` detecting sensitive equality comparisons (`password == "admin"`, dict credential stores) with false-positive suppression for test files.

## [3.5.2] - 2026-09-26

### Added
- **Unified Daemon Discovery & TUI Auto-Attachment (WO-024)**:
  - Automatic daemon discovery in `stackmind tui` (probes port 8765, attaches if running, auto-starts with fallback).
  - Clean detachment on TUI exit (does not terminate pre-existing external daemons).
  - `stackmind daemon status` command reporting online status, PID, port, and active session count.
  - Enhanced `stackmind daemon start` and `stop` with reliable PID tracking and process termination.

## [3.5.1] - 2026-09-26

### Fixed
- **Full-stretched TUI Layout Restoration (WO-022)**:
  - Eliminated centered conversation deck side gutters on wide screens, restoring full-stretched 2-column layout.
  - Normalized composer input positioning to column 0 with proper cursor repositioning.
  - Closed box borders and maintained chrome integrity across all resolution breakpoints.

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
