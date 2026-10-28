# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.10.5] - 2026-10-28

### Added
- **Project Grounding & Plan Context Auto-Injection**:
  - Automatically injects `project_plan_overview` from `PLAN.md` and `brand_reference` from `brand-kit.html` into the compiled task contract block (`TASK CONTRACT JSON`), guaranteeing all specialist workers possess foundational project constraints, brand tokens, and architecture from turn 1.
  - Expanded worker contract task `description` truncation threshold from 1,500 to 2,500 characters and increased `acceptance_criteria` capacity from 10 to 15 items.
  - Enhanced worker workflow instructions to explicitly mandate inspecting `PLAN.md` and `brand-kit.html` alongside `index.html` during initial exploratory turns.
- **Pinned Original User Goal in Transcript Compaction**:
  - Pinned the initial user prompt in the TUI transcript compaction summary (`Original user goal: '...'`), ensuring high-level user directives and requirements are never dropped after multiple compaction cycles.

### Fixed
- **Multi-Turn Tool Output Context Retention**:
  - Relaxed premature tool output truncation in `GovernedToolRuntime` (`_compact_messages_for_context`): doubled recent tool retention window (`keep_recent = 4` for cloud providers) and raised retention cap from 600 to 1,200 characters so multi-file inspections aren't wiped out before implementation.
- **Contract Directory Listing & Resource Folder Scoping**:
  - Refined `list_directory` in `validators/kernel/contract.py` to seamlessly authorize standard workspace resource folders (`assets/`, `public/`, `static/`, `docs/`, `templates/`) and allowed rule ancestors while strictly rejecting out-of-scope directories.

## [3.10.4] - 2026-10-28

### Fixed
- **Deliverable-First QA Defect Ownership Resolution**:
  - Prioritized canonical deliverable ownership via `_reworkable_wo_for_path` over raw regex matches (`WO-\d+`) in review filenames or text bodies in `_find_qa_defect_verdicts`.
  - Prevented LLM misattributions (e.g. QA blaming `WO-006` for defects in `index.html` owned by `WO-002`) from locking developers in contracts where modifying the defective file is forbidden.
  - Enhanced deliverable extraction in `_reworkable_wo_for_path` to resolve paths across both single and plural `deliverable`/`deliverables` definitions and companion contract `scope.allow` modules.
- **Supervisor Operation Contention Resilience**:
  - Handled `OperationContentionError` explicitly in `_handle_qa_evidence_gap`, `_worker_dispatch`, `_integration_rework`, and GitOps dispatch in `validators/kernel/daemon/supervisor.py`.
  - Rolled back retry and attempt counts and yielded `AdvanceResult.WAITING_FOR_OPERATION` when a session is busy with an in-flight operation, preventing temporary session concurrency locks from falsely marking work orders as failed or terminating runs into `Phase.FAILED`.
- **QA Rework Bound Alignment**:
  - Aligned worker rework escalation in `_route_qa_defect_to_developers` with `state.max_qa_reworks`.

## [3.10.3] - 2026-10-28

### Fixed
- **Permission Denial Storm & Specialist Read Scoping**:
  - Authorized all project specialists (`gemini`, `codex`, `gemma`, `local-llm`, etc.) to read workspace project files (HTML, CSS, JS, markdown documentation, project assets, configurations, and directory listings) without spurious permission denials.
  - Restricted read denials exclusively to true security boundaries (`.git/**`, `.env*`, `.sync/runtime/**`, `.sync/snapshots/**`, secrets/tokens/keys) and private inter-agent inboxes.
  - Sanitized contract compilation in `authoring_compiler.py` to prevent workspace deliverables (e.g. `index.html`) from being placed into `scope.deny`.
- **Global Turn Budget Depletion Loop Guard**:
  - Fixed turn budget deduction in `validators/kernel/daemon/supervisor.py`: budget is now only decremented after `start_turn` successfully instantiates an operation, preventing contention exceptions from burning through turn budgets in rapid loops.
  - Added single-turn in-flight operation contention guards in `_advance_executing` to prevent concurrent turn dispatch collisions when running in single-operation mode.
