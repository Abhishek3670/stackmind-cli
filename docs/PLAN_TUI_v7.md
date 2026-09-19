# StackMind CLI/TUI — Final Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`
**Target branch:** `feat/p6-open-source-tui`
**Status:** Implementation-ready canonical plan
**Scope:** P6 client foundation + StackMind governed multi-agent delivery workflow + Harness/backend integration
**Architecture basis:** Legacy StackMind governance + Knowledge Compiler + Harness model, modernized for external agent execution backends
**Primary rule:** Do not create a second StackMind runtime inside the TUI.
**Delivery rule:** Prove the runtime cancellation fix before building the interactive TUI surface. Prove the P6 vertical slice before building any P7 capability.

### What changed from v6

v6 correctly reframed the plan around StackMind's real vocabulary (Agent Role / Execution Backend / Provider / Work Order / Contract) but left several later sections copy-pasted from the earlier chat-only draft, so they still referred to a live `:provider` switch and generic "coding loop" language that no longer matched the redesigned phases. This version:

- replaces every stale `provider.*` RPC reference with the actual Agent-Role → Execution-Backend binding model (Section 28, Section 34);
- adds the RPC methods the plan described in prose but never specified (`plan.approve` / `plan.reject`, `role.list` / `role.configureBackend`, `backend.list`);
- defines what happens when a plan is **rejected** (Section 4.4), which v6 left unspecified;
- adds an explicit preflight to P7-2 (Section 28) to determine whether execution-backend abstraction is new runtime work or exposure of something that already exists — this matters because of the "do not rewrite the entire StackMind runtime" non-goal;
- clarifies that P6's chat UI is a proving milestone, not the final product surface (Section 1);
- rewrites the Final Acceptance Scenario, Definition of Done, Non-Goals, and PR breakdown so they test the actual v6/v7 workflow instead of the old provider-switch scenario.
- separates durable Work Order identity from runtime Operation identity;
- makes the existing `AgentRunner`/Harness the single-source execution path;
- distinguishes agent-native backends (Codex/AGY/Claude) from model backends (Ollama/local models).

Nothing about the underlying engineering (operation tree, cascade cancellation, D025, sandboxing) changes — this is a consistency and completeness pass, not a redesign.

---

## 1. Objective

Build a production-quality StackMind CLI with an OpenCode-like TUI that acts as the **user-facing control plane** for the StackMind runtime.

The TUI is not the coding runtime. StackMind is the governance/orchestration layer between the user-facing interface and the actual agent execution backends.

**Staged delivery note:** P6 ships a minimal interactive chat interface (Section 16) whose purpose is to prove the client/daemon RPC contract end-to-end — it is a proving milestone, not the intended end-user product. P7-4 (Section 30) supersedes it with the project-delivery-oriented UI described below. The underlying chat RPC surface (`session.sendMessage`, streaming, cancellation) remains available for ad hoc interaction alongside the project view; it does not disappear, but it is not the primary interface once P7 ships.

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
| **Provider** | Backend connectivity/configuration and credential source; not a logical agent role, and not separately RPC-addressable — it is configured as part of an Execution Backend (Section 28) |
| **Work Order** | Persistent governed unit of engineering work derived from the approved plan; execution is represented by one or more runtime Operations/Attempts (Section 26) |
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
| `validators/kernel/daemon/server.py` | `ThreadingHTTPServer`, `/health`, `/rpc`, `/mcp` | Harden lifecycle/transport integration; add notification-capable connection path | No new transport; same connection path carries agent/tool/backend notifications |
| `validators/kernel/daemon/protocol.py` | JSON-RPC validation and lifecycle methods | Add TUI RPC contract, typed errors, handshake, request dispatch | Add `plan.*`, `role.*`, `backend.*`, and `agent.*` method families |
| `validators/kernel/daemon/manager.py` | Session ownership, operations, cancellation, journaling | Separate operation cancellation from terminal session cancellation | Extend operation identity into a tree (parent/child) to represent Work Orders/subagents |
| `validators/kernel/daemon/events.py` | In-process replayable events with sequence numbers | Make event cursor semantics canonical for replay/resume | Add `agent.*`, `tool.*`, and `plan.*` event types to the same sequenced stream — no second stream |
| `validators/kernel/daemon/storage.py` | Atomic JSON persistence for sessions/events | Preserve persistence abstraction | Persist operation tree, `PLAN.md` status, and role→backend bindings alongside existing session/event records |
| `validators/kernel/session.py` | Rich session lifecycle enum/state transitions | Align RPC-visible lifecycle with the richer runtime state machine | Add session-level `planStatus` metadata field |
| `validators/kernel/boundary.py` | Provider/runtime operation authorization boundary | Keep as the privileged execution boundary | Extend to a **backend registry** behind the same boundary; add authorization checks for role→backend binding and subagent spawn/cancel |
| `tests/test_daemon_runtime.py` | Daemon/session/event/cancellation/recovery tests | Extend for operation execution, streaming, cancellation and reconnect | Extend for operation trees, role/backend binding, plan approval, subagent cancellation cascades |
| `pyproject.toml` | Python package/runtime dependencies | Add only dependencies justified by transport architecture | Add only dependencies justified by a specific, named backend adapter |
| `README.md` | TUI claims and user-facing commands | Rewrite claims to match shipped behavior | Document plan/role/backend/subagent commands only once implemented and tested |

