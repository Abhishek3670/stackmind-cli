# StackMind CLI/TUI — Final Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`
**Target branch:** `feat/p6-open-source-tui`
**Status:** Implementation-ready canonical plan
**Scope:** P6 client foundation + StackMind governed multi-agent delivery workflow
**Architecture basis:** Legacy StackMind governance + Knowledge Compiler + Harness model, modernized for external agent execution backends
**Primary rule:** Do not create a second StackMind runtime inside the TUI.
**Delivery rule:** Prove the runtime cancellation fix before building the interactive TUI surface. Prove the P6 vertical slice before building any P7 capability.

---

## 1. Objective

Build a production-quality StackMind CLI with an OpenCode-like TUI that acts as the **user-facing control plane** for the StackMind runtime.

The TUI is not the coding runtime. StackMind is the governance/orchestration layer between the user-facing interface and the actual agent execution backends.

The intended product workflow is:

1. The user submits one initial project objective.
2. The Architecture / StackMind Orchestrator Agent studies the project and produces `PLAN.md`.
3. The user reviews and explicitly approves the plan.
4. StackMind autonomously creates and dispatches governed Work Orders to Backend, Frontend, Q/A, and GitOps Agent Roles.
5. Those roles execute through configured backends such as **Codex, AGY, Claude, or local Ollama models**.
6. StackMind governs every operation through contracts, policy, Knowledge API, Harness validation, approvals, cancellation, persistence, and audit.
7. The system delivers the completed project and updated `README.md`.

### 1.1 Three-user-interaction delivery contract

```text
Interaction 1
─────────────
User submits the initial project objective.

Interaction 2
─────────────
Architecture Agent produces PLAN.md.
User reviews and approves/rejects the plan.

Autonomous execution
────────────────────
StackMind dispatches and governs the remaining work.
No repeated "what next?" prompts are required for normal execution.

Interaction 3
─────────────
The system presents final project delivery and completion summary.
```

A mandatory governance/approval gate may still interrupt autonomous execution for a destructive or policy-controlled action. That is an exceptional safety checkpoint, not the normal workflow.

### 1.2 Canonical architecture

```text
                              USER
                               │
                               ▼
                    ┌────────────────────┐
                    │   STACKMIND TUI    │
                    │ OpenCode-like UX   │
                    └─────────┬──────────┘
                              │ JSON-RPC
                              ▼
                 ┌──────────────────────────┐
                 │     STACKMIND RUNTIME    │
                 │  GOVERNANCE + CONTROL    │
                 ├──────────────────────────┤
                 │ Architecture Orchestrator│
                 │ Contracts / Work Orders  │
                 │ Knowledge API / Graph    │
                 │ Harness / Verification   │
                 │ Policy / D025            │
                 │ Approvals                │
                 │ Operation Tree           │
                 │ Audit / Persistence      │
                 └────────────┬─────────────┘
                              │
                       Agent Role binding
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
             Agent Role           Execution Backend
                    │          ┌────────┬────────┬────────┐
                    │          │ Codex  │  AGY   │ Claude │
                    │          │ Ollama │ Future │        │
                    │          └────────┴────────┴────────┘
                    ▼
             Tools / Workspace
```

### 1.3 Terminology

| Concept | Meaning |
|---|---|
| **TUI** | User-facing terminal interface |
| **StackMind Runtime** | Governance, orchestration, Knowledge API, Harness, lifecycle, verification, audit |
| **Agent Role** | Logical responsibility such as Architecture, Backend, Frontend, Q/A, GitOps |
| **Execution Backend** | Actual agent/model implementation used by a role: Codex, AGY, Claude, Ollama, etc. |
| **Provider** | Backend connectivity/configuration and credential source; not a logical agent role |
| **Work Order** | Governed unit of engineering work derived from the approved plan |
| **Contract** | Identity, scope, permissions, budgets, and policy constraints attached to work |
| **Operation** | Runtime execution instance with identity, lifecycle, cancellation, events, and parent/child relation |
| **Subagent** | Child operation/agent role created by the orchestrator; never a TUI-side simulation |

### 1.4 Legacy StackMind foundation

The TUI/P6/P7 work preserves StackMind's original three-pillar model:

```text
Runtime Governance & Contract Layer
            +
Knowledge Compiler & Graph Intelligence
            +
Harness Runtime & Verification
```

The new client surface does not replace those pillars; it exposes them through a governed interactive workflow.

---

# 2. Current Codebase Baseline

The implementation must start from the actual code in `feat/p6-open-source-tui`, not from assumptions.

Relevant runtime modules identified during review:

| File | Current responsibility | P6 change | P7 change |
|---|---|---|---|
| `validators/kernel/daemon/server.py` | `ThreadingHTTPServer`, `/health`, `/rpc`, `/mcp` | Harden lifecycle/transport integration; add notification-capable connection path | No new transport; same connection path carries agent/tool/provider notifications |
| `validators/kernel/daemon/protocol.py` | JSON-RPC validation and lifecycle methods | Add TUI RPC contract, typed errors, handshake, request dispatch | Add `provider.*` and `agent.*` method families |
| `validators/kernel/daemon/manager.py` | Session ownership, operations, cancellation, journaling | Separate operation cancellation from terminal session cancellation | Extend operation identity into a tree (parent/child) to represent subagents |
| `validators/kernel/daemon/events.py` | In-process replayable events with sequence numbers | Make event cursor semantics canonical for replay/resume | Add `agent.*` and `tool.*` event types to the same sequenced stream — no second stream |
| `validators/kernel/daemon/storage.py` | Atomic JSON persistence for sessions/events | Preserve persistence abstraction | Persist operation tree and provider selection alongside existing session/event records |
| `validators/kernel/session.py` | Rich session lifecycle enum/state transitions | Align RPC-visible lifecycle with the richer runtime state machine | Add session-level `activeProvider`/`activeModel` metadata fields |
| `validators/kernel/boundary.py` | Provider/runtime operation authorization boundary | Keep as the privileged execution boundary | Extend to a **provider registry** behind the same boundary; add authorization checks for subagent spawn/cancel |
| `tests/test_daemon_runtime.py` | Daemon/session/event/cancellation/recovery tests | Extend for operation execution, streaming, cancellation and reconnect | Extend for operation trees, provider switching, subagent cancellation cascades |
| `pyproject.toml` | Python package/runtime dependencies | Add only dependencies justified by transport architecture | Add only dependencies justified by a specific, named provider adapter |
| `README.md` | TUI claims and user-facing commands | Rewrite claims to match shipped behavior | Document coding/provider/subagent commands only once implemented and tested |