- **Daemon Boot Recovery & In-Memory Attestation Resilience**:
  - Fixed workspace root resolution in `manager.py` during boot recovery when daemon storage is nested in test or custom directories.
  - Guarded in-memory run attestation in `supervisor.advance()` so in-memory run states without prior disk checkpoints are registered seamlessly during execution.

## [3.10.2] - 2026-10-28

### Added
- **Work Order Schema `cancellation_reason` Support**:
  - Added `"cancellation_reason"` property to `schemas/work-order.schema.json` to natively support cancellation and supersession audit reasons written by authoring reconciliation.
- **Robust Work Order Shorthand Normalization**:
  - Enhanced `AuthoringGate.normalize_work_order_data` to automatically coerce LLM variations for `test_plan` (strings or lists of strings coerced to canonical `{"sources": [...], "tests": [...]}` mappings), `acceptance_criteria` (single strings to lists), `priority_override` shorthand, and `implementation_estimate` numeric fields.
- **Regression Test Suite Expansion**:
  - Added test coverage in `tests/test_blocked_orchestration_honesty_and_asset_code_gate.py` verifying `cancellation_reason` schema acceptance, compatible status handling in `work-orders/BLOCKED/`, historical contract retention for blocked orders, and authoring revision auto-bumping.

### Fixed
- **Status Protocol Synchronization for Archived/Cancelled Work Orders**:
  - Fixed `INDEX.yaml` synchronization in `validators/harness/runner.py` and `validators/kernel/daemon/supervisor.py` to preserve the work order's actual status (`CANCELLED` or `BLOCKED`) rather than overwriting `status: "BLOCKED"` in `INDEX.yaml`.
  - Updated `cli/validate.py` (`_validate_work_orders_against_index`) to recognize `CANCELLED`, `FAILED`, and `BLOCKED` as compatible non-executable states in the `work-orders/BLOCKED/` directory, preventing false protocol desync errors from blocking staged validation during planning turns.
- **Authoring Readiness Orphan Contract False Positives**:
  - Updated `validate_authoring_readiness` in `validators/harness/authoring_readiness.py` to recognize contracts belonging to work orders in `work-orders/BLOCKED/` and `work-orders/COMPLETED/`, eliminating bogus `ORPHAN_CONTRACT` errors on archived historical tasks.
- **AuthoringGate Overwrite Safeguard Auto-Bumping in All Active Supervisor Phases**:
  - Extended the repair/authoring safeguard in `validators/harness/authoring_gate.py` to cover all active orchestrator phases (`PLANNING`, `AWAITING_APPROVAL`, `AUTHORING`, `AUTHORING_READINESS_GATE`, `ARCHITECT_REPAIR`, `ARCHITECT_RECOVERY_DECISION`, `BLOCKED`), ensuring authorized authors (`claude`, `architecture`, `ceo`) automatically bump revisions with audit amendment records rather than being rejected with `Overwrite conflict` when refining tasks.

## [3.10.1] - 2026-10-28

### Added
- **Architect Situational Awareness & Orchestration State Grounding**:
  - Injected live supervisor run phase, blockers, and error telemetry (`run-*.yaml`) directly into `render_team_status` in `validators/harness/task_contract.py`.
  - Added explicit instructions directing the Lead Architect to provide completely honest, diagnostic responses when the user asks "what happened", "is it working", or for run status, strictly prohibiting hallucinating that "the orchestration is working" or "nothing broke" when the supervisor is in `BLOCKED` or `FAILED` phase.
  - Automatically halted specialist agent display statuses in `render_team_status` when the supervisor orchestration is blocked, preventing false reports of active specialist execution.
- **Robust Authoring Overwrite Safeguard for Active Runs**:
  - Updated `AuthoringGate` in `validators/harness/authoring_gate.py` to recognize active and blocked supervisor run states, allowing authorized authors (`claude`, `architecture`, `ceo`) to repair active work orders and contracts without triggering `Overwrite conflict` rejections.
  - Automatically bumps `revision` and `contract_revision` with audit amendment records during repair turns.
- **Automated Regression Suite for Orchestration Honesty & Readiness**:
  - Added `tests/test_blocked_orchestration_honesty_and_asset_code_gate.py` verifying asset exclusion, truthful status reporting, and repair turn gating.

