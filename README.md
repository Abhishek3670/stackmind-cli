# StackMind

> **Interactive Terminal OS & Governed Multi-Agent Engineering Runtime**

StackMind compiles codebases into deterministic, queryable knowledge graphs and provides an **OpenCode-inspired, contract-governed Terminal User Interface (TUI)** and **autonomous multi-role engineering runtime** (`v3.3.0 GA`).

Rather than allowing agents unrestricted access to shells and filesystems, StackMind routes all interactions through an authoritative **Runtime Kernel, Local Daemon, and Contract Boundary HUD**, complete with **Human-in-the-Loop (HITL)** plan approvals, **dynamic model backend rebinding**, and **authentic 6-dimensional verification**.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Version: 3.3.0 GA](https://img.shields.io/badge/version-3.3.0%20GA-blue.svg)](VERSION.md)
[![Tests: 85 P7 / 570+ Suite](https://img.shields.io/badge/tests-85%20P7%20%7C%20570+%20passed-brightgreen.svg)](tests/)
[![Architecture: Zero--Bypass](https://img.shields.io/badge/architecture-zero--bypass-orange.svg)](docs/P6_TUI_ARCHITECTURE.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 🖥️ The StackMind Interactive TUI Control Plane

StackMind features an interactive terminal interface built on OpenCode-compatible presentation principles, connected exclusively via **HTTP JSON-RPC 2.0 and Server-Sent Events (SSE)** to the local runtime daemon.

```text
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STACKMIND TUI v3.3.0 GA  │ Session: 2c394ab9... │ Phase: Autonomous Execution │ Provider: daemon       │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 👥 AGENT ROLES                                                                                         │
│ ● Architecture [claude]     orchestrating   (backend: echo-agent)                                      │
│ ● Backend      [codex]      running         (backend: ollama, model: ornith-1.5:9b)                    │
│ ○ Frontend     [gemini]     waiting         (backend: echo-agent)                                      │
│ ○ Q/A          [gemma]      waiting         (backend: echo-agent)                                      │
│ ○ GitOps       [local-llm]  waiting         (backend: echo-agent)                                      │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 📋 WORK ORDERS                                                                                         │
│ ✓ WO-026  BUGFIX: Thread-safe BackendRegistry & Defensive Snapshotting          [COMPLETED]            │
│ ✓ WO-027  BUGFIX: Typed Live Model Backend Error Classification                 [COMPLETED]            │
│ ● WO-028  BUGFIX: Evidentiary 6-Stage Verification Gate & Write-back            [IN_PROGRESS]          │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 🌳 HIERARCHICAL OPERATION TREE                                                                         │
│ └── Session (2c394ab9)                                                                                 │
│     └── Operation (aa98aefe): "Turn with Ollama ornith-1.5:9b" [RUNNING]                               │
│         ├── Child Op (op-101): "Generate WebSocket notification service" [COMPLETED]                  │
│         └── Child Op (op-102): "Verify syntax and execute pytest suite"   [RUNNING]                    │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ⚡ LIVE ACTIVITY FEED (Monotonic & Sequenced)                                                          │
│  [03:24:11] ✓ Allowed tool call: query_graph(symbol="ModelExecutionBackend")                           │
│  [03:24:14] ✓ Isolated scratch execution in tempfile.TemporaryDirectory()                             │
│  [03:24:17] ✓ AST check passed: ast.parse verified clean syntax on app/notifications/ws.py             │
│  [03:24:19] 🛡️  6D Gate: Scope=PASS, State=PASS, Code=PASS, Behavioral=PASS, Security=PASS, Outcome=PASS │
│  [03:24:20] 🚀 Verified write-back applied cleanly to live workspace                                  │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ :status │ :roles │ :wo │ :tree │ :plan │ :approve │ :rebind │ :diff │ :matrix │ :events │ :help        │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🏛️ Zero-Bypass Architecture & The Single-Source Rule

StackMind enforces the non-negotiable **Single-Source Rule**:
> **There is exactly one authoritative execution path in StackMind: `AgentRunner`.**  
> Under no circumstances may a daemon, TUI, orchestrator, or autonomous sub-system introduce an alternative execution engine, direct LLM provider loop, or un-governed tool execution path. All prompt turns, tool invocations, code modifications, and verifications strictly traverse the `AgentRunner` pipeline.

```text
┌────────────────────────────────────────────────────────┐
│                    USER / DEVELOPER                    │
└───────────────────────────┬────────────────────────────┘
                            │ (Keystrokes / Prompts / Rebinds)
                            ▼
┌────────────────────────────────────────────────────────┐
│              INTERACTIVE TERMINAL UI                   │
│       OpenCode-Compatible Presentation Surface         │
└───────────────────────────┬────────────────────────────┘
                            │ (HTTP JSON-RPC :8765/rpc, SSE /events)
                            ▼
┌────────────────────────────────────────────────────────┐
│               LOCAL RUNTIME DAEMON                     │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────┐  │
│  │ SessionManager │  │  Contract Gate │  │ Sandboxes│  │
│  └────────────────┘  └────────────────┘  └──────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────┐  │
│  │ OperationTree  │  │ Durable State  │  │ Rebinding│  │
│  └────────────────┘  └────────────────┘  └──────────┘  │
└───────────────────────────┬────────────────────────────┘
                            │ (Single-Source Delegation)
                            ▼
┌────────────────────────────────────────────────────────┐
│               AGENTRUNNER HARNESS                      │
│  ├── CONTRACT-01 Scope Enforcement (allow/deny rules)  │
│  ├── Isolated Staging (tempfile.TemporaryDirectory)    │
│  ├── Authentic 6D Verification Gate (Fail-closed)      │
│  └── D025 Destructive Safeguards (Backups & Approvals) │
└───────────────────────────┬────────────────────────────┘
                            │ (ExecutionBackend Protocol)
                            ▼
┌────────────────────────────────────────────────────────┐
│               BACKEND REGISTRY                         │
│  ├── Thread-safe RLock synchronization (WO-026)        │
│  ├── ModelExecutionBackend: Ollama /api/generate       │
│  └── Typed fault classification (WO-027)               │
└────────────────────────────────────────────────────────┘
```

### Architectural Guarantees:
- **No Direct Shell Access:** The TUI cannot run arbitrary commands. Shell operations execute only inside isolated temporary scratch workspaces.
- **Fail-Closed Governance:** File writes outside the active YAML contract or failing any verification dimension are rejected before workspace write-back.
- **Durable Operation Recovery:** Daemon state (`sessions`, `operations`, `role_bindings`) persists atomically in `workspace/.sync/runtime/daemon/daemon-state.json`. If restarted, operations and parent-child cancellation trees reconstruct reliably.
- **Dynamic Role Rebinding:** Agent roles can be rebound to local Ollama models (e.g. `:rebind backend ollama ornith-1.5:9b`) on the fly without restarting the daemon.
- **Zero-Leakage Security:** Scans all event streams, RPC outputs, and serialized states with `CredentialLeakScanner` to redact secrets automatically.

---

## ⚡ Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/Abhishek3670/stackmind.git
cd stackmind

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

### 2. Launching the Interactive TUI

Start the local runtime daemon and open the interactive terminal control plane:

```bash
# Start the background daemon
stackmind daemon start

# Check daemon health
stackmind daemon status

# Launch the interactive TUI
stackmind tui
```

### 3. TUI Navigation & Keybindings

| Command | Action | Description |
|---|---|---|
| `:status` / `s` | **Phase Banner** | Display overall delivery phase, session ID, and Contract HUD |
| `:roles` / `a` | **Agent Roles Panel** | View real-time status and active backend bindings for all roster agents |
| `:wo` / `w` | **Work Orders Panel** | View active vs completed Work Orders and assigned workers |
| `:tree` / `:agents` | **Operation Tree** | Inspect hierarchical parent-child operation tree (`Session` → `Operation` → `children`) |
| `:plan` / `p` | **Plan Surface** | View proposed architecture plan with HITL approval modal |
| `:approve` / `Ctrl+A` | **HITL Approve** | Formally approve proposed architecture plan to commence worker dispatch |
| `:reject` / `Ctrl+R` | **HITL Reject** | Reject proposed plan and trigger structured revision loop (up to 3 revisions) |
| `:rebind <role> <backend> [model]` | **Dynamic Rebind** | Rebind a role to a model backend (e.g. `:rebind backend ollama ornith-1.5:9b`) |
| `:diff` / `d` | **Inspect Diff** | Opens the unified diff viewer showing staged vs workspace changes |
| `:matrix` / `m` | **6D Quality Matrix** | View authentic verification dimensions (Scope, State, AST, Behavioral, Security, Outcome) |
| `:events` / `e` | **Activity Stream** | Stream live sequenced daemon events with monotonic sequence cursors |
| `:completion` / `c` | **Completion Handover**| View final delivery report, test summary, and commit SHAs |
| `Ctrl+C` / `:cancel` | **Cooperative Cancel** | Cancel in-flight operation checkpoint without killing session |
| `:exit` / `:quit` / `q` | **Exit TUI** | Disconnect from TUI (daemon continues running in background) |

---

## 🧩 The Seven Platform Pillars

StackMind provides a complete, unified operating system for autonomous agentic software engineering:

```text
                                  STACKMIND PLATFORM (v3.3.0 GA)
                                                │
    ┌──────────────┬──────────────┬─────────────┴─────────────┬──────────────┬──────────────┬──────────────┐
    ▼              ▼              ▼                           ▼              ▼              ▼              ▼
┌────────────┐ ┌────────────┐ ┌────────────┐             ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│  Pillar 1  │ │  Pillar 2  │ │  Pillar 3  │             │  Pillar 4  │ │  Pillar 5  │ │  Pillar 6  │ │  Pillar 7  │
│ Interactive│ │ Governance │ │ Knowledge  │             │   Secure   │ │ Supervised │ │ Execution  │ │ Procedural │
│  TUI & IDE │ │& Contracts │ │  Compiler  │             │  Execution │ │Multi-Agent │ │  Backends  │ │  Learning  │
│  (Phase P6)│ │(CONTRACT-01│ │  (KNOW-01) │             │ (HARNESS-01│ │  (Phase P7)│ │  (WO-020)   │ │ (LEARN-01) │
└────────────┘ └────────────┘ └────────────┘             └────────────┘ └────────────┘ └────────────┘ └────────────┘
```

1. **Interactive TUI & Presentation Layer (`Phase P6`):** OpenCode-compatible TUI and governed MCP/IDE bridges communicating over JSON-RPC 2.0 and SSE.
2. **Runtime Governance & Contract Layer (`CONTRACT-01`):** Formal YAML contracts specifying allowed AST symbols, file scopes, token budgets, and security tiers.
3. **Knowledge Compiler & Graph Intelligence (`KNOW-01`):** Deterministic AST-to-IR compilation, 14 domain compilers, runtime call tracing (`sys.setprofile`), and data-flow taint analysis (`FLOWS_TO`).
4. **Secure Execution Kernel & 6D Gate (`HARNESS-01`):** Scratch-workspace execution with fail-closed authentic 6-stage verification gate (`WO-028`).
5. **Supervised Multi-Agent Runtime (`Phase P7`):** Hierarchical operation trees, cooperative cancellation, and strict process isolation across specialized roles (`claude` Architect, `codex` Backend, `gemini` Frontend, `gemma` QA, `local-llm` GitOps).
6. **Execution Backend Abstraction (`WO-020`, `WO-026`, `WO-027`):** Thread-safe `BackendRegistry` with `threading.RLock`, live Ollama HTTP POST integration, and typed fault classification.
7. **Verified Procedural Learning (`LEARN-01`):** Distills verified execution clusters ($N \ge 3$) into reusable, 3-stage tested procedural skill manifests.

---

## 🛠️ CLI Reference

```bash
# --- Daemon Management ---
stackmind daemon start [--port 8765]      # Launch local background JSON-RPC runtime daemon
stackmind daemon status                   # Inspect active daemon sessions and connections
stackmind daemon stop                     # Gracefully stop the background daemon
stackmind daemon restart                  # Restart running daemon process

# --- Terminal Control Plane ---
stackmind tui                             # Launch the interactive terminal user interface
stackmind tui --demo                      # Run simulated multi-agent autonomous delivery demo

# --- Multi-Agent Governance Workspace ---
stackmind init ./my-project               # Initialize a new governed runtime workspace
stackmind validate ./my-project           # Execute 5-layer runtime integrity & contract validation
stackmind validate --fix ./my-project     # Auto-fix canonical drift in TREE.yaml
stackmind doctor ./my-project             # Diagnostics, dependency integrity, and agent health
stackmind shutdown <agent>                # Gracefully terminate session with handoff validation
stackmind lock acquire|release|status     # Advisory repository write lock

# --- Knowledge Compiler & Symbol Queries ---
stackmind graph build -p .                # Compile workspace into deterministic graph IR
stackmind graph update -p .               # Incremental update after code modifications
stackmind graph query "symbol" -p .       # Look up symbols without scanning source files
stackmind graph callers "symbol" -p .     # Find statically verified and runtime-traced callers
stackmind graph impact "symbol" -p .      # Analyze blast radius of changes across graph
stackmind graph context "task" -p .       # Assemble token-bounded, ranked prompt context bundle

# --- Governance & Contracts ---
stackmind graph contract show <WO>        # Inspect active contract boundaries & budgets
stackmind graph contract validate <WO>    # Test operation validity against contract rules
stackmind graph explain-denial <WO>       # Debug scope denial reasons for a symbol or file

# --- Procedural Learning & Skill Engine ---
stackmind experience search "query"       # Search past verified execution episodes (EXP-*)
stackmind learn mine -p .                 # Mine recurring execution patterns into skill candidates
stackmind skill test <name>               # Run 3-stage verification pipeline (Structural/Replay/Canary)
stackmind skill list --status active      # View active, verified procedural skills
```

---

## 🔍 Authentic 6-Dimensional Verification Gate

StackMind never relies on LLM self-reporting. Every staged code modification is verified across 6 independent dimensions before write-back to your project repository:

| Dimension | Verification Method | Enforcement |
|---|---|---|
| **Scope** | Compares modified paths against Contract YAML `allow` / `deny` rules | Fail-Closed |
| **State** | Authoritative lock state and before/after file hash consistency | Zero Corruption |
| **Code** | Full AST syntax parsing (`ast.parse`) across modified `.py` files | Rejects Syntax Errors |
| **Behavioral**| Automated command return-code validation (`exit_code == 0`) | Non-zero Exit Rejection |
| **Security** | Static credential scanning (`CredentialLeakScanner`) and D025 safeguards | Blocks Secret Leaks |
| **Outcome** | Verification criteria check against Work Order task deliverables | Acceptance Gate |

---

## 📂 Architecture & Storage Model

```text
.sync/
├── contracts/              # CONTRACT-01 scope boundaries & token/file budgets (YAML)
├── work-orders/            # Persistent task lifecycles (ACTIVE, BLOCKED, COMPLETED)
├── knowledge/              # KNOW-01 deterministic graph IR and canonical registries
│   ├── registry/           # T0 — Canonical symbol identity (birth-hashes)
│   ├── nodes/              # T1 — Sharded deterministic node documents
│   └── cache/              # T2 — Derived projections (reverse index, vector search)
├── experience/             # LEARN-01 captured execution episodes (EXP-*.json)
│   └── cache/              # Rebuildable SQLite FTS5 search index (experience_index.db)
├── skills/                 # Procedural skill manifests, receipts, and active pointers
│   ├── manifests/          # Versioned skill manifests (v1.json, v2.json, ...)
│   └── active/             # Fast O(1) active skill pointers
├── runtime/                # Canonical TREE.yaml, boot snapshots, receipts, write lock
│   └── daemon/             # Durable daemon state (daemon-state.json)
└── inbox/ & outbox/        # Structured inter-agent communication channels
```

---

## 📖 Documentation Links

- **Autonomous Multi-Role Delivery Guide**: [demo.md](demo.md)
- **CLI Architecture & Kernel Handbook**: [STACKMIND_CLI.md](STACKMIND_CLI.md)
- **TUI Architecture & Specification**: [docs/P6_TUI_ARCHITECTURE.md](docs/P6_TUI_ARCHITECTURE.md)
- **Multi-Agent Governance Rules**: [AGENTS.md](AGENTS.md)
- **P7 Technical Audit Report**: [docs/runtime-truth/P7-technical-audit-report.md](docs/runtime-truth/P7-technical-audit-report.md)
- **Agent Runtime Roadmap**: [STACKMIND_AGENT_RUNTIME_ROADMAP_FINAL.md](STACKMIND_AGENT_RUNTIME_ROADMAP_FINAL.md)
- **Complete Architecture Handbook**: [STACKMIND.md](STACKMIND.md)
- **Procedural Learning Specification**: [PLAN_PROCEDURAL_LEARNING.md](PLAN_PROCEDURAL_LEARNING.md)
- **Changelog**: [CHANGELOG.md](CHANGELOG.md)
- **Version Manifest**: [VERSION.md](VERSION.md)

---

## 📄 License

MIT © [Abhishek Sharma](https://github.com/Abhishek3670/stackmind)
