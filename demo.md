# 🚀 StackMind CLI & TUI v3.3.0 GA — Multi-Role Autonomous Engineering Delivery

Welcome to **StackMind v3.3.0 GA**, an **OpenCode-style client control plane (P6)** and **autonomous multi-role engineering delivery runtime (P7)**.

StackMind enables full-stack software development driven by specialized, contract-governed AI agent roles (`claude`, `codex`, `gemini`, `gemma`, `local-llm`), monitored live through an interactive Terminal UI control plane (`stackmind tui`), backed by Code-Graph Intelligence (KNOW-01), Verified Procedural Learning (LEARN-01), zero-leakage security, and deterministic human-in-the-loop plan approvals.

---

## 🏛️ Architecture Overview & The Single-Source Rule

StackMind enforces the non-negotiable **Single-Source Rule**:
> **There is exactly one authoritative execution path in StackMind: the `AgentRunner` Harness.**  
> Under no circumstances may a daemon, TUI, orchestrator, or autonomous sub-system introduce an alternative execution engine, direct LLM provider loop, or un-governed tool execution path. All prompt turns, tool invocations, code modifications, and verifications strictly traverse the `AgentRunner` pipeline.

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                   Terminal UI Control Plane (stackmind tui)              │
│   Phase Banner │ Agent Roles Panel │ Work Orders │ Operation Tree │ Feed │
│   Interactive REPL: :status, :roles, :wo, :tree, :rebind, :diff, :matrix │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ HTTP JSON-RPC 2.0 (:8765/rpc, SSE /events)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                LocalDaemon & SessionManager (validators/kernel/daemon)   │
│   Hierarchical Operation Tree │ Role Normalization │ State Recovery      │
│   Durable State: workspace/.sync/runtime/daemon/daemon-state.json        │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ Governed Delegation
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│               AgentRunner Harness (validators/harness/runner.py)         │
│  ├── CONTRACT-01 Scope Enforcement (Glob containment & deny boundary)   │
│  ├── KNOW-01 Knowledge API & Ranked Context Bundles                     │
│  ├── 10 Cooperative Cancellation Checkpoints (cancellation_event)        │
│  ├── Isolated Workspace Copy Staging (tempfile.TemporaryDirectory)      │
│  ├── Authentic 6D Verification Gate (Scope, State, AST, Behavioral,      │
│  │   Security, Outcome) — Fail-closed Live Write-back                    │
│  ├── D025 Destructive Safeguards (Backups, Git cleanliness, CEO approval)│
│  └── Credential Zero-Leakage & Terminal Sanitization Guard               │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ ExecutionBackend Protocol
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              BackendRegistry (validators/harness/backend.py)             │
│  ├── Thread-safe RLock synchronization & defensive snapshot iteration   │
│  ├── ModelExecutionBackend (Ollama HTTP /api/generate with 300s timeout)│
│  ├── Typed fault classification (Unavailable, Timeout, Execution errors) │
│  └── Dynamic live role rebinding (:rebind role backend [model])          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 👥 The Governed Team Roster

| Agent Role | Title | Allowed Responsibility | Scope Boundary |
|---|---|---|---|
| **Claude** | Senior Architect | Architecture, Work Orders, Contracts, Plan Approvals | `.sync/work-orders/`, `.sync/contracts/`, Plan Management. **No direct source edits.** |
| **Codex** | Backend Lead | APIs, Databases, Microservices, Python/Go/Rust backend code | Application Backend (`app/`, `api/`, `services/`, `tests/`) |
| **Gemini** | Frontend Lead | UI/UX, Web Views, Flutter, React, Client-side state | Application Frontend (`src/`, `components/`, `ui/`) |
| **Gemma** | QA Lead | Quality Gates, Secret Scans, Contract Audits, Test Suites | Audit & Validation (`tests/`, Verification gates) |
| **Local-LLM** | GitOps & Release Lead | Versioning, CHANGELOG, Release Cuts, Hygiene | Release Metadata (`VERSION.md`, `pyproject.toml`, `CHANGELOG.md`) |

---

## 🛠️ Step 1: Initialize Project & Knowledge Store

Initialize StackMind in any workspace to generate governance contracts, runtime snapshots, and build the deterministic Knowledge Graph:

```powershell
# 1. Initialize StackMind in your workspace
python -m cli.main init .
# Or via CLI binary:
stackmind init .

# 2. Build the deterministic Code-Graph Intelligence store
stackmind graph build -p .

# 3. Verify runtime health across all 5 validation layers
stackmind validate .
```

Expected output:
```text
[PASS] Schema validation
[PASS] Structure validation
[PASS] Protocol compliance
[PASS] Boot integrity
[PASS] Knowledge validation

Runtime is healthy.
```