### Fixed
- **`CANCELLED` Work Order Status Validation in `ACTIVE` & `BLOCKED`**:
  - Updated `WORK_ORDER_ALLOWED_STATUSES` and `_normalize_work_order_status` in `cli/validate.py` to allow `"CANCELLED"` (and `"CANCELED"`) work orders in `ACTIVE/` and `BLOCKED/` directories.
  - Eliminated staged validation rejection errors that previously crashed Architect repair turns.
- **Milestone Component Deliverable Deduplication**:
  - Fixed Pass 2 of `reconcile_active_work_orders_with_plan` in `validators/kernel/daemon/authoring.py`: Only deduplicates work orders sharing the exact same deliverable path or identical title within a milestone, preserving distinct component tasks (`package.json`, `index.html`, `styles.css`, `main.js`, `tokens.css`).
  - Added automatic reactivation of approved tasks in `PLAN.md` that were previously marked cancelled by deduplication bugs.
- **Asset and Directory Classification in Companion Test Gate**:
  - Updated `_is_code_deliverable` in `validators/harness/authoring_readiness.py`: Assets (`.png`, `.svg`, `.ico`), styles (`.css`), directories (paths ending in `/` or extensionless), and documents are explicitly excluded from code deliverable companion test requirements, eliminating false `TEST_COVERAGE_UNPLANNED` readiness failures on image/asset work orders.
- **Automated QA Deliverable Normalization**:
  - Guaranteed in `validators/kernel/daemon/authoring.py` that QA work orders delivering documentation (`.md` or `type: doc`) are automatically normalized to executable test suites (`tests/test_qa_verification.py` or `tests/verify_landing.py`), satisfying D024 gate requirements.
- **Contract Synthesis & Scope Authorization**:
  - Pass 3c in `authoring.py` automatically synthesizes missing contracts and ensures existing contracts allow the deliverable path and test suites.
- **TUI Cancelled Work Order Status Rendering**:
  - Fixed `cli/tui/state.py` so cancelled work orders with cancellation reasons are not mistakenly flipped to `BLOCKED` with exclamation mark `!`, rendering cleanly as `⊘` (cancelled).
  - Enhanced `CURRENT OPERATION: Blocked` panel in `cli/tui/runtime_panel.py` with `Run :resume to retry` guidance.

### Added
- **Autonomous Multi-Agent Lifecycle & Supervisor Driver Integration**:
  - Automatically attaches and spins up a background supervisor driver (`_drive_run`) upon operator plan approval in chat, seamlessly transitioning from plan proposal directly into autonomous multi-agent execution (`PLANNING → AUTHORING → DISPATCHING → EXECUTING`) without manual intervention.
  - Implemented authoring task synthesizer fallback in `validators/harness/runner.py` for headless authoring turns when `work_order_id` (e.g. `WO-000`) is not pre-materialized on disk.
  - Guarded non-report operation tracking with on-disk existence checks, preventing crashes on newly synthesized work orders.
- **Authoring Readiness Compiler Companion Test Auto-Injection**:
  - Automatic companion test planning injection in `validators/harness/authoring_compiler.py`: When a project has zero planned test artifacts (e.g. single-milestone builds or frontend landing sites), the compiler automatically injects deterministic companion test planning (`tests/test_{stem}.py`) into `test_plan` for uncovered code deliverables.
  - Automatically updates `implementation_estimate.expected_files` and contract `scope.allow` to include `tests/**`, eliminating `TEST_COVERAGE_UNPLANNED` readiness failures on frontend deliverables like `index.html`.
  - Enabled active work order compilation for work orders currently in `BLOCKED` status undergoing repair.
- **AuthoringGate Smart Overwrite Safeguard & Auto-Bumping**:
  - Resolved overwrite-conflict catch-22 in `validators/harness/authoring_gate.py`: Authorized authors (`claude`, `architecture`, `ceo`) can now repair active work orders and contracts in repair or blocked mode without triggering `Overwrite conflict`.
  - Automatically increments revisions (`revision = existing_rev + 1`, `contract_revision = existing_rev + 1`) and appends audit amendment records if the model does not supply a higher revision number during repair.
