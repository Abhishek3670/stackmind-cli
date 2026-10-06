# STACKMIND_CLI
> CLI Frontend, Governed Runtime Kernel & Autonomous Engineering Control Plane for StackMind

**Version:** 3.3.0 GA · **Python:** ≥3.10 · **License:** MIT · **Author:** Abhishek Sharma

---

## Table of Contents
1. [What Is StackMind CLI?](#1-what-is-stackmind-cli)
2. [Quick Start](#2-quick-start)
3. [Architecture & The Single-Source Rule](#3-architecture--the-single-source-rule)
4. [Runtime Kernel & Governance Primitives](#4-runtime-kernel--governance-primitives)
5. [Daemon (JSON-RPC 2.0 & SSE)](#5-daemon-json-rpc-20--sse)
6. [TUI Control Plane Client](#6-tui-control-plane-client)
7. [Execution Backend Abstraction & Dynamic Rebinding](#7-execution-backend-abstraction--dynamic-rebinding)
8. [Harness Runtime & Authentic 6D Verification Gate](#8-harness-runtime--authentic-6d-verification-gate)
9. [CLI Reference](#9-cli-reference)
10. [File Structure](#10-file-structure)
11. [Tech Stack](#11-tech-stack)
12. [Key Design Decisions](#12-key-design-decisions)
13. [Non-Goals](#13-non-goals)

---

## 1. What Is StackMind CLI?

StackMind CLI is the command-line frontend, interactive terminal operating system, and governed multi-agent engineering runtime for the StackMind platform. It provides four tightly integrated capabilities:

| Capability | What It Does | Status |
|---|---|---|
| **Terminal Control Plane (TUI)** | OpenCode-inspired interactive client with Phase Banner, Agent Roles panel, Work Orders panel, Hierarchical Operation Tree, Live Activity Feed, and interactive `:rebind` command | Shipped v3.3.0 GA |
| **Local Runtime Daemon** | Background HTTP JSON-RPC 2.0 daemon with SSE streaming, durable operation tree persistence in `.sync/runtime/daemon/daemon-state.json`, and dynamic role rebinding | Shipped v3.3.0 GA |
| **Execution Backend Abstraction** | Thread-safe `BackendRegistry` (`threading.RLock`) managing model/agent backends (Ollama, local LLMs, echo) with typed fault classification and timeout resilience | Shipped v3.3.0 GA |
| **Harness Runtime & 6D Gate** | Single-Source execution pipeline (`AgentRunner`) with isolated workspace staging and fail-closed authentic 6-stage verification gate | Shipped v3.3.0 GA |

The TUI is an **unprivileged presentation client** — it never becomes a second runtime or bypasses the daemon. All tool execution, model querying, contract validation, and file modifications strictly execute within the governed daemon and harness pipeline.

```text
                 STACKMIND CLI RUNTIME (v3.3.0 GA)
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
┌──────────────┐         JSON-RPC 2.0           ┌──────────────┐
│  TUI Client  │◄──────────────────────────────►│ LocalDaemon  │
│  (cli/tui/)  │   :8765/rpc & SSE /events      │ (validators/ │
│              │                                │  kernel/     │
│ :status      │                                │  daemon/)    │
│ :roles       │                                ├──────────────┤
│ :wo          │                                │SessionManager│
│ :tree        │                                │OperationTree │
│ :plan        │                                │RoleRegistry  │
│ :approve     │                                │DaemonStorage │
│ :rebind      │                                └──────┬───────┘
│ :diff        │                                       │ Single-Source
│ :matrix      │                                       ▼
│ :events      │                                ┌──────────────┐
│ :cancel      │                                │ AgentRunner  │
│ :exit        │                                │ (validators/ │
└──────────────┘                                │  harness/    │
                                                │  runner.py)  │
                                                ├──────────────┤
                                                │ContractGate  │
                                                │Knowledge API │
                                                │Isolated Stage│
                                                │6D Verifier   │
                                                │D025 Gate     │
                                                └──────┬───────┘
                                                       │
                                                       ▼
                                                ┌──────────────┐
                                                │BackendRegist.│
                                                │(RLock Sync)  │
                                                │OllamaBackend │
                                                └──────────────┘
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

### 1. Initialize Workspace & Build Knowledge Graph
```bash
# Initialize governed workspace
stackmind init .

# Build deterministic Code-Graph Intelligence IR
stackmind graph build -p .

# Verify runtime health across all 5 validation layers
stackmind validate .
```

### 2. Start Daemon & Launch Interactive TUI
```bash
# Start the background daemon
stackmind daemon start

# Check daemon health and active session count
stackmind daemon status

# Launch the interactive Terminal Control Plane
stackmind tui
```

### 3. Key TUI Commands & Shortcuts
| Command / Key | Action | Description |
|---|---|---|
| `:status` / `s` | **Phase Banner** | Display overall delivery phase, session ID, and Contract HUD |
| `:roles` / `a` | **Agent Roles Panel** | View real-time status and active backend bindings for all roster agents |
| `:wo` / `w` | **Work Orders Panel** | View active vs completed Work Orders and assigned workers |
| `:tree` / `:agents` | **Operation Tree** | Inspect hierarchical parent-child operation tree (`Session` → `Operation` → `children`) |
| `:plan` / `p` | **Plan Surface** | View proposed architecture plan with HITL approval modal |
| `:approve` / `Ctrl+A` | **HITL Approve** | Formally approve proposed architecture plan to commence worker dispatch |
| `:reject` / `Ctrl+R` | **HITL Reject** | Reject proposed plan and trigger structured revision loop (up to 3 revisions) |
| `:rebind <role> <backend> [model]` | **Dynamic Rebind** | Rebind a role to a model backend (e.g. `:rebind backend ollama ornith-1.5:9b`) |
| `:diff` / `d` | **Diff Viewer** | Inspect staged workspace changes before write-back |
| `:matrix` / `m` | **6D Quality Matrix** | View authentic verification dimensions (Scope, State, AST, Behavioral, Security, Outcome) |
| `:events` / `e` | **Activity Stream** | Stream live sequenced daemon events with monotonic cursors |
| `:completion` / `c` | **Completion Handover**| View final delivery report, test summary, and commit SHAs |
| `Ctrl+C` / `:cancel` | **Cooperative Cancel** | Cancel in-flight operation checkpoint without killing session |
| `:exit` / `:quit` / `q` | **Exit TUI** | Disconnect from TUI (daemon continues running in background) |

---

## 3. Architecture & The Single-Source Rule

StackMind enforces strict separation between client presentation, daemon governance, and harness execution:

### The Single-Source Rule
> **There is exactly one authoritative execution engine in StackMind: `AgentRunner`.**  
> No daemon route, TUI command, or subagent orchestrator may execute prompts or mutate code outside of `AgentRunner.run_once()`. All ad-hoc prompt turns, work-order tasks, and subagent dispatches traverse this single pipeline to ensure universal contract gating, D025 policy checks, and authentic verification.

### Core Architectural Invariants:
1. **Unprivileged Client:** The TUI client has zero file system write privileges and zero shell subprocess capabilities.
2. **Durable Daemon Recovery:** Daemon state (`sessions`, `operations`, `role_bindings`, `journals`) is persisted atomically to `workspace/.sync/runtime/daemon/daemon-state.json`. If restarted, operations and parent-child cancellation trees reconstruct reliably.
3. **Role Normalization:** Logical role names (`backend`, `architect`, `frontend`, `qa`, `gitops`) normalize deterministically to primary roster agents (`codex`, `claude`, `gemini`, `gemma`, `local-llm`), preventing citizenship errors in `TREE.yaml`.
4. **Hierarchical Operation Tree:** Every task executes as a node in an operation tree with parent-child links, cascade cancellation, and aggregated results.
5. **Fail-Closed Verification:** Staged changes execute in a temporary isolated workspace (`tempfile.TemporaryDirectory()`). Live workspace write-back occurs strictly after all 6 verification dimensions pass.

---

## 4. Runtime Kernel & Governance Primitives

The kernel (`validators/kernel/`) provides the core governance primitives:

### Module Map
| Module | Purpose |
|---|---|
| `contract.py` | `AgentContract`, `ContractEvaluator`, `ContractNormalizer` (fail-closed scope gating) |
| `operations.py` | `OperationRequest`, `OperationRecord`, `OperationTree`, `OperationJournal` |
| `daemon/manager.py` | `SessionManager` — session lifecycle, role binding, hierarchical operation dispatch |
| `daemon/server.py` | HTTP JSON-RPC 2.0 server + SSE event stream |
| `subagents.py` | Governed subagent delegation, contract narrowing, and parent lifecycle tracking |
| `security.py` | `CredentialLeakScanner`, D025 destructive safeguards, terminal sanitization, fault injection |
| `evidence.py` | `AuthenticEvidenceTracer`, `VerificationDimensions`, `evaluate_verification_dimensions()` |
| `session.py` | `AgentSession`, `Attempt`, `LifecycleState` state machine |
| `tools.py` | `ToolGateway` — all tool invocations cross policy, contract, and journal boundaries |

### Lifecycle State Machine
```text
CREATED → RUNNING → WAITING → PAUSED → VERIFYING → COMPLETED
                  ↘ FAILED  ↗          ↘ CANCELLED
```

---

## 5. Daemon (JSON-RPC 2.0 & SSE)

The daemon (`validators/kernel/daemon/`) is an HTTP JSON-RPC 2.0 server with an SSE event notification stream:

### RPC Methods
| Method | Params | Result | Description |
|---|---|---|---|
| `session.create` | agent, provider, contract, workspace | session view | Create governed session |
| `session.get` | session_id | session view | Retrieve session state |
| `session.list` | — | [session…] | List all active/historical sessions |
| `session.pause` | session_id | session view | Pause active session |
| `session.resume` | session_id | session view | Resume paused session |
| `session.cancel` | session_id, operation_id? | session view | Cancel session or operation |
| `session.approval` | session_id, approved, reason? | session view | Record HITL plan decision |
| `session.turn` | session_id, prompt | operation record | Execute governed prompt turn |
| `role.configureBackend` | role, backend, model?, credentialRef? | role config | Dynamically bind role to execution backend |
| `role.list` | — | [role config…] | List active role bindings |
| `backend.list` | — | [backend view…] | List registered execution backends |
| `event.list` | session_id, after (seq) | [event…] | Replay events from sequence cursor |

### Endpoints
| Endpoint | Method | Response |
|---|---|---|
| `/health` | GET | `{status: "ok", sessions: N}` |
| `/rpc` | POST | JSON-RPC 2.0 response |
| `/events` | GET | Server-Sent Events (SSE) notification stream |

---

## 6. TUI Control Plane Client

The TUI client (`cli/tui/`) provides an OpenCode-style terminal control surface:

### Views & Panels
- **Phase Status Banner:** Displays current delivery phase (`Planning`, `Autonomous Execution`, `Complete`) with real-time spinners.
- **Agent Roles Panel:** Real-time visibility into all 5 roster agents (`claude`, `codex`, `gemini`, `gemma`, `local-llm`), their current status, and active backend/model bindings.
- **Work Orders Panel:** Live breakdown of active vs completed work orders with priority and assigned agent badges.
- **Hierarchical Operation Tree:** Interactive display of parent-child operation linkages, cancellation tokens, and task durations.
- **Live Activity Feed:** Sequenced timeline of tool calls, contract verifications, and agent events.
- **Plan Surface Modal:** Interactive HITL plan review modal with `:approve` and `:reject` triggers.
- **Completion Handover Surface:** Comprehensive release audit screen showing test pass counts, commit SHAs, and deliverable paths.

---

## 7. Execution Backend Abstraction & Dynamic Rebinding

The execution backend layer (`validators/harness/backend.py`) abstracts model execution behind a unified protocol:

### Backend Hierarchy
- **`BaseExecutionBackend`**: Abstract protocol defining `backend_id`, `backend_type`, `capabilities`, `status`, and `complete(request) -> CompletionRecord`.
- **`AgentExecutionBackend`**: Internal agent role execution backend.
- **`ModelExecutionBackend` (Ollama)**: Live local HTTP model execution backend connecting to `/api/generate` with custom User-Agent and 300-second timeout resilience.
- **`EchoAgentBackend`**: Deterministic test backend.

### Thread-Safe Registry (`WO-026`)
`BackendRegistry` is synchronized with `threading.RLock`. All mutations (`register`, `unregister`) and accessors (`get`, `has`, `list_backends`) acquire the reentrant lock. `list_backends()` creates a defensive snapshot of values under lock before serialization, completely preventing dictionary mutation errors during iteration under concurrent multi-agent traffic.

### Typed Fault Handling (`WO-027`)
`ModelExecutionBackend.complete()` classifies live failures into typed exceptions rather than masking errors as successful completions:
- Connection refused / host unreachable $\rightarrow$ `BackendUnavailableError`
- Network or socket timeout $\rightarrow$ `BackendTimeoutError`
- HTTP 4xx/5xx errors or malformed JSON $\rightarrow$ `BackendExecutionError`
- All exception messages are sanitized with `CredentialLeakScanner` to eliminate credential, token, or local path leakage.

---

## 8. Harness Runtime & Authentic 6D Verification Gate

The Harness runtime (`validators/harness/runner.py`) executes worker turns under strict contract governance:

### Execution Pipeline:
1. **Task & Scope Resolution**: Resolve task from inbox, active work order, or ad-hoc prompt turn.
2. **Contract Pre-Gate**: Validate task target paths against active Contract YAML `allow` and `deny` rules.
3. **Knowledge Context Assembly**: Retrieve ranked, token-budgeted context bundle via KNOW-01 Knowledge API.
4. **Isolated Staging**: Execute task in an isolated temporary directory (`tempfile.TemporaryDirectory()`).
5. **Authentic 6D Verification Gate (`WO-028`)**: Evaluate all 6 verification dimensions:
   - **`scope_verified`**: Staged diff verified against contract allow/deny path rules.
   - **`state_verified`**: Lock file state and file hash consistency verified.
   - **`code_verified`**: AST parsing (`ast.parse`) executed across all modified `.py` files and test return codes verified.
   - **`behavioral_verified`**: Assert all executed commands succeeded (returncode 0).
   - **`security_verified`**: D025 safeguards enforced, path traversals blocked, and diffs scanned for credentials.
   - **`outcome_verified`**: Deliverable existence and zero unhandled blockers verified.
6. **Fail-Closed Write-Back**: Live workspace write-back via `_apply_verified_workspace_diff()` occurs strictly when all 6 dimensions pass. If any check fails, the staging directory is discarded.

---

## 9. CLI Reference

```powershell
# --- Daemon Management ---
stackmind daemon start [--port 8765]      # Start local runtime daemon
stackmind daemon status                   # Inspect active daemon status and sessions
stackmind daemon stop                     # Stop running daemon process
stackmind daemon restart                  # Restart running daemon

# --- Terminal User Interface ---
stackmind tui                             # Launch interactive TUI control plane
stackmind tui --demo                      # Run multi-agent autonomous delivery simulation

# --- Repository Governance & Integrity ---
stackmind init [path]                     # Initialize governed StackMind runtime workspace
stackmind validate [path]                 # Validate all 5 runtime integrity layers
stackmind validate --fix [path]           # Auto-normalize canonical drift in TREE.yaml
stackmind doctor [path]                   # Environment, dependency, and agent health diagnostics
stackmind shutdown <agent>                # Graceful agent session termination and handoff archive
stackmind lock acquire|release|status     # Advisory repository write lock

# --- Knowledge Graph Operations ---
stackmind graph build -p .                # Compile workspace into deterministic graph IR
stackmind graph update -p .               # Incremental update after code modifications
stackmind graph query "<symbol>" -p .     # Look up symbols without scanning source files
stackmind callers "<symbol>" -p .         # Find static and runtime-traced callers
stackmind impact "<symbol>" -p .          # Analyze blast radius of changes across graph
stackmind context "<task>" -p .           # Assemble token-bounded, ranked prompt context bundle

# --- Harness & Procedural Learning ---
stackmind harness run-once                # Run single governed harness execution cycle
stackmind learn mine -p .                 # Mine recurring execution patterns into skill candidates
stackmind skill list                      # View active and candidate procedural skills
stackmind skill test <name>               # Execute 3-stage skill verification pipeline
stackmind experience search "<query>"     # Search past verified execution episodes (EXP-*)
```

---

## 10. File Structure

```text
stackmind/
├── cli/                        # Click CLI & TUI entrypoints
│   ├── main.py                 # Root CLI group & command registrations
│   ├── daemon.py               # stackmind daemon (start, stop, restart, status)
│   ├── tui/                    # Terminal Control Plane application
│   │   ├── app.py              # OpenCode-style Textual/Rich TUI application
│   │   ├── client.py           # DaemonClient JSON-RPC & SSE client
│   │   ├── views/              # Views (banner, roles, work orders, tree, activity)
│   │   └── commands.py         # TUI command dispatcher (:rebind, :roles, :plan, etc.)
│   ├── graph.py                # Knowledge graph commands
│   ├── contract.py             # Contract inspection & denial explainer
│   ├── harness.py              # Harness run-once command
│   ├── validate.py             # 5-layer runtime validator
│   └── shutdown.py             # Session termination & receipt writing
│
├── validators/
│   ├── kernel/                 # Runtime governance kernel
│   │   ├── contract.py         # AgentContract, ContractEvaluator, ContractNormalizer
│   │   ├── operations.py       # OperationRequest, OperationRecord, OperationTree
│   │   ├── subagents.py        # Governed subagent delegation & contract narrowing
│   │   ├── security.py         # CredentialLeakScanner, D025 safeguards, sanitization
│   │   ├── evidence.py         # AuthenticEvidenceTracer, VerificationDimensions
│   │   ├── daemon/             # JSON-RPC daemon (server, protocol, manager, storage)
│   │   └── session.py          # AgentSession, Attempt, LifecycleState
│   │
│   ├── harness/                # Governed execution runtime
│   │   ├── runner.py           # AgentRunner — Single-Source execution pipeline
│   │   ├── backend.py          # ExecutionBackend, BackendRegistry, ModelExecutionBackend
│   │   ├── contract_gate.py    # Pre/post execution contract validation
│   │   ├── d025_gate.py        # D025 destructive operations safeguard
│   │   ├── retrieval.py        # Multi-signal context retrieval
│   │   └── snapshot.py         # WorkspaceSnapshot & WorkspaceDiff
│   │
│   └── knowledge/              # Knowledge compiler (Pillar 2)
│       ├── compiler/           # 14 domain AST compilers
│       ├── embedding/          # Embedding backend & cache
│       └── projections/        # Reverse index, search index, metrics
│
├── schemas/                    # JSON Schemas (boot, tree, work-order, contract, harness)
├── tests/                      # Automated test suite (85 P7 tests / 570+ suite tests)
├── pyproject.toml              # Package configuration (v3.3.0)
├── VERSION.md                  # Canonical version manifest (3.3.0)
└── CHANGELOG.md                # Release history & milestone notes
```

---

## 11. Tech Stack

| Component | Technology |
|---|---|
| **Language** | Python ≥3.10 (tested on Python 3.11 & 3.12) |
| **CLI Framework** | Click ≥8.0 |
| **TUI Control Plane** | Rich ≥13.0, custom OpenCode presentation surface |
| **Daemon Transport** | `ThreadingHTTPServer` + JSON-RPC 2.0 (stdlib) + SSE |
| **Concurrency & Synchronization** | `threading.RLock`, `threading.Barrier`, `threading.Event` |
| **Model Integration** | Native HTTP POST to Ollama `/api/generate` with timeout resilience |
| **AST Analysis** | LibCST & `ast.parse` |
| **Schema Validation** | jsonschema ≥4.0 |
| **Testing** | pytest (85 P7 tests / 570+ full suite tests, 100% pass rate) |

---

## 12. Key Design Decisions

1. **Single-Source Rule**: Exactly one execution engine (`AgentRunner`). All prompts, commands, and subagent turns traverse the same governed pipeline.
2. **Client-Daemon Separation**: The TUI client has zero execution privileges; all state mutation lives inside the daemon.
3. **Fail-Closed Verification**: Staged changes execute in a disposable temporary directory; write-back is blocked unless all 6 verification dimensions pass.
4. **Thread-Safe Backend Registry**: Registry operations are guarded by `threading.RLock` with defensive snapshot iteration.
5. **Durable Operation Tree**: Daemon crashes or restarts reconstruct active operations, role bindings, and cancellation checkpoints from `.sync/runtime/daemon/daemon-state.json`.
6. **Zero-Leakage Security**: Proactive regex and entropy scanning redacts credentials from RPC payloads, journals, and event streams.

---

## 13. Non-Goals

- No direct shell execution from TUI client (client is presentation-only)
- No un-governed tool loops (all tools cross `ToolGateway` and contract boundaries)
- No JavaScript/Node.js dependency (pure Python stdlib runtime)
- No external database requirement (file-system persistence under `.sync/`)
- No session-wide hard aborts (targeted cooperative cancellation checkpointing)

---

*StackMind CLI v3.3.0 GA — Enterprise Multi-Role Engineering Runtime Built for Absolute Reliability.*