---

## 💻 Step 2: Start the StackMind Daemon & Launch TUI Control Plane

StackMind runs a local background daemon communicating over HTTP JSON-RPC 2.0 with the interactive terminal UI:

```powershell
# Start the Local Runtime Daemon in the background (default port: 8765)
stackmind daemon start

# Verify daemon health and active sessions
stackmind daemon status

# Launch the redesigned OpenCode-style Terminal Control Plane
stackmind tui
# Or directly via Python module:
python -m cli.main tui

# Optional flags:
# stackmind tui --demo                      # Run simulated multi-agent delivery session
# stackmind tui --daemon-url http://127.0.0.1:8765  # Target a custom daemon endpoint
# stackmind tui --agent codex               # Bind active conversation directly to a role
```

---

## 🖥️ TUI Architecture & Visual Layout

The StackMind TUI provides a distraction-free, chat-first experience inspired by OpenCode while exposing real-time autonomous delivery telemetry:

```text
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│  ✦ StackMind v3.3.0                                                                                            │
│  The AI-native execution kernel for autonomous engineering delivery                                             │
│  Code-Graph Intelligence (KNOW-01)  │  Procedural Learning (LEARN-01)  │  Governed Runtime (HARNESS-01)             │
│  "Verify First. Deliver Authentically. Never Hallucinate Progress."                                            │
│  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────  │
│         Status:  ● online                                                                                       │
│        Session:  s_9f3b12ce (claude)                                                                            │
│        Project:  .../stackmind-cli                                                                              │
├───────────────────────────────────────────────────────────────────┬─────────────────────────────────────────────┤
│  CONVERSATION AREA (70–75% width)                                 │  STACKMIND RUNTIME (25–30% width)           │
│                                                                   │                                             │
│  You                                                              │  AGENTS                                     │
│  Build a WebSocket notification service with unit tests           │  ├─ ○ claude [Senior Architect] idle        │
│                                                                   │  ├─ ● codex [Backend Lead] running (2m 14s) │
│  ✦ StackMind                                                      │  └─ ○ gemma [QA Lead] idle                  │
│  Thinking... (4.2s) ✓                                             │                                             │
│                                                                   │  WORK ORDERS                                │
│  ▸ Actions · 3 completed                                          │  ├─ ✓ WO-024 (Setup WS models)              │
│                                                                   │  ├─ ▶ WO-025 (WebSocket Service)           │
│  I'll implement the WebSocket notification service per the active │  └─ ○ WO-026 (Unit Test Suite)              │
│  architecture plan. Below is the staged implementation:           │                                             │
│                                                                   │  CURRENT OPERATION                          │
│  ```python                                                        │  ID: op_88a4c1                              │
│  # app/notifications/ws.py                                        │  Role: codex                                │
│  class NotificationChannel:                                       │  Step: Writing app/notifications/ws.py      │
│      async def broadcast(self, event: Event) -> None: ...         │  Tokens: 1,842 │ Duration: 12.4s            │
│  ```                                                              │                                             │
├───────────────────────────────────────────────────────────────────┴─────────────────────────────────────────────┤
│ ╭─ Type a message... ──────────────────────────────────────────────────────── Ctrl+K commands | Ctrl+L clear ─╮ │
│ │ >                                                                                                           │ │
│ ╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯ │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 1. Branded Landing Card
- **Header & Motto**: Features the official `✦ StackMind v3.3.0` brand banner, architectural pillars, and verified execution motto.
- **Strict Metadata Order**: Status (`● online` / `○ reconnecting`) → Session ID → Project directory.
- **Path Abbreviation**: Long repository paths are automatically abbreviated visually (e.g. `.../stackmind-cli`) to prevent awkward line wraps while keeping essential telemetry readable.

### 2. Responsive Two-Column Layout
- **Left Column — Conversation Viewport (70–75% width)**:
  - Quiet, unboxed `You` user message blocks.
  - Subtle `✦ StackMind` assistant identity with breathing room.
  - **Genuine Provider Reasoning**: Upstream thinking is displayed as `Thinking... (N.Ns) ✓` and automatically strips internal `<think>` delimiters. Synthetic or fake loops are strictly forbidden.
  - **Turn Actions Disclosure**: Tool calls, file inspections, and AST validations are grouped into a compact disclosure block (`▸ Actions · N completed`). Clicking the header or running `:actions` expands full step-by-step telemetry.
  - **Syntax Highlighting & Inline Diffs**: Code snippets render with the Monokai palette; staged diffs render with explicit additions (`+` green) and deletions (`-` red).
- **Right Column — Persistent Runtime Panel (25–30% width)**:
  - Dynamic agent roster tree (`AGENTS`) showing live status icons and active execution backends.
  - Live Work Order queue (`WORK ORDERS`) showing completed, in-progress, and queued items.
  - Authoritative telemetry (`CURRENT OPERATION`) displaying active node IDs, token consumption, and latency.
- **Narrow Terminal Fallback (< 100 columns)**:
  - On terminals narrower than 100 columns (e.g. standard 80x24), the right-side runtime panel is gracefully suppressed, giving 100% width to conversation and the composer.

### 3. Pinned Bottom Composer
- **Visual Hierarchy**: Pinned at the very bottom with the strongest border in the interface (`#475569` idle, highlighted to `#60a5fa` when focused/typing).
- **Multiline Upward Expansion**: Automatically expands upwards as you type multiline prompts (`Shift+Enter` or paste).
- **Non-Blocking Operation**: You can prepare your next prompt or query colon commands even while background agents are executing.
- **Focus Preservation**: Completed background tasks restore focus smoothly without stealing or clearing your active input buffer.