- **Governed Architect Repair Guidance & Schema Templates**:
  - In `validators/kernel/daemon/supervisor.py`, `_enter_architect_repair` now monotonically increments `state.authoring_revision += 1` on each repair attempt.
  - Injected explicit, copy-pasteable schema templates for `test_plan`, `implementation_estimate`, and contract `budget` into `_authoring_repair_prompt`, eliminating guesswork and schema violations during repair turns.
- **Child Work Order Collision Preservation & Plan Reconciliation**:
  - Fixed renumbering bug in `validators/kernel/daemon/authoring.py`: Preserved `proposed_wos = renumbered_wos` during child work order synthesis, preventing ID collisions and duplicate work order sets (`WO-001..WO-004` alongside `WO-005..WO-008`).
  - Added plan reconciliation to `SessionManager.approve_plan` in `validators/kernel/daemon/manager.py` to automatically cancel and clean up orphaned active work orders when a new plan is approved.
- **TUI State Optimistic Worker Desync Fix**:
  - Fixed worker status rendering in `cli/tui/state.py`: Worker agent statuses (`Backend`, `Frontend`, `QA`, `GitOps`) now remain `WAITING` until operations actually start, eliminating misleading optimistic `implementing` status before dispatch.
- **TUI Mouse Access, Scrollbar Navigation & Native Clipboard Integration**:
  - Native zero-dependency cross-platform clipboard utility (`cli/tui/clipboard.py`) leveraging Win32 `ctypes` (`OpenClipboard`, `GetClipboardData`, `SetClipboardData`), macOS `pbcopy`/`pbpaste`, and Linux `wl-clipboard`/`xclip`/`xsel`.
  - Full keyboard clipboard paste support via `Ctrl+V` (`\x16` or VK `0x56`) and `Shift+Insert` in `RawLineEditor`.
  - Right-click mouse paste: right-clicking anywhere in the input line or terminal viewport immediately pastes OS clipboard text into the active composer.
  - Interactive mouse scrollbar navigation: clicking on the conversation viewport's scrollbar track jumps or smoothly pages up/down, keeping multiline conversations easily navigable without leaving the mouse.
  - Multi-line clipboard paste: preserves CRLF / LF line endings cleanly into composer buffer.
  - System clipboard commands (`:copy`, `:yank`, `:clipboard`, `:copy all`): copies latest assistant response or full conversation transcript to the OS clipboard with character confirmation.
- **Multi-Modal Document, Image & Archive Ingestion Engine (Pillar 4)**:
  - New `validators/ingest` subsystem (`models.py`, `parsers.py`, `store.py`) enabling attachment ingestion and prompt grounding across documents, images, and archives.
  - Zero-dependency document extraction: parses Word `.docx` documents (paragraphs, headings, Markdown tables via stdlib `xml.etree.ElementTree` and `zipfile`), PDFs (stream object text extraction), CSVs (Markdown tables), and multi-encoding plain text / Markdown.
  - Image asset inspection: extracts width, height, aspect ratio, color mode, and SHA-256 for `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`, and produces structured grounding metadata.
  - Secure archive extraction: decompresses `.zip`, `.tar.gz`, `.tgz` with strict Zip Slip path traversal protection (rejecting `..` and escaped target paths) and maximum uncompressed byte bounds.
  - Workspace attachment persistence: manages raw immutable files under `.sync/attachments/raw/`, extracted markdown under `.sync/attachments/extracted/`, and registry index in `manifest.json`.
  - Context grounding injection: automatically injects verified user attachments into KnowledgeAPI `assemble_context` for both Architecture (`WO-000`) planning and Worker execution.
  - TUI commands: `:attach <path>` / `:upload <path>` (ingest and index), `:attachments` / `:files` (list tracked assets), and `:attachment <id>` (view details/preview).
  - CLI commands: `stackmind attach <path> [--workspace PATH] [--tag TAG]` and `stackmind attachments`.
  - Full test coverage with 7 automated unit tests in `tests/test_ingest_attachments.py`.

### Fixed
- **Win32 `Ctrl+O` Thinking Toggle and Control Key Parsing**:
  - Resolved an issue in `cli/tui/keyboard.py` where Windows console / Windows Terminal sends `UnicodeChar == '\x00'` when `Ctrl` is held, causing `Win32ConsoleReader.parse_input_record` to drop `Ctrl+O` (`vk=0x4F`), `Ctrl+K` (`vk=0x4B`), and `Ctrl+L` (`vk=0x4C`).
  - Added dedicated `is_ctrl` virtual key code resolution for `Ctrl+O`, `Ctrl+K`, `Ctrl+L`, `Ctrl+C`, `Ctrl+D`, and `Ctrl+V`.