Important current-state observations (carried over, still true):

1. The daemon currently exposes `/rpc` over `ThreadingHTTPServer`.
2. JSON-RPC currently covers session lifecycle/event retrieval but does not provide the complete prompt/turn API required by the TUI.
3. `event.list` is replay/polling, not true server-push streaming.
4. Current session cancellation is terminal session cancellation; it is not equivalent to cancelling one in-flight operation.
5. `events.py` already provides monotonic event sequencing and replay semantics. Reuse that model instead of inventing a second ordering system — this applies to agent, tool, and plan events too.
6. `session.py` contains richer lifecycle semantics than the currently exposed daemon RPC.
7. Persistence is atomic JSON and is acceptable for the first implementation, but event persistence must remain replaceable.
8. Existing tests cover important daemon behavior, but there is no complete end-to-end interactive TUI test path, and there is currently **no** multi-step Work Order loop, backend abstraction, or subagent concept anywhere in the exposed RPC surface. These are new capabilities, not hidden existing ones — Section 28 requires confirming exactly how new before implementation.

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
18. **Execution backend selection is a runtime/configuration concern, bound to an Agent Role at setup time or between assignments — never a live mid-Work-Order TUI action.**
19. **All agent/tool/backend/plan/subagent events use one canonical sequenced stream.**
20. **Reconnect must reconstruct observable runtime state — including `PLAN.md` status and role→backend bindings — from persisted state/events.**
21. **No backend receives an authority bypass because it is trusted, local, or well-known.**
22. **No new backend may bypass Contracts, Knowledge scope, policy, verification, or audit.**
23. **P6 must be proven before P7 autonomous delivery capabilities are implemented.**
24. **New — a `PLAN.md` rejection never silently discards the objective; it returns to the Architecture Agent for revision (Section 4.4).**
25. **New — no Work Order may claim completion while a required child operation is non-terminal or has failed.**

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

**Reconfiguring a role's backend after initial setup** is a between-assignments action — it is only accepted by the daemon when that Agent Role has no non-terminal Work Order in flight (Section 28). It is not a live in-chat command; there is no `:provider` or `:backend` switch mid-execution. A read-only `:roles` command in the TUI shows current bindings and status at any time.

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

Approval crosses the daemon boundary via `plan.approve` (Section 27). On approval:

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

**Rejection path** (previously unspecified): rejection crosses the boundary via `plan.reject`, optionally carrying free-text feedback. Rejection never creates Work Orders. The Architecture Agent receives the feedback (if any), revises `PLAN.md`, and re-submits it as a new `AWAITING_APPROVAL` version. The plan's revision history is retained and inspectable; a rejected version is never silently discarded (Rule 24, Section 3).

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
| Plan approval / rejection | Collect explicit approve/reject + optional feedback | Authorize plan → Work Orders transition, or route feedback back to Architecture Agent for revision | N/A |
| Backend implementation | Render status/diffs/tests | Contract, Work Order, tool authorization, verification, audit | Codex / AGY / Claude / local |
| Frontend implementation | Render status/diffs/tests | Same governed execution path | Codex / AGY / Claude / local |
| Q/A | Render validation state | Review/validation and verdicts | Ollama / Codex / Claude / other |
| GitOps | Render release state | Promotion, D025, commit/release governance | Ollama / dedicated backend |
| Role/backend configuration | Present setup/status UX; read-only `:roles` view | Store/resolve role→backend bindings and credentials; reject rebinding while a Work Order is in flight | Backend-specific |
| Subagent orchestration | Render tree and controls | Spawn/authorize/cancel/aggregate child operations | Any configured backend |
| Reconnect | Reconstruct UI state | Persist/replay canonical runtime state, including plan status and bindings | N/A |

**Core principle:** TUI presents and requests; StackMind governs and orchestrates; execution backends perform the work.

---

# 6. Phase P6-0 — Targeted Runtime Preflight

P6-0 is intentionally **small and time-boxed**. The repository/runtime has already been inspected; this phase exists only to re-confirm the facts that P6-1 depends on before changing cancellation semantics.

## Goal