Important current-state observations (carried over from v4, still true):

1. The daemon currently exposes `/rpc` over `ThreadingHTTPServer`.
2. JSON-RPC currently covers session lifecycle/event retrieval but does not provide the complete prompt/turn API required by the TUI.
3. `event.list` is replay/polling, not true server-push streaming.
4. Current session cancellation is terminal session cancellation; it is not equivalent to cancelling one in-flight operation.
5. `events.py` already provides monotonic event sequencing and replay semantics. Reuse that model instead of inventing a second ordering system — this applies to agent and tool events too.
6. `session.py` contains richer lifecycle semantics than the currently exposed daemon RPC.
7. Persistence is atomic JSON and is acceptable for the first implementation, but event persistence must remain replaceable.
8. Existing tests cover important daemon behavior, but there is no complete end-to-end interactive TUI test path, and there is currently **no** multi-step tool loop, provider abstraction, or subagent concept anywhere in the exposed RPC surface. These are new capabilities, not hidden existing ones.

---

# 3. Non-Negotiable Architecture Rules

1. **TUI is a client, not a runtime.**
2. **StackMind is the governance authority.**
3. **Agent Roles are logical responsibilities; Codex/AGY/Claude/Ollama are execution backends.**
4. **No direct provider/backend calls from the TUI.**
5. **No direct tool execution from the TUI.**
6. **All privileged actions cross the StackMind daemon/runtime boundary.**
7. **The Architecture Agent is the default top-level project orchestrator.**
8. **Normal execution after plan approval is autonomous.**
9. **Every worker operation has an explicit identity, Contract, Work Order, lifecycle, and audit trail.**
10. **Knowledge retrieval is contract-gated and should use StackMind's Knowledge API rather than unrestricted scanning.**
11. **Every coding tool call uses the same authorization → execution → verification → journal/event boundary.**
12. **D025 applies regardless of which agent or backend initiates a destructive action.**
13. **Cancellation is operation-scoped; cancelling one operation does not terminate the session or unrelated siblings.**
14. **Parent cancellation cascades to child operations according to the operation-tree rules.**
15. **Child contracts can never exceed parent contract scope.**
16. **The TUI may request approval; it does not decide authorization.**
17. **Provider credentials remain daemon-side and never cross the TUI boundary.**
18. **Execution backend selection is a runtime concern, not a TUI concern.**
19. **All agent/tool/provider/subagent events use one canonical sequenced stream.**
20. **Reconnect must reconstruct observable runtime state from persisted state/events.**
21. **No backend receives an authority bypass because it is trusted, local, or well-known.**
22. **No new backend may bypass Contracts, Knowledge scope, policy, verification, or audit.**
23. **P6 must be proven before P7 autonomous delivery capabilities are implemented.**

---

# 4. Target User Flow

## 4.1 First launch and configuration

Running:

```bash
stackmind
```

opens the TUI. On first setup, it collects configuration for these logical Agent Roles:

```text
Architecture / StackMind Orchestrator
Backend
Frontend
Q/A
GitOps
```

Each role is mapped to an execution backend and configuration:

```text
Agent Role
  ├── execution backend
  ├── base URL
  ├── credential reference
  └── model
```

Example mappings are illustrative, not fixed:

```text
Architecture → Claude / AGY / Codex
Backend      → Codex
Frontend     → AGY / Codex
Q/A          → Ollama
GitOps       → Ollama
```

The daemon owns credentials. The TUI displays provider/model identity and status only.

## 4.2 Automated agent initialization

Legacy StackMind required manual boot instructions. The CLI must automate the same or a better initialization flow.

For each role, the runtime constructs the equivalent of:

```text
You are agent <agent_name> on the StackMind project at this directory.
Read AGENTS.md.
Boot from .sync/runtime/boot/<agent_name>.boot.yaml.
Process your unread inbox at .sync/inbox/<agent_name>/.
```

The generated bootstrap additionally carries:

```text
Project
Agent Role
Work Order
Contract
Knowledge scope
Tool permissions
Budget
Execution backend
Parent operation (if applicable)
```

The user does not manually initialize five agents.

## 4.3 Interaction 1 — Initial project objective

```text
User
 ↓
TUI
 ↓
Architecture / StackMind Orchestrator
 ↓
Knowledge API + project state
 ↓
requirements + architecture + work decomposition
 ↓
PLAN.md
```

## 4.4 Interaction 2 — Plan approval

The TUI presents `PLAN.md` as the primary governance artifact:

```text
PLAN.md
──────────────────────────────
Status: AWAITING_APPROVAL

[ Approve ]   [ Reject ]
```

Approval crosses the daemon boundary. On approval:

```text
Approved PLAN.md
       ↓
Architecture Orchestrator
       ↓
Work Orders + Contracts
       ↓
┌─────────┬──────────┬─────────┬─────────┐
Backend   Frontend    Q/A      GitOps
```

## 4.5 Autonomous execution

The Architecture Agent orchestrates; StackMind governs; worker roles execute.

```text
PLAN.md
  ↓
Work Order graph
  ↓
StackMind governance
  ↓
Agent role assignment
  ↓
Execution backend
  ↓
Tool operations
  ↓
Validation / QA
  ↓
Promotion / GitOps
```

The TUI continuously renders the operation state:

```text
PROJECT: my-app
STATUS: EXECUTING

AGENTS
✓ Architecture      orchestrating
● Backend           implementing
● Frontend          implementing
○ Q/A               waiting
○ GitOps            waiting

WORK ORDERS
✓ WO-001 Architecture
● WO-002 Backend API
● WO-003 Frontend
○ WO-004 Q/A
○ WO-005 GitOps
```