- **Smooth Mouse Scrollbar Dragging**:
  - Updated terminal mouse reporting sequences to enable SGR Mode 1002 (`\x1b[?1000h\x1b[?1002h\x1b[?1006h`), enabling continuous button-motion tracking during mouse drags.
  - Added support for button code `32` (left drag) in `_handle_mouse_click`, mapping the thumb's proportional position directly across the scrollbar travel height to smoothly drag the conversation viewport.
- **Native Mouse Text Selection & Clipboard Copy**:
  - Direct native click-and-drag text selection is enabled by default without requiring `Shift`, leaving terminal mouse management to the native console / terminal emulator.
  - Removed `:mouse` command and removed the temporary "Hold Shift+Drag to select & copy text (or run :copy)" tooltip hint for an uncluttered terminal experience.
  - Retained `:copy` (for latest assistant response) and `:copy all` (for full conversation transcript) for instant zero-dependency OS clipboard copying.
  - Updated `:help` menu and shortcuts documentation to cleanly reflect `Ctrl+O`, `Ctrl+C`, `Ctrl+V`, and `:copy [all]`.
- **Provider vs. Role Single-Responsibility Separation & Mismatch Fix**:
  - Enforced strict separation of concerns between `:provider` and `:roles`:
    - **`:provider`** (with alias `:providers`) is the ONLY surface for configuring provider definitions (endpoints, base URLs, API keys, status enabled/disabled, default models, and connection health checks). Purged duplicate role menus and role prompts from the provider configuration surface.
    - **`:roles`** (with alias `:role`) is the ONLY surface for assigning providers and models to agent roles (`Architecture`, `Backend`, `Frontend`, `Q/A`, `GitOps`).
    - Unified `:models` into `:provider` and removed duplicate `:models` command from autocompletion and help.
    - Updated `:rebind` deprecation notices to cleanly distinguish `:roles` for role assignments from `:provider` for provider definitions.
  - Fixed role persistence and daemon synchronization:
    - Resolved an issue where pressing `[Esc]` in `:roles` exited without persisting role changes to `settings.json` or syncing with the daemon.
    - Added immediate auto-persistence to disk and real-time daemon synchronization (`client.settings_update`) whenever role providers or models are cycled, updating both the `AGENTS` sidebar and runtime state in real-time.
    - Updated `SessionManager._default_runner` in `validators/kernel/daemon/manager.py` to prioritize workspace role assignments from `settings.json` over in-memory defaults, eliminating provider/role mismatches where turns were routed to stale providers.
- **Provider View Navigation Cleanup**:
  - Removed confusing `◄ Return to StackMind Settings (:config)` row from the `:provider` view in `cli/tui/settings_panel.py`.
  - `:provider` now cleanly presents only provider definitions and the direct `Save Settings to Disk & Apply to Runtime` action, while `[Esc]` cleanly exits and saves (or returns to `:config` if the user originally navigated from `:config`).

### Deprecated
- **TUI `:rebind` Colon Command**:
  - The `:rebind <role> <backend> [model]` colon command is formally deprecated and scheduled for removal in a future major release.
  - Interactive role binding is now fully consolidated under `:roles` (for visual role assignment across providers and models) and `:provider` (for endpoint and credential management).
  - Executing `:rebind` now surfaces structured `[DEPRECATED]` notices and redirection guidance, helping users avoid positional syntax errors and active-turn rebinding lock conflicts.

## [3.9.0] - 2026-10-17