---

## 🧭 How to Navigate the TUI

StackMind TUI is built for high-velocity keyboard navigation with comprehensive interactive colon commands:

### ⌨️ Global Keyboard Shortcuts

| Shortcut | Action | Description |
|---|---|---|
| `Ctrl+K` | **Command Palette** | Open quick command help and shortcut guide |
| `Ctrl+L` | **Clear Screen** | Redraw screen and clear visible viewport without resetting session |
| `Ctrl+C` | **Cooperative Cancel** | Cancel the currently running agent operation safely via daemon RPC |
| `Shift+Enter` | **Multiline Input** | Insert a newline in the composer without submitting |
| `Up` / `Down` | **Input History** | Cycle through previously submitted prompts and commands |
| `PageUp` / `PageDown` | **Scroll Viewport** | Scroll conversation history up or down |
| `Home` / `End` | **Jump Viewport** | Jump directly to the top or bottom of the conversation |
| `q` | **Quick Quit** | Exit TUI when composer is empty |

### 🖱️ Mouse Navigation
- **Actions Disclosure Toggle**: Click directly on the `▸ Actions · N completed` header line with your mouse to toggle between collapsed summary and expanded telemetry.

### 📜 Scroll Ergonomics & New Activity Indicator
- **Auto-Follow Mode**: When your viewport is at the bottom, conversation automatically scrolls down to follow new tokens, agent responses, and tool events.
- **Viewport Lock**: When you scroll up to inspect previous responses or diffs, auto-scroll is paused so your view doesn't jump.
- **`↓ New activity` Indicator**: If new messages arrive while you are scrolled up, a high-visibility badge appears. Scrolling back to the bottom automatically dismisses the badge and re-engages live follow.

---

## 📋 Comprehensive Colon Commands Reference

Type `:` in the composer to enter command mode:

| Command | Aliases | Category | Description |
|---|---|---|---|
| `:help` | `:?`, `Ctrl+K` | General | Show interactive command guide and keyboard shortcuts |
| `:status` | `:s` | Telemetry | Display overall delivery phase, session metadata, and connection health |
| `:landing` | | View | Re-render the branded landing card at the top of the conversation |
| `:roles` | `:agents`, `:a` | Runtime | View real-time agent status tree (`idle`, `running`, `completed`) and model bindings |
| `:wo` | `:workorders`, `:w` | Runtime | Inspect active, pending, and completed Work Orders |
| `:tree` | | Runtime | Inspect hierarchical parent-child operation tree (`Session` → `Plan` → `Task`) |
| `:actions` | | Chat | Toggle expansion of the most recent turn's tool action disclosure group |
| `:diff` | `:d` | Inspection | View real-time unified diffs for staged files in the staging workspace |
| `:matrix` | `:m` | Governance | Inspect authentic 6-dimensional verification flags (`scope`, `state`, `code`, `behavior`, `security`, `outcome`) |
| `:plan` | `:p` | Governance | View the current architectural plan proposed by Senior Architect (`claude`) |
| `:approve` | `Ctrl+A` | HITL | Formally approve the proposed architecture plan to commence worker dispatch |
| `:reject` | `Ctrl+R` | HITL | Reject the proposed plan with feedback and trigger revision loop |
| `:rebind <role> <backend> [model]` | | Runtime | Dynamically switch backend model for a role (e.g. `:rebind codex ollama qwen2.5-coder:14b`) |
| `:reconnect` | | Network | Re-synchronize session state and recover missing events past `last_sequence` |
| `:events` | `:e` | Debug | Stream raw sequenced events from the daemon journal |
| `:clear` | `:cls`, `Ctrl+L` | View | Clear conversation viewport while preserving active runtime session |
| `:quit` | `:exit`, `:q`, `q` | General | Exit the TUI cleanly, restoring terminal cursor and ANSI modes |

