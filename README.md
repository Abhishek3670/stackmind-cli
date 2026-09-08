# StackMind

> **Interactive Terminal OS & Governed Multi-Agent Engineering Runtime**

StackMind compiles codebases into deterministic, queryable knowledge graphs and provides a **zero-bypass, contract-governed Terminal User Interface (TUI)** for autonomous AI software engineering. 

Rather than allowing agents unrestricted access to shells and filesystems, StackMind routes all interactions through an authoritative **Runtime Kernel, Local Daemon, and Contract Boundary HUD**, complete with **Human-in-the-Loop (HITL)** controls and **6-dimensional authentic verification**.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Version: 3.2.0 GA](https://img.shields.io/badge/version-3.2.0%20GA-blue.svg)](VERSION.md)
[![Tests: 32 Kernel / 480 Suite](https://img.shields.io/badge/tests-32%20kernel%20%7C%20480%20passed-brightgreen.svg)](tests/)
[![Architecture: Zero--Bypass](https://img.shields.io/badge/architecture-zero--bypass-orange.svg)](docs/P6_TUI_ARCHITECTURE.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 🖥️ The StackMind Interactive TUI

StackMind features an interactive terminal interface built on OpenCode-compatible presentation principles, connected exclusively via **HTTP JSON-RPC** to the local runtime daemon.

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STACKMIND TUI v3.2.0  │ Session: sess-9f20c │ State: RUNNING │ Provider: claude-3-5-sonnet       │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 📋 CONTRACT HUD (WO-007)                                                                         │
│ Agent: codex (Backend Lead)  │ Token Budget: 34,210 / 50,000 │ Touched Files: 3 / 8              │
│ Allowed Subgraphs: validators.kernel.*, cli.tui, tests.test_tui_*                                │
│ Denied Subgraphs:  validators.learning.*, .sync.runtime.boot.*, production_db.*                  │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ⚡ ACTIVITY STREAM (Sequenced & Checkpointed)                                                     │
│  [10:42:01] ✓ Allowed tool call: query_graph(symbol="StackMindTuiAdapter")                      │
│  [10:42:03] ✓ Allowed tool call: read_file(path="validators/kernel/tui/adapter.py")              │
│  [10:42:05] ✓ Staged scratch mutation: validators/kernel/tui/views.py (+44 lines)                │
│  [10:42:07] ✗ BLOCKED: write_file("validators/learning/skill.py") -> OUT_OF_SCOPE_DENIAL         │
│  [10:42:10] ⚠️  HITL REQUIRED: staged write to production-mapped path                            │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 🛡️  HUMAN-IN-THE-LOOP (HITL) APPROVAL REQUIRED                                                    │
│ Operation: Apply scratch changeset to workspace                                                  │
│ Target:    validators/kernel/tui/views.py                                                        │
│ Action:    [ A - Approve ]   [ R - Reject ]   [ D - Inspect Unified Diff ]   [ C - Cancel Turn ] │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 🔍 6-DIMENSIONAL VERIFICATION MATRIX                                                            │
│ Scope: PASS  │ State: PASS  │ AST: PASS  │ Behavioral: PASS  │ Security: PASS  │ Outcome: PASS    │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ :new [wo]  │ :resume <id>  │ :diff  │ :pause  │ :cancel  │ :help                   [STATUS: READY] │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🏛️ Zero-Bypass Architecture

A core invariant of StackMind: **The TUI never executes subprocesses, accesses the filesystem, or contacts LLM providers directly.** All operations flow through an isolated JSON-RPC daemon and the governed runtime kernel:

```text
┌────────────────────────────────────────────────────────┐
│                    USER / DEVELOPER                    │
└───────────────────────────┬────────────────────────────┘
                            │ (Keystrokes / Approvals)
                            ▼
┌────────────────────────────────────────────────────────┐
│              INTERACTIVE TERMINAL UI                   │
│       OpenCode-Compatible Presentation Surface         │
└───────────────────────────┬────────────────────────────┘
                            │ (Lifecycle & HITL Events)
                            ▼
┌────────────────────────────────────────────────────────┐
│               STACKMIND TUI ADAPTER                    │
│   Maps TUI events to strictly typed RPC requests       │
└───────────────────────────┬────────────────────────────┘
                            │ (HTTP JSON-RPC over /rpc)
                            ▼
┌────────────────────────────────────────────────────────┐
│               STACKMIND RUNTIME DAEMON                 │
│                                                        │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────┐  │
│  │ SessionManager │  │  Contract Gate │  │ Sandboxes│  │
│  └────────────────┘  └────────────────┘  └──────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────┐  │
│  │ ProviderGateway│  │ 6D Verifier    │  │ Evidence │  │
│  └────────────────┘  └────────────────┘  └──────────┘  │
└───────────────────────────┬────────────────────────────┘
                            │ (Deterministic Isolation)
                            ▼
┌────────────────────────────────────────────────────────┐
│          SCRATCH-WORKSPACE EXECUTION KERNEL            │
│       Non-Destructive Mutations & Formal Audits        │
└────────────────────────────────────────────────────────┘
```

### Architectural Guarantees:
- **No Direct Shell Access:** The TUI cannot run arbitrary commands. Shell operations execute only inside sandboxed scratch workspaces.
- **Fail-Closed Governance:** File writes outside the active YAML contract are rejected at the kernel level before disk mutation occurs.
- **Persistent Auditability:** Every keystroke, tool invocation, token count, and HITL decision is recorded in monotonic journal receipts.
- **Seamless Reconnection:** Client connections stream checkpointed events using sequence cursors (`event.list`), enabling instant session recovery upon terminal restart.

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

Start the local runtime daemon and open the interactive terminal interface:

```bash
# Start the background daemon
stackmind daemon start

# Launch the interactive TUI
stackmind tui
```

### 3. TUI Navigation & Keybindings

| Command | Action | Description |
|---|---|---|
| `:new <WO-ID>` | **Start Session** | Initializes a governed session bound to a specific Work Order contract |
| `:resume <id>` | **Resume Session** | Reconnects to an existing session and replays checkpointed events |
| `:diff` | **Inspect Diff** | Opens the unified diff viewer showing scratch vs. workspace changes |
| `:approve` / `A` | **HITL Approve** | Authorizes an in-scope sensitive tool or changeset application |
| `:reject` / `R` | **HITL Reject** | Denies the requested operation and returns control to the agent |
| `:pause` | **Pause Turn** | Temporarily halts agent execution turns without dropping state |
| `:cancel` / `C` | **Abort Turn** | Immediately cancels in-flight agent actions and records cancellation receipt |
| `:help` | **Help HUD** | Toggles the command palette and active keyboard shortcuts |

---

## 🧩 The Six Platform Pillars

StackMind provides a complete, unified operating system for autonomous agentic engineering:

```text
                                  STACKMIND PLATFORM (v3.2.0 GA)
                                                │
    ┌──────────────┬──────────────┬─────────────┴─────────────┬──────────────┬──────────────┐
    ▼              ▼              ▼                           ▼              ▼              ▼
┌────────────┐ ┌────────────┐ ┌────────────┐             ┌────────────┐ ┌────────────┐ ┌────────────┐
│  Pillar 1  │ │  Pillar 2  │ │  Pillar 3  │             │  Pillar 4  │ │  Pillar 5  │ │  Pillar 6  │
│ Interactive│ │ Governance │ │ Knowledge  │             │   Secure   │ │ Supervised │ │ Procedural │
│  TUI & IDE │ │& Contracts │ │  Compiler  │             │ Execution  │ │Multi-Agent │ │  Learning  │
│  (P5 & P6) │ │(CONTRACT-01│ │  (KNOW-01) │             │ (HARNESS-01│ │  (Phase P7)│ │ (LEARN-01) │
└────────────┘ └────────────┘ └────────────┘             └────────────┘ └────────────┘ └────────────┘
```

1. **Interactive TUI & Presentation Layer (`Phase P5 & P6`):** OpenCode-compatible TUI and governed MCP/IDE bridges communicating over JSON-RPC.
2. **Runtime Governance & Contract Layer (`CONTRACT-01`):** Formal YAML contracts specifying allowed AST symbols, file scopes, token budgets, and security tiers.
3. **Knowledge Compiler & Graph Intelligence (`KNOW-01`):** Deterministic AST-to-IR compilation, 14 domain compilers, runtime call tracing (`sys.setprofile`), and data-flow taint analysis (`FLOWS_TO`).
4. **Secure Execution Kernel (`HARNESS-01`):** Scratch-workspace execution isolating unverified code changes until passing 6-dimensional verification.
5. **Supervised Multi-Agent Runtime (`Phase P7`):** Strict process isolation across specialized roles (`claude` Architect, `codex` Backend, `gemini` Frontend, `gemma` QA, `local-llm` GitOps).
6. **Verified Procedural Learning (`LEARN-01`):** Distills verified execution clusters ($N \ge 3$) into reusable, 3-stage tested procedural skill manifests.

---

## 🛠️ CLI Reference

```bash
# Terminal User Interface & Daemon
stackmind tui                          # Launch the interactive terminal user interface
stackmind daemon start                 # Start the local JSON-RPC runtime daemon
stackmind daemon status                # Inspect active daemon sessions and connections

# Multi-Agent Governance Workspace
stackmind init ./my-project            # Initialize a new governed runtime workspace
stackmind validate ./my-project        # Execute 5-layer runtime integrity & contract validation
stackmind doctor ./my-project          # Diagnostics, dependency integrity, and agent health
stackmind shutdown <agent>             # Gracefully terminate session with handoff validation

# Knowledge Compiler & Symbol Queries
stackmind graph build -p .             # Compile workspace into deterministic graph IR
stackmind graph query "symbol" -p .    # Look up symbols without scanning source files
stackmind graph callers "symbol" -p .  # Find statically verified and runtime-traced callers
stackmind graph impact "symbol" -p .   # Analyze blast radius of changes across graph
stackmind graph context "task" -p .    # Assemble token-bounded, ranked prompt context bundle

# Governance & Verification
stackmind graph contract show <WO>     # Inspect active contract boundaries & budgets
stackmind graph contract validate <WO> # Test operation validity against contract rules
stackmind graph explain-denial <WO>    # Debug scope denial reasons for a symbol or file

# Procedural Learning & Skill Engine
stackmind experience search "query"    # Search past verified execution episodes (EXP-*)
stackmind learn mine -p .              # Mine recurring execution patterns into skill candidates
stackmind skill test <name>            # Run 3-stage verification pipeline (Structural/Replay/Canary)
stackmind skill list --status active   # View active, verified procedural skills
```

---

## 🔍 Authentic 6-Dimensional Verification

StackMind never relies on LLM self-reporting. Every staged code modification is verified across 6 independent dimensions before touching your project repository:

| Dimension | Verification Method | Enforcement |
|---|---|---|
| **Scope** | Compares modified paths against Contract YAML `allow` / `deny` | Fail-Closed |
| **State** | Authoritative `WorkspaceSnapshot` before/after diff analysis | Zero Phantom Edits |
| **AST** | Syntactic parse validation preventing syntax regressions | Lint & AST check |
| **Behavioral**| Automated test execution (`pytest`) in isolated virtual environment | Non-zero Exit Rejection |
| **Security** | Static taint analysis (`FLOWS_TO`) and secret regex scanning | Prevents credential leaks |
| **Outcome** | Verification criteria check against Work Order deliverables | Acceptance Gate |

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
└── inbox/ & outbox/        # Structured inter-agent communication channels
```

---

## 📖 Documentation Links

- **TUI Architecture & Specification**: [docs/P6_TUI_ARCHITECTURE.md](docs/P6_TUI_ARCHITECTURE.md)
- **TUI Evaluation & Adoption Decision**: [docs/P6_TUI_ADOPTION_DECISION.md](docs/P6_TUI_ADOPTION_DECISION.md)
- **Multi-Agent Governance Rules**: [AGENTS.md](AGENTS.md)
- **Agent Runtime Roadmap**: [STACKMIND_AGENT_RUNTIME_ROADMAP_FINAL.md](STACKMIND_AGENT_RUNTIME_ROADMAP_FINAL.md)
- **Complete Architecture Handbook**: [STACKMIND.md](STACKMIND.md)
- **Procedural Learning Specification**: [PLAN_PROCEDURAL_LEARNING.md](PLAN_PROCEDURAL_LEARNING.md)
- **Changelog**: [CHANGELOG.md](CHANGELOG.md)
- **Version Manifest**: [VERSION.md](VERSION.md)

---

## 📄 License

MIT © [Abhishek Sharma](https://github.com/Abhishek3670/stackmind)