Normal worker progress requires no additional user direction.

## 4.6 Interaction 3 — Final project handover

When all required Work Orders pass their completion and verification criteria:

```text
Architecture Agent
   ↓
Final verification
   ↓
README.md update
   ↓
Project delivery
```

The TUI presents the final summary, changed files, test status, verification state, and Git result.

---

# 5. Capability Contract

| Capability | TUI responsibility | StackMind runtime responsibility | Execution backend |
|---|---|---|---|
| Initial project request | Collect/display objective | Create orchestrator operation | Architecture backend |
| PLAN.md generation | Render progress/plan | Knowledge assembly, architecture reasoning, plan artifact | Architecture backend |
| Plan approval | Collect explicit approval | Authorize plan → Work Orders transition | N/A |
| Backend implementation | Render status/diffs/tests | Contract, Work Order, tool authorization, verification, audit | Codex / AGY / Claude / local |
| Frontend implementation | Render status/diffs/tests | Same governed execution path | Codex / AGY / Claude / local |
| Q/A | Render validation state | Review/validation and verdicts | Ollama / Codex / Claude / other |
| GitOps | Render release state | Promotion, D025, commit/release governance | Ollama / dedicated backend |
| Backend configuration | Present setup/status UX | Store/resolve backend configuration and credentials | Backend-specific |
| Subagent orchestration | Render tree and controls | Spawn/authorize/cancel/aggregate child operations | Any configured backend |
| Reconnect | Reconstruct UI state | Persist/replay canonical runtime state | N/A |

**Core principle:** TUI presents and requests; StackMind governs and orchestrates; execution backends perform the work.

---

# 6. Phase P6-0 — Targeted Runtime Preflight

P6-0 is intentionally **small and time-boxed**. The repository/runtime has already been inspected; this phase exists only to re-confirm the facts that P6-1 depends on before changing cancellation semantics.

## Goal

Validate the current cancellation and operation paths immediately before the lifecycle refactor.

## Time box

Target: **30–60 minutes**.

Do not spend this phase designing the TUI, choosing a new transport, evaluating OpenTUI, designing providers/subagents, or polishing documentation.

## Verify only these runtime facts

Confirm the current implementation and call sites for:

- `SessionManager.cancel_session()`;
- `SessionManager.begin_operation()`;
- `SessionManager.complete_operation()`;
- active-operation storage and lookup;
- ownership/lifetime of the cancellation `threading.Event`;
- session state transitions performed during cancellation;
- operation journal entries;
- operation/session events emitted during cancellation and completion;
- persistence/recovery behavior for an active or cancelled operation;
- tests covering cancellation and restart recovery;
- the real runtime/provider/tool caller that owns the active operation.

The preflight should answer five questions:

```text
1. Where is an operation created?
2. Where is its cancellation handle stored?
3. What exact code path currently makes a session terminal?
4. Which component observes cancellation and stops work?
5. Which events/journal records define the terminal result?
```

## Deliverable

Create or update a short implementation note containing:

```text
operation creation → active-operation registration
operation execution → cancellation observation
cancel request → cancellation signal
operation exit → terminal state
session lifecycle → resumability
events/journal → observable history
```

Record any discrepancy between the reviewed baseline and the branch at implementation time.

## Exit criteria

- The five questions above are answered from code.
- P6-1 has identified the exact methods/tests/callers it must change.
- No new architectural work is introduced into P6-0.
- Existing relevant tests pass before modification.

---

# 7. Phase P6-1 — Correct the Runtime Operation Model

This is the most important backend change, and the single hard blocker for everything else in this document, including all of P7.

## Problem

The current `SessionManager.cancel_session()` cancels the active operation and then transitions the session itself to terminal `CANCELLED`.

That behavior is unsuitable for an interactive chat UI where:

> cancel this response

must mean:

> stop the current operation but keep the session available for another message.

It is even more unsuitable for P7, where a single session may have many concurrent operations (subagents) — session-level cancellation would kill all of them at once when only one was meant to stop.

## Required model

Separate:

```text
Session lifecycle
```

from:

```text
Operation lifecycle
```

Recommended operation states:

```text
REQUESTED
AUTHORIZED
RUNNING
COMPLETED
FAILED
CANCEL_REQUESTED
CANCELLED
```

A session can remain:

```text
RUNNING / WAITING / PAUSED
```

after an operation is cancelled.

Terminal session cancellation should remain available as a separate explicit lifecycle action.

## Required manager changes

Refactor the manager around explicit operation identity:

```python
operation_id
session_id
request_id
```

Do not overload `session_id` as the operation identity.

Required semantics:

### Begin

```text
begin_operation(session_id, operation_name, metadata)
    -> operation_id + cancellation handle
```

### Cancel

```text
cancel_operation(operation_id)
```

This should:

1. mark cancellation requested;
2. signal the cancellation event;
3. journal the request;
4. allow the runtime operation to stop;
5. emit a terminal operation event;
6. leave the session resumable.

### Complete

Completion must be idempotent with respect to cancellation:

```text
CANCEL_REQUESTED + operation exits
    -> CANCELLED
```

and not:

```text
CANCEL_REQUESTED + operation exits
    -> COMPLETED
```

unless cancellation was requested after the operation had already reached a terminal state.

## Concurrency

Protect mutable manager state.

At minimum, synchronize:

- session lookup/update;
- active operation registration;
- cancellation lookup;
- operation completion;
- event/journal updates;
- persistence snapshots.

Do not hold a global lock while executing provider/tool work.

## Exit criteria

Tests prove:

- operation cancellation does not terminate the session;
- a new operation can start afterward;
- cancellation is race-safe;
- completion and cancellation cannot corrupt state;
- daemon restart preserves valid session state.

## P6-1 milestone gate

**Do not start the TUI vertical slice until this gate passes. Do not start any P7 phase until this gate passes.**