---

## 🔄 Step 3: End-to-End Autonomous Engineering Delivery Walkthrough

### 1. Submit an Engineering Goal
In the composer box, enter your prompt:
```text
> Build a secure real-time notification service with WebSocket backend and unit tests
```
The prompt appears immediately under `You`, the composer border highlights to `#60a5fa`, and the assistant begins processing.

### 2. Observe Upstream Reasoning & Tool Actions
- StackMind displays `Thinking... (3.8s) ✓` as the architect analyzes the codebase.
- As the agent runs graph searches, an actions card appears:
  ```text
  ▸ Actions · 4 completed
  ```
- Click the card or type `:actions` to unfold individual operations:
  ```text
  ▾ Actions · 4 completed
    ✓ Query Knowledge Graph for existing notification routes (0.3s)
    ✓ Check WebSocket protocol conventions in validators/kernel/ (0.5s)
    ✓ Formulate contract scope boundary app/notifications/ (0.2s)
    ✓ Author WO-025 and Contract WO-025.yaml (0.8s)
  ```

### 3. Review & Approve the Architecture Plan (HITL)
When Claude formulates the plan, the TUI presents the Plan Approval Surface:
- Review the proposed files (`app/notifications/ws.py`, `tests/test_notifications.py`).
- Check the assigned agent (`codex`) and budget limits.
- Type **`:approve`** (or press `Ctrl+A`) to approve, or **`:reject`** to request adjustments.

### 4. Monitor Live Worker Execution in the Runtime Panel
Once approved, watch the right-side **StackMind Runtime** panel update dynamically:
- **`codex`** transitions to `● running`.
- **`CURRENT OPERATION`** tracks the active step: `Writing app/notifications/ws.py`.
- **`WORK ORDERS`** marks `WO-025` as in-progress.

### 5. Inspect Real-Time Diffs & 6D Verification
- Type **`:diff`** to inspect staged changes before they are finalized.
- When implementation finishes, the **6D Verification Gate (HARNESS-01)** validates all six dimensions:
  - `scope_verified`: All modified files lie strictly inside `app/notifications/` and `tests/`.
  - `code_verified`: AST parsing clean, pytest suite passes 100%.
  - `security_verified`: Zero leaked credentials or forbidden path traversals.
- Type **`:matrix`** to view the full verification receipt.

### 6. Clean Exit & Session Resumption
- Type **`:quit`** or press `Ctrl+C` when done.
- The TUI cleanly restores cursor visibility and terminal ANSI attributes via `restore_terminal_state()`.
- The background daemon keeps session state intact. Launching `stackmind tui` again will immediately reconnect and recover your session seamlessly.

---

## 💡 Key CLI Commands Quick Reference

```powershell
# --- Daemon Management ---
stackmind daemon start --port 8765        # Launch background JSON-RPC daemon
stackmind daemon status                   # Check active daemon status and session count
stackmind daemon stop                     # Gracefully stop the running daemon
stackmind daemon restart                  # Restart the daemon process

# --- Terminal User Interface ---
stackmind tui                             # Open Terminal Control Plane UI
stackmind tui --demo                      # Run simulated multi-agent demo session

# --- Knowledge Graph Operations ---
stackmind graph build -p .                # Initial deterministic graph compilation
stackmind graph update -p .               # Incremental update after code edits
stackmind graph query "WebSocketManager"  # Find exact symbol definition
stackmind graph callers "send_event"      # Discover all caller locations
stackmind graph impact "send_event"       # Analyze blast radius of changes across graph
stackmind graph context "auth flow"       # Retrieve token-bounded, ranked prompt context bundle

# --- Harness & Procedural Learning ---
stackmind harness run-once                # Run single governed harness turn
stackmind learn mine -p .                 # Mine candidate procedural skills from verified runs
stackmind skill list                      # List procedural skills and confidence scores
stackmind skill test <name>               # Execute 3-stage skill verification pipeline
stackmind experience search "<query>"     # Search past verified execution episodes (EXP-*)

# --- Repository Validation & Lifecycle ---
stackmind validate .                      # Validate all 5 runtime integrity layers
stackmind validate --fix .                # Auto-fix canonical drift in TREE.yaml
stackmind doctor .                        # Diagnostics, dependency integrity, and agent health
stackmind shutdown <agent>                # Run clean agent session shutdown with handoff archiving
```

---

*StackMind CLI & TUI v3.3.0 GA — Autonomous Multi-Role Engineering Delivery Built for Production.*