Validate the current cancellation and operation paths immediately before the lifecycle refactor.

## Time box

Target: **30–60 minutes**.

Do not spend this phase designing the TUI, choosing a new transport, evaluating OpenTUI, designing backends/subagents, or polishing documentation.

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

It is even more unsuitable for P7, where a single session may have many concurrent operations (Work Orders/subagents) — session-level cancellation would kill all of them at once when only one was meant to stop.

## Identity model

A Work Order is a persistent project-governance artifact. An Operation is a runtime execution/attempt used to carry out that Work Order or a delegated child task.

| Object | Purpose | Example |
|---|---|---|
| Plan | Project planning revision | `plan-003` |
| Work Order | Durable governed assignment | `WO-002` |
| Operation | Runtime execution/attempt | `op-9a2...` |
| Agent Role | Logical responsibility | `backend` |

A retry of `WO-002` retains `work_order_id` and creates a distinct `operation_id`.

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

The runtime must have a demonstrably correct operation-scoped cancellation model before the TUI — or Work Orders/subagents, which are additional operations — are allowed to depend on it.

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
P7-0 governed agent roles + operation tree
        ↓
P7-1 governed Work Order harness loop + plan approval
        ↓
P7-2 execution backend abstraction (post-preflight)
        ↓
P7-3 Work Order dispatch + subagent orchestration
        ↓
P7-4 TUI surfaces for autonomous delivery
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