The runtime must have a demonstrably correct operation-scoped cancellation model before the TUI — or subagents, which are additional operations — are allowed to depend on it.

```text
P6-0 targeted preflight
        ↓
P6-1 operation lifecycle + cancellation
        ↓
P6-2 RPC contract
        ↓
P6-3 streaming / event protocol
        ↓
P6-4 replay / reconnect
        ↓
P6-5 typed adapter
        ↓
P6-6 OpenTUI feasibility spike
        ↓
P6-7 core interactive TUI
        ↓
   [P6 Definition of Done + Final Acceptance Scenario]
        ↓
P7-0 operation tree model
        ↓
P7-1 agentic coding loop
        ↓
P7-2 provider runtime layer
        ↓
P7-3 subagent orchestration
        ↓
P7-4 TUI surfaces for coding/providers/subagents
        ↓
P7-5 security hardening for agentic execution
        ↓
P7-6 P7 acceptance
```

---

# 8. Phase P6-2 — Define the Versioned JSON-RPC Contract

Use JSON-RPC 2.0 as the logical contract.

Do not implement gRPC as a parallel protocol.

## Protocol version

Start with:

```text
protocolVersion = 1
```

P7 introduces `protocolVersion = 2` (Section 16) — additive, not breaking — once P7 methods exist. Protocol version must be independently tracked from StackMind package version.

## Handshake

Expose:

```text
health.version
```

Result (P6):

```json
{
  "version": "3.2.0",
  "protocolVersion": 1,
  "capabilities": {
    "streaming": true,
    "cancel": true,
    "tools": true,
    "history": true
  }
}
```

Result (P7, once shipped):

```json
{
  "version": "3.3.0",
  "protocolVersion": 2,
  "capabilities": {
    "streaming": true,
    "cancel": true,
    "tools": true,
    "history": true,
    "agenticCoding": true,
    "providers": true,
    "subagents": true
  }
}
```

A client that does not declare support for `agenticCoding`, `providers`, or `subagents` must never receive those event types or be offered those commands — this is a capability-negotiated surface, not an always-on one.

The exact package version must be read from the project version source rather than hard-coded in the RPC handler.

Unsupported protocol versions must return a structured protocol mismatch error.

---

# 9. Phase P6-3 — RPC Methods (P6 baseline)

## `health.version`

Returns daemon version and protocol capabilities. No authentication-sensitive information.

## `session.create`

Request: `{ "metadata": {}, "model": "optional-model" }`
Result: `{ "sessionId": "sess-...", "createdAt": "...", "metadata": {} }`

Only expose fields actually supported by the runtime. Do not invent a `userId` authorization model if the daemon does not currently have one.

## `session.resume`

Request: `{ "sessionId": "sess-..." }`
Result: `{ "sessionId": "sess-...", "resumedAt": "..." }`

Resume must not create a second session.

## `session.list`

Request: `{ "filter": {}, "limit": 50, "offset": 0 }`
Result: `{ "sessions": [] }`

Never return an unbounded session list.

## `session.history`

Request: `{ "sessionId": "sess-...", "cursor": "...", "limit": 100 }`
Result: `{ "events": [], "nextCursor": "..." }`

History and live events must use a compatible sequence/cursor model.

## `session.close`

Explicitly terminates/closes the session. Distinct from cancelling a single request.

## `session.sendMessage`

Request: `{ "sessionId": "sess-...", "text": "...", "messageId": "...", "options": { "stream": true } }`
Immediate result: `{ "requestId": "req-...", "accepted": true }`

The daemon must not hold the RPC request open until the entire assistant response finishes.

## `tool.execute`

Only expose this if the existing daemon has a valid authorized tool boundary for it.

Request: `{ "sessionId": "sess-...", "toolId": "...", "input": {}, "requestId": "req-..." }`

The daemon remains responsible for authorization, policy, contract, sandbox, argument validation, and execution.

## `request.cancel`

Request: `{ "requestId": "req-...", "sessionId": "sess-...", "reason": "user_cancelled" }`
Result: `{ "requestId": "req-...", "canceled": true }`

`canceled: true` means cancellation was accepted/requested, not necessarily that execution has already stopped. The final event confirms actual termination.

---

# 10. Phase P6-4 — Streaming/Event Protocol (P6 baseline)

Use server-to-client JSON-RPC notifications.

The existing `RuntimeEvent.sequence` model remains the canonical event ordering mechanism. Do not create a second independent stream counter — this rule is what lets P7 add new event types onto the same stream later without a second reconnect/replay system.

## Assistant stream event

```json
{
  "jsonrpc": "2.0",
  "method": "event.streamChunk",
  "params": {
    "requestId": "req-1",
    "sessionId": "sess-1",
    "seq": 42,
    "role": "assistant",
    "chunkText": "hello",
    "complete": false
  }
}
```

Terminal event:

```json
{
  "jsonrpc": "2.0",
  "method": "event.streamChunk",
  "params": {
    "requestId": "req-1",
    "sessionId": "sess-1",
    "seq": 43,
    "role": "assistant",
    "chunkText": "",
    "complete": true,
    "status": "completed"
  }
}
```

## Required event properties

Every event carries: `sessionId`, `seq`, `type`, and a terminal/non-terminal marker. P7 event types (Section 15) follow this exact same envelope.

## Reconnect invariant

A client that reconnects and replays from its last known `seq` must reconstruct an identical event history to one that never disconnected, at any depth of the operation tree.

---

# 11. Phase P6-5 — Tool Event Model (P6 baseline)

Tool execution emits at least: a running event, a result event (success/failure), and structured output rather than free text where possible. P7-1 reuses this exact event pair (`event.toolCall` / `event.toolResult`) for every step of the coding loop — it does not introduce a new tool-event shape.

---

# 12. Phase P6-6 — RPC Error Contract

Errors are structured, typed, and never raw stack traces. Minimum error categories: invalid params, session not found, operation not found, policy denied, tool failure, protocol mismatch, internal error. P7 adds: provider not found, provider unauthorized, subagent scope exceeds parent contract, subagent not found — all following the same structured shape.

