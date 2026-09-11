# STACKMIND_CLI
> CLI Frontend & Governed Runtime Kernel for StackMind

**Version:** 3.2.0 · **Python:** ≥3.10 · **License:** MIT · **Author:** Abhishek Sharma

---

## Table of Contents
1. [What Is StackMind CLI?](#1-what-is-stackmind-cli)
2. [Quick Start](#2-quick-start)
3. [Architecture](#3-architecture)
4. [Runtime Kernel](#4-runtime-kernel)
5. [Daemon (JSON-RPC 2.0)](#5-daemon-json-rpc-20)
6. [TUI Client](#6-tui-client)
7. [Governance Layer](#7-governance-layer)
8. [Harness Runtime](#8-harness-runtime)
9. [CLI Reference](#9-cli-reference)
10. [File Structure](#10-file-structure)
11. [Tech Stack](#11-tech-stack)
12. [Key Design Decisions](#12-key-design-decisions)
13. [Non-Goals](#13-non-goals)

---

## 1. What Is StackMind CLI?

StackMind CLI is the command-line frontend and governed runtime kernel for the StackMind platform. It provides three integrated capabilities:

| Capability | What It Does | Status |
|---|---|---|
| **TUI Client + JSON-RPC Daemon** | Interactive terminal client communicating with a local daemon over JSON-RPC 2.0; session lifecycle, streaming events, HITL approvals, operation-scoped cancellation | Shipped v3.2.0 GA |
| **Runtime Governance Kernel** | Operation journal, contract enforcement, authorization boundary, sandboxing, D025 destructive safeguards | Shipped v3.2.0 GA |
| **Harness Runtime** | Governed agent execution loop with Knowledge API integration, contract verification gates, D025 safeguards | Shipped v2.0.0 |

The TUI is a **client only** — it never becomes a second runtime. The daemon is the trusted governance authority. All tool execution, contract evaluation, and state mutation happen inside the daemon process.

```text
                 STACKMIND CLI RUNTIME
                          │
        ┌─────────────────┼─────────────────┐
        ▼                                   ▼
┌──────────────┐                   ┌──────────────┐
│  TUI Client  │  JSON-RPC 2.0    │   LocalDaemon │
│  (tui.py)    │◄─────────────────►│  (server.py)  │
│              │    request/id      │              │
│ :new         │                   │ SessionManager │
│ :status      │                   │ EventDispatcher│
│ :approve     │                   │ DaemonStorage  │
│ :cancel      │                   │ JsonRpcProtocol│
│ :events      │                   │ /health /rpc   │
│ :diff        │                   └──────────────┘
│ :matrix      │
│ :pause       │              ┌──────────────────┐
│ :demo        │              │  Runtime Kernel  │
└──────────────┘              │  (kernel/*)      │
                              │  Contract/Policy │
                              │  ToolGateway     │
                              │  ScratchWorkspace│
                              │  D025 Gate       │
                              └──────────────────┘
```

---

## 2. Quick Start

### Installation
```bash
pip install stackmind
```

Or from source:
```bash
git clone https://github.com/Abhishek3670/stackmind.git
cd stackmind
pip install -e ".[dev]"
```

### Run the Interactive TUI (v3.2.0 GA)
```bash
# Start the TUI — launches daemon + interactive terminal
stackmind tui
# Or directly:
python tui.py
```

### Available TUI Commands
| Command | Description |
|---|---|
| `:new [agent]` | Create a new governed session (default agent: codex) |
| `:status` | Display session header + Contract Boundary HUD |
| `:events` | Stream incremental sequenced events from daemon |
| `:approve [reason]` | Submit Human-in-the-Loop (HITL) approval |
| `:reject [reason]` | Submit HITL rejection |
| `:pause` / `:resume` | Pause / resume active session |
| `:cancel` | Cancel in-flight operation (operation-scoped, not session-kill) |
| `:diff` | Unified diff viewer for staged changes |
| `:matrix` | 6-Dimensional Verification Matrix |
| `:demo` | Re-run automated TUI demonstration |
| `:help` | Show help |
| `:exit` / `:quit` / `q` | Graceful shutdown |

### Standalone Knowledge Graph (unchanged)
```bash
stackmind graph build -p /path/to/project
stackmind graph query "AuthService.login" -p /path/to/project
stackmind graph callers "AuthService.login" -p /path/to/project
stackmind graph impact "AuthService.login" --depth 3 -p /path/to/project
```

---

## 3. Architecture

### Client–Daemon Separation (v3.2.0)

The TUI client and daemon are separate processes communicating over JSON-RPC 2.0:

```text
┌─────────────────┐         JSON-RPC 2.0          ┌──────────────────┐
│  TUI Client      │  session.create/get/list      │   LocalDaemon    │
│  (tui.py)        │  session.pause/resume/cancel  │   (server.py)    │
│                  │  session.approval             │                  │
│  StackMindTuiAdapter│ event.list                │  SessionManager  │
│  DaemonClient    │                               │  EventDispatcher │
│                  │◄── notification (stream) ─────│  DaemonStorage   │
└─────────────────┘    keyed by requestId + seq    └──────────────────┘
       │                                                      │
       │  :new / :status / :approve / :cancel / :events        │
       ▼                                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Runtime Kernel (kernel/*)                      │
│  ContractEvaluator  │  ToolGateway  │  RuntimeBoundary          │
│  ScratchWorkspace   │  D025 Gate    │  AuthenticEvidenceTracer  │
└──────────────────────────────────────────────────────────────────┘
```

### Key Separation Principles
1. **TUI is a client, never a runtime** — no execution privileges, no direct file access
2. **Daemon is the trusted authority** — session state, contract enforcement, cancellation
3. **Operation-scoped cancellation** — canceling a response cancels only that operation, not the session
4. **Event sequence numbers** — monotonic cursor for replay/resume, canonical ordering
5. **Capability negotiation** — client declares support via `health.version` handshake

---

## 4. Runtime Kernel

The kernel (`validators/kernel/`) provides the core governance primitives:

### Module Map
| Module | Purpose |
|---|---|
| `boundary.py` | P0 provider-to-runtime operation boundary; records + authorizes, no live execution |
| `contract.py` | `AgentContract` (immutable), `ContractNormalizer`, `ContractEvaluator` (fail-closed) |
| `evidence.py` | `AuthenticEvidenceTracer`, `AuthenticObservation`, `ExperienceEligibilityGate` |
| `identity.py` | `AgentIdentity`, `ProviderIdentity`, `HumanIdentity`, `AuthorizationPolicy` |
| `operations.py` | `OperationRequest`, `OperationRecord`, `OperationJournal` (auditable first-class ops) |
| `session.py` | `AgentSession`, `Attempt`, `LifecycleState` state machine |
| `tools.py` | `ToolGateway` — all tools cross policy + contract + journal boundaries |
| `workspace.py` | `ScratchWorkspace` — disposable copy, never live directory; escape detection |
| `sandbox.py` | `ProcessSandbox` — contained subprocess execution |

### Lifecycle States
```text
CREATED → RUNNING → WAITING → PAUSED → VERIFYING → COMPLETED
                  ↘ FAILED  ↗          ↘ CANCELLED
```

### Operation Types
`READ_FILE` · `WRITE_FILE` · `RUN_COMMAND` · `QUERY_GRAPH`

Every operation is journaled, authorized against contract scope, and recorded with before/after hashes.

---

## 5. Daemon (JSON-RPC 2.0)

The daemon (`validators/kernel/daemon/`) is a single-threaded HTTP JSON-RPC server with a notification-capable event stream.

### RPC Methods
| Method | Params | Result | Description |
|---|---|---|---|
| `session.create` | agent, provider, contract, workspace | session view | Create governed session |
| `session.get` | session_id | session view | Get session state |
| `session.list` | — | [session…] | List all sessions |
| `session.pause` | session_id | session view | Pause session |
| `session.resume` | session_id | session view | Resume paused session |
| `session.cancel` | session_id | session view | Cancel session (terminal) |
| `session.approval` | session_id, approved, reason? | session view | Record HITL decision |
| `event.list` | session_id, after (seq) | [event…] | Replay events since cursor |

### Error Codes
| Code | Meaning |
|---|---|
| `-32600` | Invalid Request |
| `-32602` | Invalid params / unknown session / session terminal |
| `-32603` | Internal error |

### Event Stream (notification-capable)
Events carry: `sequence`, `name`, `session_id`, `payload`, `timestamp`. Key events:
- `session.started` · `session.recovered` · `session.paused` · `session.resumed`
- `operation.requested` · `operation.authorized` · `operation.started` · `operation.completed` · `operation.cancelled`
- `contract.loaded` · `attempt.started`
- `approval.recorded` · `verification.started/completed` · `experience.recorded`

### Endpoints
| Endpoint | Method | Response |
|---|---|---|
| `/health` | GET | `{status, sessions}` |
| `/rpc` | POST | JSON-RPC response |
| `/mcp` | POST | MCP protocol dispatch (optional) |

### Persistence
`DaemonStorage` uses atomic write (tempfile + fsync + rename) to `daemon-state.json`. On recovery, RUNNING sessions are reset to WAITING; events are replayable from sequence numbers.

---

## 6. TUI Client

The TUI client (`validators/kernel/tui/`) is a dependency-free terminal renderer with no execution privileges.

### Components
| Component | Purpose |
|---|---|
| `DaemonClient` | HTTP/JSON-RPC client — raw TCP, no execution |
| `StackMindTuiAdapter` | Maps TUI commands to daemon calls; event replay cursor |
| `session_header` | Renders session ID, state, provider |
| `contract_panel` | Renders allow/deny scope boundary |
| `activity_line` | Renders sequenced event with ✓/✗ marker |
| `diff_viewer` | Unified diff renderer |
| `hitl_prompt` | Human-in-the-loop approval prompt |
| `verification_matrix` | 6-dimension verification display |

### Adapter Command Map
| TUI Input | Daemon Call |
|---|---|
| `:new` | `session.create` |
| `:resume` | `session.get` |
| `:pause` | `session.pause` |
| `:cancel` | `session.cancel` |
| `:approve` / `:reject` | `session.approval` |
| `:events` | `event.list` (streaming with seq cursor) |

---

## 7. Governance Layer

### D025 Destructive Operations Safeguard
Programmatic gate for agent-proposed commands:
1. Detects destructive operations (git history rewrite, mass deletion, docker prune, DB drop)
2. Enforces mandatory pre-operation backup
3. Enforces mandatory post-operation verification
4. Blocks execution when safeguards are missing
5. Emits structured audit events

### Contract Enforcement (CONTRACT-01)
- Fail-closed scope boundaries (allow/deny rules)
- Write-mode gating (read-only vs read-write)
- File-touch budget enforcement
- Path traversal prevention
- Rule matching via fnmatch + prefix checks

### Authorization Chain
```text
TUI Command → DaemonClient → JsonRpcProtocol → SessionManager
    → RuntimeBoundary → ContractEvaluator → AuthorizationPolicy
    → OperationJournal → ToolGateway → ProcessSandbox
```

---

## 8. Harness Runtime

The Harness (`validators/harness/`) provides the governed execution loop:

```text
1. Poll Inbox / Work Orders
2. Validate active Contract boundary
3. Assemble context via Knowledge API (contract-gated)
4. Invoke LLM provider
5. Validate LLM output against harness schema
6. Validate staged diff against Contract scope & D025
7. Write-back results on SUCCESS
8. Emit audit/events
9. Update Work Order / operation state
```

### Key Primitives
| Primitive | Purpose |
|---|---|
| `AgentRunner` | Governed worker execution loop with staged verification |
| `LLMProvider` | Protocol for LLM backends (echo for testing) |
| `HarnessTask` | Inbox item or assigned work order |
| `HarnessDecision` | Validated LLM output safe to stage |
| `D025Gate` | Destructive operations evaluator |
| `contract_gate.py` | Pre/post execution contract validation |
| `retrieval.py` | Multi-signal retrieval (lexical + semantic + graph) |
| `snapshot.py` | Workspace snapshot + diff + verification dimensions |

---

## 9. CLI Reference

| Command | Subcommands / Options | Description |
|---|---|---|
| `stackmind tui` | — | Launch interactive TUI (v3.2.0 GA) |
| `stackmind init` | `[path] [--name] [--agents]` | Initialize governed runtime |
| `stackmind validate` | `[path] [--fix]` | 5-layer runtime integrity check |
| `stackmind doctor` | `[path]` | System diagnostics |
| `stackmind graph` | `build`, `update`, `query`, `callers`, `impact`, `context` | Knowledge graph commands |
| `stackmind analyze` | `runtime`, `flows` | Runtime call tracer & data-flow |
| `stackmind lock` | `acquire`, `release`, `status` | Advisory write lock |
| `stackmind shutdown` | `<agent> [--defer]` | Mandatory session termination |
| `stackmind promote` | `<agent>` | Worker draft → canonical |
| `stackmind harness` | `run-once` | Single governed execution cycle |
| `stackmind migrate` | `[path] [--check] [--rollback]` | Version upgrade manager |

---

## 10. File Structure

```text
stackmind/
├── cli/                        # Click CLI entrypoints (18 modules)
│   ├── main.py                 # Root CLI group
│   ├── graph.py                # Knowledge graph commands
│   ├── contract.py             # Contract inspection & denial explainer
│   ├── analyze.py              # Runtime call tracer & data-flow analyzer
│   ├── harness.py              # Governed execution runner
│   ├── validate.py             # 5-layer runtime validator
│   ├── lock.py                 # Advisory write lock management
│   ├── shutdown.py             # Session termination & receipt writing
│   └── +10                     # decisions, doctor, experience, learn, etc.
│
├── validators/
│   ├── kernel/                 # Runtime governance kernel
│   │   ├── boundary.py         # P0 provider-to-runtime boundary
│   │   ├── contract.py         # AgentContract, ContractEvaluator, ContractNormalizer
│   │   ├── evidence.py         # AuthenticEvidenceTracer, Observation, EligibilityGate
│   │   ├── identity.py         # Agent/Provider/Human identities, AuthorizationPolicy
│   │   ├── operations.py       # OperationRequest, OperationRecord, OperationJournal
│   │   ├── session.py          # AgentSession, Attempt, LifecycleState
│   │   ├── tools.py            # ToolGateway — all tools cross policy/contract/journal
│   │   ├── workspace.py        # ScratchWorkspace — disposable copy, escape detection
│   │   ├── sandbox.py          # ProcessSandbox — contained subprocess execution
│   │   ├── verification/       # SandboxCanaryVerifier
│   │   ├── daemon/             # JSON-RPC daemon (server, protocol, manager, events, storage)
│   │   ├── mcp/                # MCP protocol server + tool registry
│   │   ├── multi/              # Agent roles, ensemble supervisor, task delegation
│   │   ├── providers/          # Provider adapter, gateway, models, errors
│   │   ├── tui/                # TUI client (adapter, client, views)
│   │   └── experience/         # Experience recorder, models, store, index
│   │
│   ├── harness/                # Governed execution runtime
│   │   ├── runner.py           # AgentRunner — governed execution loop
│   │   ├── contract_gate.py    # Pre/post execution contract validation
│   │   ├── d025_gate.py        # D025 destructive operations safeguard
│   │   ├── retrieval.py        # Multi-signal retrieval
│   │   ├── snapshot.py         # Workspace snapshot + diff + verification dimensions
│   │   └── __init__.py         # Exports AgentRunner, D025Gate, HarnessTask, etc.
│   │
│   └── knowledge/              # Knowledge compiler (Pillar 2)
│       ├── analysis/           # Runtime tracer, FLOWS_TO, evidence model
│       ├── compiler/           # 14 domain compilers
│       ├── embedding/          # Embedding backend + cache
│       ├── projections/        # Reverse index, search index, metrics
│       └── contract.py         # Contract parser + fail-closed gate
│
├── schemas/                    # JSON Schemas (boot, tree, work-order, contract, harness)
├── tests/                      # pytest suite (20+ test files)
├── cli/                        # Click CLI modules
├── migrations/                 # Version upgrade manifests
├── templates/                  # Scaffolding templates
├── docs/                       # Architecture handbook, RFCs, guides
├── tui.py                      # TUI entrypoint (v3.2.0 GA)
├── pyproject.toml              # Package config (v3.2.0)
└── README.md                   # User-facing documentation
```

---

## 11. Tech Stack

| Component | Technology |
|---|---|
| **Language** | Python ≥3.10 |
| **CLI Framework** | Click ≥8.0 |
| **Daemon Transport** | `ThreadingHTTPServer` + JSON-RPC 2.0 (stdlib) |
| **TUI** | Rich ≥13.0 (renderers), stdio input (no external TUI framework) |
| **AST Parser** | LibCST (full-fidelity AST) |
| **Symbol Resolver** | Jedi (cross-file static inference) |
| **Tree-sitter** | Codebase-Memory (`cbm`) multi-language backend |
| **Schema Validation** | jsonschema ≥4.0 |
| **Data Format** | YAML (PyYAML ≥6.0) + Sharded JSON (Knowledge Store) |
| **Build Backend** | Hatchling |
| **Testing** | pytest (20+ test files), Ruff linting |

---

## 12. Key Design Decisions

1. **TUI is a client, never a runtime** — daemon is the sole execution authority; no second runtime path.
2. **Operation-scoped cancellation** — canceling a response cancels only that operation; session survives.
3. **Event sequence numbers as canonical cursor** — monotonic, replayable, single stream for all event types.
4. **File-system as database** — all state lives in deterministic JSON/YAML under `.sync/`; no external DB.
5. **Fail-Closed Contract Layer** — agents cannot query or touch files outside assigned scope.
6. **D025 Destructive Safeguard** — backup-verify-escalate gate before any non-reversible operation.
7. **ScratchWorkspace isolation** — disposable copy, never live directory; escape detection on every path resolution.
8. **Authentic evidence capture** — before/after hashes, diffs, timing, exit codes recorded per operation.
9. **No Node `vm` module** — Python runtime only; no JavaScript sandbox as security boundary.
10. **Stdlib-only daemon transport** — `ThreadingHTTPServer` + `urllib.request`, no external dependencies.

---

## 13. Non-Goals

- No Node.js runtime or `vm` module as security boundary
- No WebSocket transport (JSON-RPC over HTTP for now)
- No external database (file-system persistence only)
- No second execution runtime (TUI is client-only)
- No provider switching mid-execution (backend binding is setup-time, not live)
- No session-level cancellation that kills all operations at once