### Added
- **Multi-Engine Image Search & Media Asset Extraction**:
  - `IMAGE_SEARCH = "image_search"` registered in `OperationType` and permitted across `claude`, `codex`, and `gemini` agent roles in `validators/kernel/identity.py`.
  - DuckDuckGo Images API integration (`search_duckduckgo_images`) with automated session `vqd` token acquisition and query parsing.
  - Wikimedia Commons image search (`search_wikimedia_images`) querying Wikimedia Action APIs for public domain and Creative Commons media assets with author and license attribution metadata.
  - SerpAPI Google Images mode (`engine="google_images"`, `tbm="isch"`) in `search_serpapi`.
  - Integrated `perform_image_search` and `perform_web_search(media_type="images")` routing queries to DuckDuckGo, Wikimedia, or SerpAPI based on provider configuration.
  - HTML media asset extraction in `perform_web_fetch` (`_HTMLToTextExtractor`): parses `<img>` tags (`src`, `srcset`, `alt`, `title`), resolves relative paths to absolute URLs using page `base_url`, and synthesizes a dedicated `## Discovered Images & Media Assets:` reference block.
  - `ToolGateway.image_search` and `ToolGateway.web_fetch(extract_images=True)` exposed with tool call routing in `ProviderGateway`.
- **Deliverable Quality Assurance & Media Integrity Gate**:
  - HTML media asset integrity verification in `DeliverableVerifier` (`_check_html_media_assets`).
  - Strict detection of broken media syntax: flags empty or missing `src`, placeholder strings, search and gallery landing pages incorrectly used as raw image URLs (e.g. Unsplash photo search URLs), and missing local relative assets.
  - Non-blocking remote image reachability probe (HTTP HEAD requests) detecting 404s, 403s, and non-image HTML responses.
  - Integrated deliverable verification directly into `ToolGateway.verify_deliverable` to ensure agent-produced deliverables conform to QA integrity standards before finalization.
  - Comprehensive unit test coverage across web search, image extraction, and deliverable QA gates (`test_tools_web_research.py`, `test_deliverable_verifier.py`, `test_tools_qa_verification.py`).

## [3.8.0] - 2026-10-13

### Added
- **Tolerant Contract Shorthand Normalization & Autonomous Recovery Resilience**:
  - `schemas/contract.schema.json` relaxed to accept float/number versions (`schema_version`, `contract_revision`), string rule lists for `scope.allow` and `scope.deny`, and write target arrays for `scope.write`.
  - `AuthoringGate.normalize_contract_data` automatically coerces LLM shorthand into canonical schema dicts (`[{"module": ...}]`, `"read-write"`) and attaches `normalized_content` to `AuthoringGateDecision`.
  - `ToolGateway.write_file` and `apply_patch` write canonicalized YAML to disk, guaranteeing consistent structured format for all subsequent validation gates and runner turns.
  - Transparent bundled schema fallback in `ToolGateway.read_file` when reading `schemas/**` or `.sync/schemas/**` without a local workspace copy.
  - Schema mirroring in `cli/init.py#ensure_workspace_protocol_scaffolding` to ensure bundled schemas exist in `<workspace>/schemas/`.
  - Actionable error feedback in provider gateway for authoring validation failures, eliminating false "out of scope" warnings.
  - Multi-layer tolerance in `ContractNormalizer.normalize` and `AgentContract.is_node_in_scope` for list `write` modes and string-shaped rules.
  - Explicit schema structure guidance in `LifecycleSupervisor._recovery_repair_prompt`.
- **Procedural Skills & Execution Profiles System**:
  - Multi-tier execution profiles (`lite`, `medium`, `full`, `ultra`) tuning skill injection budgets and tool latency.
  - Automated skill consumer, store, and selector for dynamic runtime procedural knowledge injection.
  - Architectural skill selection and runbook audit tools.
- **Central Home & Projects Workspace Management**:
  - Multi-project registry management in `~/.stackmindcli/` via `cli/projects.py` and `validators/kernel/home.py`.
- **GitOps & Security Scan Autonomous Recovery**:
  - Authorized `run_security_scan` across root workspace directories without false contract denials.
  - Bounded GitOps recovery retries with structured feedback in `_advance_gitops`.
  - Allowlisted standard container credentials (`postgres`, `localhost`, `root`) in security credential leak detector.
- **TUI Visual Fidelity & Streaming Robustness**:
  - `:skills` and `:skill` commands for exploring registered procedural skills.
  - Robust healing of invalid JSON escapes and unwrap decision blocks to markdown in stream turns.
  - Dropdown line clearing on shrunk command matches and stream termination on autonomous delivery completion.
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