---

# 13. Phase P6-7 — Transport Decision

## Default recommendation

Keep HTTP + JSON-RPC with a notification-capable connection (e.g. long-lived SSE/streaming response or WebSocket upgrade on the existing server) rather than introducing a second transport.

## Decision gate

Verify before committing: does the existing `ThreadingHTTPServer` support a long-lived streaming connection cleanly, or is a minimal addition (e.g. WebSocket) justified? Record the decision and rationale. P7 does not revisit this decision — the same transport carries agent/tool/provider notifications.

---

# 14. Phase P6-8 — `StackMindTuiAdapter`

## Adapter responsibilities

Manage connection lifecycle, marshal requests, validate responses, convert streamed notifications into async events, translate daemon errors into UI-safe errors, and keep transport details out of UI components.

## Retry rules

Idempotent reads (`session.list`, `health.version`) may retry with backoff. Non-idempotent writes (`session.sendMessage`, `tool.execute`, and in P7, `agent.spawn`, `provider.setActive`) must never be silently retried.

P7 extends the adapter's typed surface with `spawnAgent`, `cancelAgent`, `inspectAgent`, `listProviders`, `setActiveProvider` — implemented as thin wrappers over the same request/response and notification plumbing already built in P6. No new adapter architecture is introduced.

---

# 15. Phase P6-9 — OpenTUI Feasibility Gate

Evaluate `@opentui/react` against Bun/Zig/native build requirements, CI compatibility, packaging complexity, and streaming performance, with Ink/Blessed as a pure-TypeScript fallback. The adapter must stay framework-independent regardless of outcome — this is what allows the P7 UI surfaces (Section 19) to be built without revisiting this decision.

---

# 16. Phase P6-10 — Core TUI

Build only after the client vertical slice works without the UI.

## Required layout

```text
┌─────────────────────────────────────────────────────────────┐
│ StackMind • Session sess-abc123                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ You                                                         │
│ Explain this project                                        │
│                                                             │
│ StackMind                                                    │
│ Here is how the system works...                             │
│ ▌ streaming                                                 │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│ > Type your message...                                      │
└─────────────────────────────────────────────────────────────┘
```

## Required features

Text input, Enter to send, streaming assistant output, user/assistant/tool/system roles, scrolling, keyboard navigation, focus management, resize handling, cancellation shortcut, connection status, request status, new-message indicator when scrolled away from bottom.

## Rendering rules

Pure rendering where possible; no blocking network calls in render code; coalesce tiny stream chunks; auto-scroll only when user is already at bottom; preserve scroll position when historical content is inserted.

---

# 17. Phase P6-11 — Session Management UI

Implement `:new`, `:resume`, `:close` and corresponding controls. Session browser shows session ID, last activity, metadata, lifecycle state. Use `session.history(cursor, limit)` and virtualized rendering — never load unlimited history into memory.

---

# 18. Phase P6-12 — Reconnect & Fault UX

Handle daemon-unavailable, connection reset, RPC timeout, malformed response, protocol mismatch, policy denial, tool execution failure, interrupted generation, and user cancellation, all without dumping raw stack traces. Reconnect enters a visible reconnecting state, replays from last sequence, and deduplicates before returning to live stream.

---

# 19. Phase P6-13 — Security Hardening (P6 baseline)

## Trust boundary

The TUI must not become a new code-execution environment. All privileged execution remains in the daemon / ToolGateway / Sandbox.

## Requirements

Authenticate TUI → daemon where appropriate; sanitize terminal-rendered content; prevent arbitrary ANSI/escape-sequence injection; validate RPC arguments; enforce authorization server-side; never use Node's `vm` module as a security boundary.

## Terminal sanitization

Strip or escape control sequences from any model, tool, or (in P7) subagent/provider output before it reaches the terminal renderer.

---

# 20. Phase P6-14 — Concurrency & Persistence

Server concurrency: synchronized manager state as in Section 7. Persistence: atomic JSON is acceptable for P6 and P7-0; event persistence must remain swappable without changing the RPC contract.

---

# 21. Phase P6-15 — Performance

Benchmark large histories (~1000 messages/lines), avoid rendering full history unnecessarily, use virtualized scrolling, keep heavy work off the event loop, apply stream backpressure, bound memory used by retained chat data. P7 adds: bound memory/rendering cost of a wide operation tree (many concurrent subagents), not just a long linear history.

---

# 22. Phase P6-16 — Logging

Local logging captures RPC calls, errors, and lifecycle transitions without leaking secrets. P7 extends this to provider identifiers (never credentials) and subagent lifecycle transitions.

---

# 23. Phase P6-17 — Testing Strategy (P6 baseline)

Backend unit tests, RPC integration tests, adapter tests, real daemon integration, TUI tests, security tests, fault injection — all as defined in v4. P7's testing additions are in Section 20 below and follow the same categories.

---

# 24. Phase P6-18 — CLI Integration

Intended commands: `stackmind daemon start`, `stackmind tui`. The TUI must discover/connect to the intended daemon, provide actionable daemon-unavailable errors, avoid silently starting an unauthorized second runtime, exit cleanly, and preserve session state in the daemon. Existing CLI commands must continue to work. P7 adds no new top-level commands — `:provider`, `:model`, and the subagent tree view are in-TUI, not new CLI entry points.

---

# 25. Phase P6-19 — Documentation (P6 baseline)

Update README TUI section, architecture diagram, RPC contract, protocol version, transport, authentication, capability negotiation, keyboard shortcuts, session management, cancellation behavior, reconnect behavior, troubleshooting, security model, framework/build requirements, CHANGELOG — only after implementation behavior is real. Remove or qualify any README statement that claims functionality not covered by tests. P7 documentation is Section 21.

---

## P6 is now complete as specified in v4. Everything below is new in v5.

---

# 26. Phase P7-0 — Governed Agent Roles & Operation Tree

## Goal

Extend the P6-1 operation model into the legacy StackMind concept of logical Agent Roles executing governed Work Orders, while representing nested work as a parent/child operation tree. A subagent is a child operation under a parent operation and Contract.