P7 introduces `protocolVersion = 2` — additive, not breaking — once P7 methods exist. Protocol version must be independently tracked from StackMind package version.

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
    "governedWorkOrders": true,
    "executionBackends": true,
    "subagents": true
  }
}
```

A client that does not declare support for `governedWorkOrders`, `executionBackends`, or `subagents` must never receive those event types or be offered those commands — this is a capability-negotiated surface, not an always-on one.

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

Every event carries: `sessionId`, `seq`, `type`, and a terminal/non-terminal marker. P7 event types follow this exact same envelope.

## Reconnect invariant

A client that reconnects and replays from its last known `seq` must reconstruct an identical event history to one that never disconnected, at any depth of the operation tree.

---

# 11. Phase P6-5 — Tool Event Model (P6 baseline)

Tool execution emits at least: a running event, a result event (success/failure), and structured output rather than free text where possible. P7-1's Work Order harness loop reuses this exact event pair (`event.toolCall` / `event.toolResult`) for every step — it does not introduce a new tool-event shape.

---

# 12. Phase P6-6 — RPC Error Contract

Errors are structured, typed, and never raw stack traces. Minimum error categories: invalid params, session not found, operation not found, policy denied, tool failure, protocol mismatch, internal error. P7 adds: execution backend not found, execution backend unauthorized, role binding not configured, role binding rejected (Work Order in flight), plan not awaiting approval, subagent scope exceeds parent contract, subagent not found — all following the same structured shape.

---

# 13. Phase P6-7 — Transport Decision

## Default recommendation

Keep HTTP + JSON-RPC with a notification-capable connection (e.g. long-lived SSE/streaming response or WebSocket upgrade on the existing server) rather than introducing a second transport.

## Decision gate

Verify before committing: does the existing `ThreadingHTTPServer` support a long-lived streaming connection cleanly, or is a minimal addition (e.g. WebSocket) justified? Record the decision and rationale. P7 does not revisit this decision — the same transport carries agent/tool/backend/plan notifications.

---

# 14. Phase P6-8 — `StackMindTuiAdapter`

## Adapter responsibilities

Manage connection lifecycle, marshal requests, validate responses, convert streamed notifications into async events, translate daemon errors into UI-safe errors, and keep transport details out of UI components.

## Retry rules

Idempotent reads (`session.list`, `health.version`, `role.list`, `backend.list`) may retry with backoff. Non-idempotent writes (`session.sendMessage`, `tool.execute`, and in P7, `plan.approve`, `plan.reject`, `role.configureBackend`, `agent.cancel`) must never be silently retried.

P7 extends the adapter's typed surface with `approvePlan`, `rejectPlan`, `listRoles`, `configureRoleBackend`, `listBackends`, `listAgents`, `cancelAgent`, `inspectAgent` — implemented as thin wrappers over the same request/response and notification plumbing already built in P6. No new adapter architecture is introduced.

---

# 15. Phase P6-9 — OpenTUI Feasibility Gate

Evaluate `@opentui/react` against Bun/Zig/native build requirements, CI compatibility, packaging complexity, and streaming performance, with Ink/Blessed as a pure-TypeScript fallback. The adapter must stay framework-independent regardless of outcome — this is what allows the P7 UI surfaces (Section 30) to be built without revisiting this decision.

---

# 16. Phase P6-10 — Core TUI

Build only after the client vertical slice works without the UI. This is a proving milestone for the RPC contract, not the intended end-user surface (see Section 1's staged delivery note).

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

Strip or escape control sequences from any model, tool, subagent, or execution-backend output before it reaches the terminal renderer.

---

# 20. Phase P6-14 — Concurrency & Persistence

Server concurrency: synchronized manager state as in Section 7. Persistence: atomic JSON is acceptable for P6 and P7-0; event persistence must remain swappable without changing the RPC contract.

---

# 21. Phase P6-15 — Performance

Benchmark large histories (~1000 messages/lines), avoid rendering full history unnecessarily, use virtualized scrolling, keep heavy work off the event loop, apply stream backpressure, bound memory used by retained chat data. P7 adds: bound memory/rendering cost of a wide operation tree (many concurrent Work Orders/subagents), not just a long linear history.

---

# 22. Phase P6-16 — Logging

Local logging captures RPC calls, errors, and lifecycle transitions without leaking secrets. P7 extends this to execution-backend identifiers and role bindings (never credentials), and to plan approval/rejection events.

---

# 23. Phase P6-17 — Testing Strategy (P6 baseline)

Backend unit tests, RPC integration tests, adapter tests, real daemon integration, TUI tests, security tests, fault injection. P7's testing additions are in Section 32 and follow the same categories.

---

# 24. Phase P6-18 — CLI Integration

Intended commands: `stackmind daemon start`, `stackmind tui` (equivalently `stackmind`, per Section 4.1). The TUI must discover/connect to the intended daemon, provide actionable daemon-unavailable errors, avoid silently starting an unauthorized second runtime, exit cleanly, and preserve session state in the daemon. Existing CLI commands must continue to work. P7 adds no new top-level CLI commands: `:roles` (read-only bindings view) and the subagent/Work Order tree view are in-TUI; reconfiguring a role's backend is a setup-time action (Section 4.1), not a command.

---

# 25. Phase P6-19 — Documentation (P6 baseline)

Update README TUI section, architecture diagram, RPC contract, protocol version, transport, authentication, capability negotiation, keyboard shortcuts, session management, cancellation behavior, reconnect behavior, troubleshooting, security model, framework/build requirements, CHANGELOG — only after implementation behavior is real. Remove or qualify any README statement that claims functionality not covered by tests. P7 documentation is Section 33.

---

## P6 is complete as specified above. Everything below is the P7 capability expansion.

---

# 26. Phase P7-0 — Governed Agent Roles & Operation Tree

## Goal

Extend the P6-1 operation model into the legacy StackMind concept of logical Agent Roles executing governed Work Orders, while representing nested work as a parent/child operation tree. A subagent is a child operation under a parent operation and Contract.

## Required model

```text
Session
  └── Operation (top-level execution of a Work Order, e.g. "implement JWT auth")
        ├── operation_id
        ├── work_order_id
        ├── parent_operation_id: null
        ├── contract_scope
        └── children: [Operation, Operation, ...]
              each child Operation (== child Work Order):
                ├── operation_id
                ├── parent_operation_id: <top-level id>
                ├── agent_id (stable label, e.g. "backend")
                ├── role (e.g. "coder", "planner", "tester")
                └── contract_scope  (subset of parent's contract_scope)
```

`work_order_id` and `operation_id` are deliberately distinct identifiers. A Work Order is durable project governance; an Operation is one runtime execution/attempt. A retry may retain the same Work Order while creating a new Operation and execution history.

Reuse the identical states from P6-1 (`REQUESTED, AUTHORIZED, RUNNING, COMPLETED, FAILED, CANCEL_REQUESTED, CANCELLED`) for every node.

## Cascade rules

- Cancelling a parent operation issues `cancel_operation` to every non-terminal child, then waits for children to reach a terminal state before marking the parent `CANCELLED`.
- Cancelling a child operation must not affect its parent or siblings.
- A parent cannot reach `COMPLETED` while any required child is non-terminal or has failed (Rule 25, Section 3).
- Contract scope is checked at spawn time: a child's requested scope is authorized against the intersection with its parent's scope, never a wider grant (Rule 15, Section 3).

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
- a parent cannot complete while required children are still running or have failed;
- daemon restart preserves a valid, non-corrupted operation tree;
- a retried Work Order retains its `work_order_id` while receiving a distinct `operation_id` and distinct execution history.

---

# 27. Phase P7-1 — Harness Integration, Work Order Execution & Plan Approval

## Goal

Integrate the daemon Operation lifecycle with the **existing StackMind Harness Runtime** rather than creating a second execution engine.

The repository already contains a governed `AgentRunner`/Harness flow with an `LLMProvider` protocol, Knowledge API context assembly, pre/post Contract validation, staged workspace verification, D025 evaluation, write locking, diff analysis, experience recording, and persistence. P7-1 must expose and orchestrate this existing capability through the daemon/Work Order lifecycle instead of duplicating it.

This is what turns the TUI from a chat surface into an autonomous engineering delivery interface.

## Harness integration boundary

```text
Daemon Work Order
       ↓
Operation lifecycle
       ↓
Harness integration
       ↓
existing AgentRunner / governed execution
       ↓
Knowledge API
       ↓
Contract validation
       ↓
Execution Backend
       ↓
Tool/workspace operations
       ↓
D025 + diff + verification
       ↓
Work Order + Operation result
```

### Required integration checks

Before implementation, verify:

1. Which existing `AgentRunner` methods can be invoked by the daemon.
2. Which state exists in `.sync/*` artifacts versus daemon memory.
3. How `HarnessTask.work_order_id` maps to daemon `operation_id`.
4. How the P6-1 cancellation signal reaches an executing `AgentRunner`.
5. How Harness verification/persistence results become RuntimeEvents without duplicating journals.
6. How the existing `LLMProvider` abstraction should be generalized for execution backends.

### Single-source execution requirement

There must be **one authoritative governed execution path**.

Do not create an independent daemon coding loop while leaving the existing CLI `AgentRunner` as a second path with potentially divergent Contract, D025, policy, or verification semantics.

P7-1 may wrap, refactor, or move the existing Harness code, but governance and verification semantics must remain single-sourced.

## Required RPC methods

### `plan.approve`

Request: `{ "sessionId": "sess-...", "planId": "plan-..." }`
Result: `{ "planId": "plan-...", "status": "approved", "workOrdersCreated": ["wo-001", "wo-002", ...] }`

Approving a plan that is not in `AWAITING_APPROVAL` status is a structured error, not a silent no-op.

### `plan.reject`

Request: `{ "sessionId": "sess-...", "planId": "plan-...", "feedback": "optional free text" }`
Result: `{ "planId": "plan-...", "status": "rejected" }`

Rejection creates no Work Orders. It emits `event.planRejected` carrying the feedback (if any); the Architecture Agent's next operation consumes it and produces a revised `PLAN.md` under a new `planId`, linked to the rejected one for history (Section 4.4).

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
PLAN.md (with revision history across approve/reject cycles)
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

- `plan.approve` and `plan.reject` behave exactly as specified, including the reject → revise → re-submit cycle.
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

## Preflight (required before any implementation in this phase)

Before writing new abstraction code, confirm against the actual `feat/p6-open-source-tui` branch:

```text
1. Does role-to-backend binding already exist in any form (even hardcoded), or is it entirely new?
2. Does boundary.py already support more than one execution backend, or exactly one?
3. Is there an existing adapter pattern for Codex/AGY/Claude/Ollama, or would each be new?
4. What does the "legacy fixed role-to-vendor arrangement" (Section 40) actually look like in code today?
5. How should the existing `LLMProvider` Protocol in `validators/harness/runner.py` be generalized without duplicating Harness execution?
```

**If the answer to (1)–(3) is "entirely new" across the board**, treat this phase as a genuine runtime capability addition, not a thin RPC exposure layer — flag it explicitly against the "do not rewrite the entire StackMind runtime" non-goal (Section 35) and scope it as its own reviewed sub-effort with its own PR sequence, rather than folding it silently into the TUI plan's timeline. This preflight's answers determine which of those two situations is real; do not assume either without checking, the same discipline P6-0 applies to the cancellation fix.

## Goal

Generalize the repository's existing `LLMProvider` abstraction into a StackMind **Execution Backend** boundary that supports both agent-native backends and model-backed runtimes, then bind those backends to logical Agent Roles and expose the binding over RPC.

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

## Backend categories

```text
Execution Backend
├── Agent Backend
│     ├── Codex
│     ├── AGY
│     └── Claude
└── Model Backend
      └── Ollama / local models
```

Both categories cross the same StackMind Contract/Harness boundary.

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

## Required RPC methods

### `backend.list`

Result: `{ "backends": [ { "id": "codex", "status": "available" }, { "id": "agy", "status": "available" }, { "id": "claude", "status": "available" }, { "id": "ollama", "status": "unavailable", "reason": "not configured" } ] }`

Never includes credentials, keys, or tokens.

### `role.list`

Result: `{ "roles": [ { "role": "architecture", "backend": "claude", "model": "..." }, { "role": "backend", "backend": "codex", "model": "..." }, ... ] }`

This is the read-only source for the TUI's `:roles` command (Section 24).

### `role.configureBackend`

Request: `{ "role": "backend", "backend": "codex", "model": "...", "credentialRef": "..." }`
Result: `{ "role": "backend", "backend": "codex", "appliedAt": "..." }`

Rejected with a structured error if the role currently has a non-terminal Work Order (Rule 18, Section 3) — rebinding is a between-assignments action, never a live interruption of running work.

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

- Backend selection is a runtime/configuration decision, not a TUI decision.
- The selected backend is captured in the operation record.
- Backend identity does not change the Agent Role's Contract.
- Backend failure produces a structured failure.
- No silent fallback to another backend.
- Backend adapters cannot bypass Harness/Contract/verification.
- Credentials are never exposed to the TUI.

## Exit criteria

- At least two real execution backends can be configured and exercised.
- The same Agent Role can map to different backends without TUI code changes.
- No credential appears in RPC responses, events, logs, or UI.
- Backend failure semantics are deterministic.
- `role.configureBackend` is correctly rejected while that role has a Work Order in flight, and correctly accepted between assignments.

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

## Required RPC methods

### `agent.list`

Request: `{ "sessionId": "sess-...", "operationId": "op-..." }`
Result: `{ "agents": [ { "agentId": "backend", "role": "coder", "state": "RUNNING", "workOrderId": "wo-002" }, ... ] }`

### `agent.cancel`

Request: `{ "sessionId": "sess-...", "agentId": "backend", "reason": "user_cancelled" }`
Result: `{ "agentId": "backend", "canceled": true }`

Cancels only that agent's operation subtree — siblings and parent are unaffected.

### `agent.inspect`

Request: `{ "sessionId": "sess-...", "agentId": "backend" }`
Result: recent events/log excerpt for that agent's operation, using the same `session.history`-style cursor model.

`agent.spawn` is a daemon-internal decision surfaced only through `event.agentSpawned`, not a directly user-invocable RPC method — subagent creation is a runtime planning decision, not a UI action (Rule 1, Section 3).

## Required controls (TUI-facing)

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

Build the TUI around project delivery state, not an endless interactive prompt loop. This is the surface that supersedes the P6 core chat UI as the primary product experience (Section 1).

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

`PLAN.md` is the primary governance artifact and must expose its status, work decomposition, dependencies, risks, validation, and approval controls, plus revision history across any reject/revise cycles (Section 4.4).

## Role/backend surface (read-only, `:roles`)

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
2. review and approve or reject `PLAN.md`, and see a rejected plan return as a revised version;
3. watch autonomous delivery;
4. inspect agent/work-order/backend state via `:roles` and the operation tree;
5. respond to mandatory governance approval when one occurs;
6. receive final project handover.

Normal worker execution requires no manual steering.

---

# 31. Phase P7-5 — Security Hardening for Agentic Execution

## Trust boundary

Unchanged in principle from P6 (Section 19): the TUI must not become a new code-execution environment. P7 adds surface area, not exceptions.

## Requirements

- Every tool call inside the Work Order harness loop is authorized and sandboxed exactly as a single P6 `tool.execute` call — no "trusted mode" for agent-originated calls.
- Subagent contract scope is checked at spawn time against the parent's scope (Rule 15); this check itself needs a positive and negative test.
- Execution-backend credentials never cross the RPC boundary in either direction; a leak-detection test asserts no RPC response or log line ever contains a configured credential/key/token pattern.
- D025 destructive safeguards apply identically inside and outside the harness loop, and to every subagent, not just the top-level operation.
- Terminal sanitization (P6-13) is verified against subagent and execution-backend output specifically, not only top-level assistant output.
- `role.configureBackend`'s in-flight rejection (Section 28) is itself tested as a security control, not just a UX nicety — it prevents a running Work Order's authority from being silently redirected to a different backend mid-execution.

## Security testing

Extend the P6 security suite with: subagent scope-escalation attempts, execution-backend credential leak scanning, runaway-loop budget enforcement, approval-gate bypass attempts, and role-rebinding-during-active-Work-Order attempts.

## Exit criteria

No P7 feature can bypass daemon policy, exceed its parent's contract scope, redirect a running Work Order to an unauthorized backend, or directly execute untrusted code on the host — the same guarantee P6 established, verified again at every new capability.

---

# 32. Phase P7-6 — Testing Strategy for P7

Extending the P6 categories (Section 23) with:

- **Operation tree tests**: spawn/cancel/cascade correctness (Section 26).
- **Plan lifecycle tests**: approve, reject-with-feedback, reject-without-feedback, re-submission after revision, approving an already-approved or already-rejected plan (error case).
- **Harness loop integration tests**: representative Work Orders run end-to-end against a mock tool sandbox; budget enforcement; approval-gate flow.
- **Backend adapter tests**: each adapter tested against a contract/interface test suite, independent of any specific backend's live API; `backend.list`/`role.configureBackend` tested for the unavailable/misconfigured case and the in-flight-rejection case.
- **Subagent RPC tests**: `agent.list`/`agent.cancel`/`agent.inspect` correctness, including reconnect reconstructing full tree state.
- **Fault injection**: subagent crash mid-execution, backend timeout mid-call, cascade-cancel racing with a child's natural completion, role rebind attempted concurrently with Work Order completion.

---

# 33. Phase P7-7 — Documentation

Add, only once real: plan approval/rejection workflow and revision history; Work Order harness loop behavior and approval prompts; execution-backend configuration (server-side credential setup) and the `:roles` command; subagent tree UI and per-agent cancel/inspect; updated architecture diagram matching Section 1.2; updated protocol version and capability list (Section 8).

---

# 34. Updated RPC Method Summary

| Method | Phase | Notes |
|---|---|---|
| `health.version` | P6-3 | Now also reports `governedWorkOrders`/`executionBackends`/`subagents` capabilities |
| `session.create` | P6-3 | unchanged |
| `session.resume` | P6-3 | unchanged |
| `session.list` | P6-3 | unchanged |
| `session.history` | P6-3 | unchanged |
| `session.close` | P6-3 | unchanged |
| `session.sendMessage` | P6-3 | unchanged; still used for ad hoc chat alongside the project-delivery surface |
| `tool.execute` | P6-3 | Also used internally by the P7-1 harness loop, unchanged shape |
| `request.cancel` | P6-3 | unchanged |
| `plan.approve` | P7-1 | new |
| `plan.reject` | P7-1 | new — never creates Work Orders; triggers plan revision |
| `backend.list` | P7-2 | new — replaces the earlier draft's `provider.list`; no credentials returned |
| `role.list` | P7-2 | new — read-only source for `:roles` |
| `role.configureBackend` | P7-2 | new — replaces the earlier draft's `provider.setActive`; rejected while the role has a Work Order in flight |
| `agent.list` | P7-3 | new |
| `agent.cancel` | P7-3 | new |
| `agent.inspect` | P7-3 | new |

There is no `provider.*` method family — "Provider" (Section 1.3) is configuration data attached to an Execution Backend via `role.configureBackend`, not a separately addressable RPC surface.

---

# 35. Updated Explicit Non-Goals

- rewrite the entire StackMind runtime;
- replace the existing session architecture wholesale;
- introduce a database solely because the TUI exists;
- introduce gRPC alongside JSON-RPC;
- embed OpenCode as a second runtime;
- allow TUI components to call execution backend APIs directly;
- allow TUI components to execute tools directly, including every tool used by the P7-1 harness loop;
- add arbitrary remote access;
- build a general distributed, multi-user daemon — Work Orders/subagents in P7-3 are in-process operation-tree nodes within one daemon and one user's project, not multi-user or distributed execution;
- introduce speculative plugin architecture — backend adapters (P7-2) are a fixed, reviewed internal interface, not an open plugin system;
- optimize before measuring;
- replace HTTP with sockets without a demonstrated requirement;
- let the TUI decide when to spawn a subagent; that decision is the daemon's runtime/planning logic;
- let a subagent acquire a contract scope wider than its parent's, under any circumstance;
- let the TUI or first-run setup flow bypass the Section 4.4 approval gate, however small the project;
- treat P7-2's execution-backend abstraction as license to redesign StackMind's core runtime beyond exposing/config-binding already-authorized backends — the Section 28 preflight determines this scope before implementation begins, and any finding that this is substantial new runtime work gets its own reviewed sub-effort rather than silent inclusion here.

---

# 36. Updated Definition of Done

P6 sections (Architecture, Runtime, RPC, Client, TUI, Security, Quality) are unchanged and must still hold. Add:

### Plan governance

- `plan.approve` and `plan.reject` work correctly, including the reject → revise → re-submit cycle and revision history retention.
- A rejected plan never creates Work Orders.
- Approving an already-approved or already-rejected plan returns a structured error.

### Governed Work Order execution

- A representative multi-role Work Order graph completes end-to-end with zero direct tool execution from the TUI.
- Every step is individually authorized, sandboxed, and journaled.
- D025-gated actions correctly pause for approval and resume only on explicit approval.
- Runaway loops self-terminate within budget.
- A failed required child Work Order correctly prevents false parent completion.

### Agent Roles & Execution Backends

- `backend.list`, `role.list`, and `role.configureBackend` work against at least two real adapters.
- No credential material ever appears in an RPC response, event, or log (verified by an automated leak-detection test).
- Rebinding a role's backend is rejected while that role has a Work Order in flight, and accepted between assignments.
- The Section 28 preflight has been completed and its scope finding recorded before this section is considered done.

### Subagents

- The operation tree correctly represents parent/child relationships and survives reconnect.
- Cancelling a subagent never affects its siblings or parent; cancelling a parent cascades correctly.
- No subagent can be authorized with a contract scope wider than its parent's.

---

# 37. Updated Final Acceptance Scenario

The P6 scenario must still pass in full as the prerequisite gate:

```text
1. Start StackMind daemon.
2. Launch StackMind TUI.
3. Complete health/version handshake.
4. Create a session.
5. Enter a prompt.
6. Receive assistant output incrementally.
7. Observe structured tool activity when applicable.
8. Cancel an in-flight operation.
9. Confirm the session remains usable.
10. Send another message.
11. Close the TUI.
12. Restart the daemon.
13. Relaunch the TUI.
14. Resume the previous session.
15. Replay missed history/events.
16. Continue the conversation.
17. Trigger a daemon/connection failure.
18. Verify deterministic reconnect/error UX.
19. Verify malicious terminal sequences are sanitized.
20. Run the full CI suite successfully.
```

**P7 extended scenario** (gated behind the above passing first):

```text
21. During first-run setup, configure Architecture, Backend, Frontend, Q/A, and GitOps
    Agent Roles with at least two distinct execution backends across them.
22. Submit a single initial project objective.
23. Confirm PLAN.md is generated and rendered with AWAITING_APPROVAL status.
24. Reject the plan with feedback; confirm no Work Orders are created and a revised
    PLAN.md is produced and re-submitted for approval.
25. Approve the revised plan; confirm Work Orders are created and dispatched without
    further prompting.
26. Observe Backend and Frontend roles executing concurrently against their configured,
    distinct execution backends.
27. Trigger a D025-gated action within a Work Order and confirm execution pauses until
    explicit approval.
28. Approve it and confirm the Work Order resumes and completes.
29. Attempt to rebind a role's execution backend while its Work Order is in flight;
    confirm it is rejected.
30. Cancel a single in-progress role's Work Order and confirm sibling roles and the
    parent are unaffected.
31. Force a required Work Order to fail and confirm it correctly prevents false project
    completion.
32. Disconnect and reconnect mid-execution; confirm the full Work Order/operation tree,
    PLAN.md status, and role/backend bindings are reconstructed correctly.
33. Confirm project completion triggers a README.md update and a final delivery summary
    covering PLAN.md, per-role, test, verification, and Git status.
34. Confirm no execution-backend credential ever appeared in any RPC response, event,
    log, or TUI surface throughout the run.
35. Run the full CI suite, including the P7 security and fault-injection additions,
    successfully.
```

---

# 38. Updated PR Breakdown

P6 rows (P6-0 through P6-14) are unchanged. Add:

| PR | Scope | Exit condition |
|---|---|---|
| P7-0 | Operation tree model | Cascade cancel/spawn/scope tests pass |
| P7-1 | Work Order harness loop + plan approval/rejection RPC | Representative Work Order graph passes end-to-end; reject → revise → re-submit cycle verified |
| P7-2 | Execution backend abstraction (post-preflight) | Preflight finding recorded; two adapters pass the backend contract test suite; no credential leakage; in-flight rebind correctly rejected |
| P7-3 | Work Order dispatch + subagent RPC | `agent.list/cancel/inspect` correct; reconnect reconstructs full tree |
| P7-4 | TUI surfaces (plan/roles/work orders/tree) | Manual + smoke test of Section 30 flows |
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

The key implementation principle, extended one level:

> **Prove the daemon/client vertical slice before building the visual interface. Prove the P6 vertical slice, fully, before building any P7 capability. Complete the P7-2 preflight before writing execution-backend abstraction code.**

The first working P7 milestone is not a polished delivery-dashboard UI. It is an integration proof over the existing Harness:

```text
Daemon
  ↕
Operation tree (parent + child runtime Operations, each linked to a Work Order where applicable)
  ↕
One PLAN.md, approved via RPC, producing real Work Orders
  ↕
One Work Order, driven headlessly through the harness loop, tool calls authorized and journaled
  ↕
One role→backend binding, applied via RPC and correctly rejected mid-Work-Order
  ↕
One subagent, spawned, cancelled independently, and reconstructed on reconnect
```

Once that is proven without any UI involved, the plan surface, role/backend view, and Work Order tree become frontend implementation work over an already-correct daemon.

---

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

The existing Harness Runtime remains the authoritative governed execution mechanism. P7 integrates that Harness with the daemon Operation lifecycle and Work Order orchestration rather than introducing a parallel coding engine.

The legacy fixed role-to-vendor arrangement becomes configurable role-to-backend binding while preserving the original governance semantics: Contracts, Work Orders, Knowledge API, Harness validation, QA/review, promotion, and D025 — with the Section 28 preflight as the explicit checkpoint confirming how much of that configurability is new work versus existing capability being exposed.