## Required model

```text
Session
  └── Operation (top-level, e.g. "implement JWT auth")
        ├── operation_id
        ├── parent_operation_id: null
        ├── contract_scope
        └── children: [Operation, Operation, ...]
              each child Operation:
                ├── operation_id
                ├── parent_operation_id: <top-level id>
                ├── agent_id (stable label, e.g. "backend")
                ├── role (e.g. "coder", "planner", "tester")
                └── contract_scope  (subset of parent's contract_scope)
```

Reuse the identical states from P6-1 (`REQUESTED, AUTHORIZED, RUNNING, COMPLETED, FAILED, CANCEL_REQUESTED, CANCELLED`) for every node.

## Cascade rules

- Cancelling a parent operation issues `cancel_operation` to every non-terminal child, then waits for children to reach a terminal state before marking the parent `CANCELLED`.
- Cancelling a child operation must not affect its parent or siblings.
- A parent cannot reach `COMPLETED` while any child is non-terminal.
- Contract scope is checked at spawn time: a child's requested scope is authorized against the intersection with its parent's scope, never a wider grant (Rule 21, Section 3).

## Required manager changes

```python
begin_operation(session_id, operation_name, metadata, parent_operation_id=None, contract_scope=None)
    -> operation_id + cancellation handle

cancel_operation(operation_id, cascade=True)

list_children(operation_id) -> [operation_id, ...]
```

## Concurrency

The same synchronization primitives from P6-1 apply, extended to cover parent/child registration and cascade cancellation as atomic-enough sequences (no child can be "orphaned" mid-cascade).

## Exit criteria

Tests prove:

- a child operation can be created under a running parent;
- cancelling a parent cascades correctly to all children;
- cancelling one child does not affect siblings or the parent;
- a child cannot be authorized with a wider contract scope than its parent;
- a parent cannot complete while children are still running;
- daemon restart preserves a valid, non-corrupted operation tree.

---

# 27. Phase P7-1 — Autonomous Governed Agent Execution

## Goal

Implement the real StackMind Harness behavior behind an approved `PLAN.md`.

This is what turns the TUI from a chat surface into an autonomous engineering delivery interface.

## Required execution model

```text
Approved PLAN.md
      ↓
Architecture / StackMind Orchestrator
      ↓
Work Orders
      ↓
Contracts
      ↓
Knowledge-gated context
      ↓
Execution backend
      ↓
Tool actions
      ↓
Verification
      ↓
Promotion / next Work Order
```

## Harness behavior

Preserve the legacy governed loop:

```text
1. Receive Work Order
2. Validate Contract
3. Assemble context via Knowledge API
4. Invoke execution backend
5. Validate backend result
6. Validate staged diff against Contract and D025
7. Write back only after successful validation
8. Emit audit/events
9. Update Work Order / operation state
```

## Required project artifacts

```text
PLAN.md
Work Orders
Contracts
agent boot/snapshot state
audit receipts
verification results
README.md
```

## Completion semantics

A backend success response does not automatically mean a Work Order is complete. Completion requires the configured validation/verification criteria to pass.

## Exit criteria

- Approved `PLAN.md` produces persistent Work Orders.
- Every Work Order resolves to an Agent Role and execution backend.
- Work Orders execute autonomously after approval.
- Knowledge context is contract-gated.
- Tool calls remain daemon-governed.
- D025 is enforced.
- Failed validation prevents promotion.
- Work Order state is persisted and observable.
- A representative full-stack task proceeds without repeated user direction.

---

# 28. Phase P7-2 — Execution Backend Abstraction & Configuration

## Goal

Separate **logical StackMind Agent Roles** from the **execution backends** that implement them.

## Required abstraction

```text
Agent Role
    ↓
Contract + Work Order
    ↓
Backend Adapter
    ↓
Execution Backend
```

Example role mappings:

```text
Architecture → Claude / AGY / Codex
Backend      → Codex
Frontend     → AGY / Codex
Q/A          → Ollama
GitOps       → Ollama
```

These are configurable examples, not hard-coded assignments.

## Backend adapter contract

Each backend adapter supports at least:

```text
start operation
send/continue work
stream events
request/resolve approval
cancel
inspect
report completion/failure
```

## Configuration

First-run setup must support:

```text
agent role
execution backend
base URL
credential reference
model
```

The runtime validates configuration before dispatch.

## Rules

- Backend selection is a runtime decision.
- The selected backend is captured in the operation record.
- Backend identity does not change the Agent Role's Contract.
- Backend failure produces a structured failure.
- No silent fallback to another backend.
- Backend adapters cannot bypass Harness/Contract/verification.
- Credentials are never exposed to the TUI.

## Exit criteria

- At least two real execution backends can be configured and exercised.
- The same Agent Role can map to different backends without TUI changes.
- No credential appears in RPC responses, events, logs, or UI.
- Backend failure semantics are deterministic.

---

# 29. Phase P7-3 — Work Order Dispatch & Subagent Orchestration

## Goal

Make the legacy multi-agent Work Order workflow first-class in the runtime.

The Architecture Agent generates/decomposes Work Orders; specialized Agent Roles execute them under StackMind governance.

## Required flow

```text
Architecture Agent
      ↓
Work Order graph
      ↓
Backend / Frontend / Q/A / GitOps roles
      ↓
child operations
      ↓
execution backends
```

## Required orchestration rules

- Every child operation has `parent_operation_id`.
- Every child has an explicit Agent Role.
- Every child receives a Work Order and Contract.
- Child scope is a subset of parent authority.
- Child results are aggregated into the parent.
- A parent cannot complete while required children remain non-terminal.
- A failed required child prevents false parent success.
- Cancelling a child affects only its subtree.
- Cancelling a parent cascades to non-terminal children.
- Reconnect reconstructs the tree from persisted state/events.

## Required controls

```text
list agents/work orders
inspect operation
cancel operation
pause/resume where supported
show current backend
show contract/work-order status
```

## Exit criteria

- Architecture can dispatch Backend, Frontend, Q/A, and GitOps Work Orders.
- Child operations inherit governance correctly.
- One child can be cancelled independently.
- Parent cancellation cascades correctly.
- Parent result aggregates child results.
- Full tree survives reconnect/restart.

---

# 30. Phase P7-4 — TUI Surfaces for Autonomous Delivery

Build the TUI around project delivery state, not an endless interactive prompt loop.

## Required project view

```text
STACKMIND • my-project
────────────────────────────────────────────────────

PHASE
✓ PLAN approved
● Autonomous Execution

AGENTS
✓ Architecture      orchestrating
● Backend           implementing
● Frontend          implementing
○ Q/A               waiting
○ GitOps            waiting

WORK ORDERS
✓ WO-001 Architecture
● WO-002 Backend API
● WO-003 Frontend
○ WO-004 Q/A
○ WO-005 GitOps

ACTIVITY
12:41 Backend  read auth/service.py
12:42 Backend  edit auth/service.py
12:43 Frontend create LoginPage.tsx
12:44 Backend  pytest
```

## Plan approval surface

`PLAN.md` is the primary governance artifact and must expose its status, work decomposition, dependencies, risks, validation, and approval controls.

## Agent/backend surface

Show both logical role and backend:

```text
Backend Agent
  Role: Backend
  Backend: Codex
  Work Order: WO-002
  State: RUNNING
```

## Operation tree

```text
● Architecture
  ├─ ● Backend / Codex
  ├─ ● Frontend / AGY
  ├─ ○ QA / Ollama
  └─ ○ GitOps / Ollama
```

## Completion surface

```text
PROJECT COMPLETE

PLAN.md ✓
Backend ✓
Frontend ✓
Q/A ✓
GitOps ✓
README.md ✓
Tests ✓
Git ✓
```

## Exit criteria

A user can:

1. submit the initial objective;
2. review and approve `PLAN.md`;
3. watch autonomous delivery;
4. inspect agent/work-order/backend state;
5. respond to mandatory governance approval when one occurs;
6. receive final project handover.

Normal worker execution requires no manual steering.

---

# 31. Phase P7-5 — Security Hardening for Agentic Execution

## Trust boundary

Unchanged in principle from P6 (Section 19): the TUI must not become a new code-execution environment. P7 adds surface area, not exceptions.

## Requirements

- Every tool call inside the coding loop is authorized and sandboxed exactly as a single P6 `tool.execute` call — no "trusted mode" for agent-originated calls.
- Subagent contract scope is checked at spawn time against the parent's scope (Rule 21); this check itself needs a positive and negative test.
- Provider credentials never cross the RPC boundary in either direction; a leak-detection test asserts no RPC response or log line ever contains a configured provider key/token pattern.
- D025 destructive safeguards apply identically inside and outside the agentic loop, and to every subagent, not just the top-level operation.
- Terminal sanitization (P6-13) is verified against subagent and provider output specifically, not only top-level assistant output.

## Security testing

Extend the P6 security suite with: subagent scope-escalation attempts, provider-credential leak scanning, runaway-loop budget enforcement, and approval-gate bypass attempts.

## Exit criteria

No P7 feature can bypass daemon policy, exceed its parent's contract scope, or directly execute untrusted code on the host — the same guarantee P6 established, verified again at every new capability.

---

# 32. Phase P7-6 — Testing Strategy for P7

Extending the P6 categories (Section 23) with:

- **Operation tree tests**: spawn/cancel/cascade correctness (Section 26).
- **Coding loop integration tests**: representative tasks run end-to-end against a mock tool sandbox; budget enforcement; approval-gate flow.
- **Provider adapter tests**: each adapter tested against a contract/interface test suite, independent of any specific provider's live API; `provider.list`/`provider.setActive` tested for the unavailable/misconfigured case.
- **Subagent RPC tests**: `agent.list`/`agent.cancel`/`agent.inspect` correctness, including reconnect reconstructing full tree state.
- **Fault injection**: subagent crash mid-execution, provider timeout mid-call, cascade-cancel racing with a child's natural completion.

---

# 33. Phase P7-7 — Documentation

Add, only once real: coding-loop behavior and approval prompts; provider configuration (server-side credential setup) and the `:provider`/`:model` commands; subagent tree UI and per-agent cancel/inspect; updated architecture diagram matching Section 1; updated protocol version and capability list (Section 8).

---

# 34. Updated RPC Method Summary

| Method | Phase | Notes |
|---|---|---|
| `health.version` | P6-3 | Now also reports `agenticCoding`/`providers`/`subagents` capabilities |
| `session.create` | P6-3 | unchanged |
| `session.resume` | P6-3 | unchanged |
| `session.list` | P6-3 | unchanged |
| `session.history` | P6-3 | unchanged |
| `session.close` | P6-3 | unchanged |
| `session.sendMessage` | P6-3 | `options.mode` may now request `agentic` execution (P7-1) |
| `tool.execute` | P6-3 | Also used internally by the P7-1 coding loop, unchanged shape |
| `request.cancel` | P6-3 | unchanged |
| `request.approve` | P7-1 | new — resolves an `AWAITING_APPROVAL` operation |
| `provider.list` | P7-2 | new |
| `provider.setActive` | P7-2 | new |
| `agent.list` | P7-3 | new |
| `agent.cancel` | P7-3 | new |
| `agent.inspect` | P7-3 | new |

`agent.spawn` is a daemon-internal decision surfaced only through events (`event.agentSpawned`), not a directly user-invocable RPC method — this keeps subagent creation a runtime planning decision rather than a UI action, consistent with Rule 1.

---

# 35. Updated Explicit Non-Goals

Carried over from v4, all still true and now explicitly extended to cover P7:

- rewrite the entire StackMind runtime;
- replace the existing session architecture wholesale;
- introduce a database solely because the TUI exists;
- introduce gRPC alongside JSON-RPC;
- embed OpenCode as a second runtime;
- **allow TUI components to call provider APIs directly** — still forbidden; provider execution is exclusively daemon-side even after P7-2;
- **allow TUI components to execute tools directly** — still forbidden, including every tool used by the P7-1 coding loop;
- add arbitrary remote access;
- **build a general distributed, multi-user daemon** — subagents in P7-3 are in-process operation-tree nodes within one daemon and one user's sessions; this is not multi-user or distributed execution, and building that remains out of scope;
- introduce speculative plugin architecture — provider adapters (P7-2) are a fixed, reviewed internal interface, not an open plugin system;
- optimize before measuring;
- replace HTTP with sockets without a demonstrated requirement;
- **new — let the TUI decide when to spawn a subagent**; that decision is the daemon's runtime/planning logic, not a client-triggered action;
- **new — let a subagent acquire a contract scope wider than its parent's**, under any circumstance.

---

# 36. Updated Definition of Done

P6 sections (Architecture, Runtime, RPC, Client, TUI, Security, Quality) are unchanged from v4 and must still hold. Add:

### Agentic coding

- A representative multi-step coding task completes end-to-end with zero direct tool execution from the TUI.
- Every step is individually authorized, sandboxed, and journaled.
- D025-gated actions correctly pause for approval and resume only on explicit approval.
- Runaway loops self-terminate within budget.

### Providers

- `provider.list` and `provider.setActive` work against at least two real adapters.
- No credential material ever appears in an RPC response, event, or log (verified by an automated leak-detection test).
- Provider switching is rejected mid-operation and accepted between operations.

### Subagents

- The operation tree correctly represents parent/child relationships and survives reconnect.
- Cancelling a subagent never affects its siblings or parent; cancelling a parent cascades correctly.
- No subagent can be authorized with a contract scope wider than its parent's.

---

# 37. Updated Final Acceptance Scenario

The P6 scenario (v4, Section 29) must still pass in full, unmodified, as the prerequisite gate. Append:

```text
21. Run "implement JWT authentication" as an agentic coding task.
22. Observe plan/tool/diff/test status render live.
23. Trigger a D025-gated action and confirm the approval prompt blocks progress until approved.
24. Approve it and confirm the operation completes.
25. Switch provider via :provider between tasks and confirm the new provider is used.
26. Attempt to switch provider mid-operation and confirm it is rejected.
27. Run a task that spawns multiple subagents; confirm the tree renders live.
28. Cancel one subagent and confirm siblings and the parent are unaffected.
29. Cancel the parent and confirm cascade-cancellation of all remaining children.
30. Disconnect and reconnect mid-multi-agent-task; confirm the full operation tree is correctly reconstructed.
31. Run the full CI suite, including the P7 security and fault-injection additions, successfully.
```

This extended scenario is the P7 release gate, itself gated behind the unmodified P6 scenario passing first.

---

# 38. Updated PR Breakdown

P6 rows (P6-0 through P6-14) are unchanged from v4. Add:

| PR | Scope | Exit condition |
|---|---|---|
| P7-0 | Operation tree model | Cascade cancel/spawn/scope tests pass |
| P7-1 | Agentic coding loop | Representative coding task passes end-to-end with budget/approval enforcement |
| P7-2 | Provider runtime layer | Two adapters pass the provider contract test suite; no credential leakage |
| P7-3 | Subagent RPC | `agent.list/cancel/inspect` correct; reconnect reconstructs full tree |
| P7-4 | TUI surfaces (coding/provider/subagent) | Manual + smoke test of Section 30 flows |
| P7-5 | Agentic security hardening | Extended security suite passes |
| P7-6 | P7 CI additions | All new test categories run in CI |
| P7-7 | P7 documentation | Docs match shipped behavior |
| P7-8 | P7 final acceptance | Full extended E2E scenario (Section 37) passes |

Do not combine P7 backend, adapter, and UI changes into one PR, for the same review-size reasons as P6.

---

# 39. Updated Implementation Order

```text
P6-0 → P6-1 → P6-2 → P6-3 → P6-4 → P6-5 → P6-6 → P6-7 → P6-8 → P6-9 → P6-10 → P6-11 → P6-12 → P6-13 → P6-14
                                                                                                     │
                                                          [P6 Definition of Done + Acceptance Scenario]
                                                                                                     │
                                                                                                     ▼
                                                P7-0 → P7-1 → P7-2 → P7-3 → P7-4 → P7-5 → P7-6 → P7-7 → P7-8
```

The key implementation principle, unchanged from v4 and now extended one level:

> **Prove the daemon/client vertical slice before building the visual interface. Prove the P6 vertical slice, fully, before building any P7 capability.**

The first working P7 milestone is not a polished coding/provider/subagent UI. It is:

```text
Daemon
  ↕
Operation tree (parent + child operations)
  ↕
One coding task, driven headlessly, with tool calls authorized and journaled
  ↕
One provider switch, applied and reflected in session metadata
  ↕
One subagent, spawned, cancelled independently, and reconstructed on reconnect
```

Once that is proven without any UI involved, the coding surface, provider selector, and subagent tree become frontend implementation work over an already-correct daemon — exactly the same discipline that made the P6 TUI a frontend exercise rather than an architectural experiment.

# 40. Canonical Architecture Decision

The final plan is a synthesis of the legacy StackMind core and the new CLI/TUI workflow.

```text
USER
  ↓
TUI
  ↓
StackMind Governance + Orchestration
  ├── Contracts
  ├── Work Orders
  ├── Knowledge Compiler / Knowledge API
  ├── Harness
  ├── Verification
  ├── D025
  ├── Operation lifecycle
  ├── Audit / persistence
  └── Agent routing
          ↓
      Agent Role
          ↓
   Execution Backend
   ┌──────┬──────┬──────┬──────┐
  Codex  AGY   Claude Ollama Future
```

StackMind is **not merely a provider router** and the TUI is **not the agent runtime**. StackMind is the governed engineering runtime between the user-facing agent interface and heterogeneous execution backends.

The legacy fixed role-to-vendor arrangement becomes configurable role-to-backend binding while preserving the original governance semantics: Contracts, Work Orders, Knowledge API, Harness validation, QA/review, promotion, and D025.

---

