# StackMind TUI — Final Implementation Plan (v5)

**Repository:** `Abhishek3670/stackmind-cli`
**Target branch:** `feat/p6-open-source-tui`
**Status:** Implementation-ready plan
**Scope:** P6 (client foundation) + P7 (agentic coding, provider management, subagent orchestration)
**Primary rule:** Do not create a second StackMind runtime inside the TUI.
**Delivery rule:** Prove the runtime cancellation fix before building the interactive TUI surface. Prove the P6 vertical slice before building any P7 capability.

---

## 1. Objective

Build a production-quality interactive StackMind terminal UI as a separate client frontend over the existing StackMind daemon, capable of:

1. ordinary streamed chat (P6 — unchanged from v4);
2. Claude-Code-class interactive coding (P7-1);
3. provider/model selection (P7-2);
4. subagent orchestration and inspection (P7-3).

The final architecture is:

```text
┌────────────────────────────────────────────────────┐
│                    StackMind TUI                    │
│           terminal rendering / input / display      │
└───────────────────────────┬──────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────┐
│                 StackMindTuiAdapter                 │
│            typed client / RPC transport             │
└───────────────────────────┬──────────────────────────┘
                            │ JSON-RPC 2.0
                            │ request/response + notifications
                            ▼
┌────────────────────────────────────────────────────┐
│                     LocalDaemon                     │
├──────────────────────────────────────────────────────┤
│ JsonRpcProtocol                                     │
│ SessionManager (session + operation + subagent tree) │
│ Policy / Contract boundary                          │
│ Provider Runtime Layer (multi-provider)             │
│ Agent Orchestrator (subagent lifecycle)             │
│ ToolGateway / sandbox                               │
│ SessionStore / Event journal                        │
└────────────────────────────────────────────────────┘
```

The daemon remains authoritative for:

- sessions, operations, and subagents;
- lifecycle and cancellation at every level of that tree;
- authorization, policy, and contracts;
- provider selection and provider execution;
- tool execution;
- persistence;
- audit/event history.

The TUI owns only presentation, input, local UI state, and client-side transport state — at every layer, including coding, providers, and subagents. **Nothing in P7 changes this split.** P7 adds capability; it does not move execution authority toward the client.

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

Unchanged from v4 — these apply identically to P6 and to every P7 capability:

1. **TUI is a client, not a runtime.**
2. **No direct provider calls from TUI.** Provider selection is a daemon-side operation; the TUI only sends a selection request and renders the result.
3. **No direct tool execution from TUI.** This includes every tool a coding loop uses — file read/write, test runner, git, shell.
4. **No direct filesystem mutation from TUI except client-local UI state/cache where explicitly approved.**
5. **All privileged actions cross the daemon RPC boundary.** This includes spawning, cancelling, or inspecting a subagent.
6. **UI components must never import daemon internals.**
7. **The adapter must be framework-independent.**
8. **OpenTUI/Ink/Blessed must not leak into backend modules.**
9. **JSON-RPC 2.0 is the logical RPC contract.**
10. **Request IDs are mandatory for long-running operations**, including every subagent operation.
11. **Streaming is a first-class protocol feature.**
12. **Cancellation is request/operation-scoped unless the user explicitly closes/terminates a session.** In P7, cancelling a parent operation cascades to its child (subagent) operations; cancelling a single subagent must not cancel its siblings or parent.
13. **Event sequence numbers are the canonical ordering/replay cursor**, for chat events, tool events, provider events, and agent events alike.
14. **Client reconnect must be able to recover missed events**, from any depth of the operation tree.
15. **All externally supplied text and tool output is untrusted terminal content.** This explicitly includes subagent output and provider responses.
16. **No raw ANSI/CSI/control sequences from model/tool output may directly control the terminal.**
17. **Protocol version and capabilities are negotiated explicitly.** A client that does not declare `agents` or `providers` capability support must not receive those event types.
18. **No silent retry of non-idempotent operations.** This includes tool calls with side effects (file writes, commits) and provider calls.
19. **Every new runtime behavior gets automated tests before the corresponding UI depends on it.**
20. **Do not remove working daemon behavior merely to simplify the TUI.**
21. **New — no subagent may hold a wider contract scope than its parent operation.** A child operation is authorized against the intersection of its own requested scope and its parent's contract, never a superset.
22. **New — provider credentials are never sent to, stored by, or displayed in full by the TUI.** The TUI receives only provider name, model name, and status.
23. **New — a coding operation's destructive-action gate (`D025`) applies identically whether the operation is the top-level operation or a subagent.**

---

# 4. Target User Flow

## 4.1 Ordinary chat (P6 — unchanged)

```text
start daemon
    ↓
launch `stackmind tui`
    ↓
connect to daemon
    ↓
health/version handshake
    ↓
create or resume session
    ↓
user enters prompt
    ↓
session.sendMessage
    ↓
immediate request acknowledgement
    ↓
runtime executes operation
    ↓
daemon emits structured events
    ↓
TUI receives streamed events
    ↓
assistant/tool output renders incrementally
    ↓
operation completes
    ↓
terminal completion event
    ↓
user continues conversation
```

Cancellation and reconnect paths are unchanged from v4 (see Phase P6-1 and P6-4/P6-12 below); they are extended, not replaced, in P7.

## 4.2 Interactive coding (P7-1)

```text
you type: "implement JWT authentication"
    ↓
session.sendMessage (options.mode = "agentic")
    ↓
daemon begins a coding operation
    ↓
event.agentStep  { step: "planning" }
    ↓
event.toolCall   { tool: "read_file",  status: "running" }
event.toolResult { tool: "read_file",  status: "completed" }
    ↓
event.toolCall   { tool: "edit_file",  status: "running" }
event.toolResult { tool: "edit_file",  status: "completed", diffSummary: {...} }
    ↓
event.toolCall   { tool: "run_tests",  status: "running" }
event.toolResult { tool: "run_tests",  status: "completed", passed: true }
    ↓
event.agentStep  { step: "done" }
    ↓
operation.state = COMPLETED
    ↓
TUI renders:
  ● Planning
  ● Reading existing auth code
  ● Editing src/auth.py
  ● Running tests
  ✓ Tests passed
  Files changed:
    src/auth.py
    tests/test_auth.py
```

Every tool call in this flow is authorized, sandboxed, and journaled by the daemon exactly as a single `tool.execute` call is in P6 — the coding loop is many gated tool calls in sequence, not a new execution path.

## 4.3 Provider selection (P7-2)

```text
you run: :provider
    ↓
provider.list
    ↓
TUI shows selector:
  ● Anthropic
    OpenAI
    Ollama
    ↓
you select "OpenAI", model "gpt-5.6"
    ↓
provider.setActive { sessionId, provider: "openai", model: "gpt-5.6" }
    ↓
daemon validates provider is registered and authorized
    ↓
session metadata updated
    ↓
event.providerChanged
    ↓
subsequent session.sendMessage calls use the new provider
```

The TUI never calls OpenAI, Anthropic, or Ollama directly. It only ever calls the daemon's `provider.*` methods.

## 4.4 Subagent orchestration (P7-3)

```text
you type: "build the backend and frontend for this feature in parallel"
    ↓
session.sendMessage
    ↓
daemon's main operation decides to spawn subagents
    ↓
event.agentSpawned { agentId: "backend",  parentOperationId, role: "coder" }
event.agentSpawned { agentId: "frontend", parentOperationId, role: "coder" }
    ↓
event.toolCall / event.toolResult streamed per-agent, tagged with agentId
    ↓
event.agentCompleted { agentId: "backend"  }
event.agentCompleted { agentId: "frontend" }
    ↓
main operation aggregates results
    ↓
operation.state = COMPLETED

TUI renders:
  ● Main agent
    ├─ ✓ Planner
    ├─ ✓ Backend agent
    ├─ ✓ Frontend agent
    └─ ○ Test agent (waiting)
```

Subagents are **runtime objects owned by the daemon's operation tree**, not TUI-side simulations. The TUI can cancel, pause, or inspect a specific subagent by its `agentId` — each such action is its own authorized RPC call, scoped to that node of the tree.

---

# 5. Capability Contract

This section exists because a plan that only ships `create session / list events / approve / reject / cancel` is not sufficient — StackMind needs to be a coding-and-orchestration interface, not only a chat client. Every P7 phase below exists to satisfy one row of this table, and none of them is optional or "future work" — they are scoped, gated phases of this same plan.

| Capability | TUI responsibility | Daemon/runtime responsibility | Phase |
|---|---|---|---|
| Interactive coding | Render plan/tool/diff/test status; collect approval where required; display file change summary | Own the tool loop: plan, call tools, validate diffs against contract, apply D025 gates, write back | P7-1 |
| Provider management | Present provider/model selector; show active provider/model; never transmit credentials | Own the provider registry, adapters, authorization, and execution | P7-2 |
| Subagent lifecycle | Render the operation tree; allow cancel/pause/inspect per node; stream per-agent output | Own spawn/cancel/pause authorization, contract-scoped down-scoping, aggregation of results | P7-3 |

**Gate:** No P7 phase begins until the P6 Definition of Done (Section 27) and the P6 Final Acceptance Scenario (Section 29) both pass in full. P7 is capability built **on top of** a proven client/daemon vertical slice — not a parallel effort.

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

# 26. Phase P7-0 — Operation Tree Model

## Goal

Extend the P6-1 operation model from a flat `session -> operation` relationship into a tree, so a subagent is simply an operation whose parent is another operation, using the exact same state machine already proven in P6-1.

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

# 27. Phase P7-1 — Agentic Coding Loop

## Goal

Give `session.sendMessage` the ability to drive a multi-step tool loop — the Claude-Code-class interaction model — entirely inside the daemon, using the existing `tool.execute` authorization path for every step.

## Required execution loop (daemon-side only)

```text
1. Assemble context (may use StackMind's own Knowledge API / graph query internally).
2. Plan next step.
3. Emit event.agentStep.
4. If a tool is needed: authorize via existing contract/policy boundary,
   execute via ToolGateway/sandbox, emit event.toolCall then event.toolResult.
5. Repeat 2-4 until the model signals completion or a step/time/token budget is hit.
6. Emit final operation completion event with a structured summary
   (files changed, tests run, commands executed).
```

This is not a new execution path — it is `tool.execute` called repeatedly by the daemon's own runtime under one parent operation, exactly as already authorized in P6.

## Required tool surface (daemon-owned, TUI-invisible)

At minimum: read file, search code, edit/write file, run tests, inspect git status/diff, execute an explicitly approved command. Each must go through the same authorization/policy/contract/sandbox path as any other `tool.execute` call — no coding-specific bypass.

## Required safeguards

- **D025 destructive safeguard applies identically** inside the coding loop as it does to any other tool call (Rule 23, Section 3) — no silent mass deletion, history rewrite, or unbacked modification.
- A **step/time/token budget** is mandatory; a runaway loop must self-terminate into `FAILED` with a structured reason, not run indefinitely.
- Any tool call requiring human approval (per existing contract policy) pauses the operation in a new `AWAITING_APPROVAL` sub-state and emits an event the TUI can render as a prompt; the operation does not proceed until `request.approve` (Section 28) is received.

## Required events

Reuses the existing envelope from Section 10:

```json
{
  "jsonrpc": "2.0",
  "method": "event.agentStep",
  "params": { "sessionId": "sess-1", "operationId": "op-1", "seq": 50, "step": "planning" }
}
```

```json
{
  "jsonrpc": "2.0",
  "method": "event.toolResult",
  "params": {
    "sessionId": "sess-1",
    "operationId": "op-1",
    "seq": 51,
    "tool": "edit_file",
    "status": "completed",
    "diffSummary": { "file": "src/auth.py", "additions": 42, "deletions": 3 }
  }
}
```

## Exit criteria

- A representative coding task (e.g. add an endpoint with a test) completes end-to-end through the daemon with zero direct tool execution from the TUI.
- Every tool call in the loop is individually authorized, sandboxed, and journaled.
- D025-gated actions correctly pause for approval.
- A forced runaway loop self-terminates within its configured budget.
- Cancelling the top-level operation mid-loop stops in-flight tool execution and leaves the session resumable (reuses P6-1 guarantees).

---

# 28. Phase P7-2 — Provider Runtime Layer

## Goal

Let a session use a chosen provider/model, selected through the TUI but executed and managed entirely by the daemon.

## Required model

```text
                 Provider Runtime Layer (daemon-side)
       ┌──────────────┬──────────────┬──────────────┐
       │   Anthropic   │    OpenAI    │    Ollama    │
       │   adapter     │   adapter    │   adapter    │
       └──────────────┴──────────────┴──────────────┘
```

Each provider is implemented as an adapter behind `boundary.py`'s existing authorization boundary — adding a provider means implementing one adapter to a fixed internal interface, not modifying TUI code or the RPC contract.

## Required RPC methods

### `provider.list`

Result: `{ "providers": [ { "id": "anthropic", "models": ["claude-..."], "status": "available" }, { "id": "openai", "models": ["gpt-5.6"], "status": "available" }, { "id": "ollama", "models": ["llama3"], "status": "unavailable", "reason": "not configured" } ] }`

Never includes credentials, keys, or tokens.

### `provider.setActive`

Request: `{ "sessionId": "sess-...", "provider": "openai", "model": "gpt-5.6" }`
Result: `{ "sessionId": "sess-...", "provider": "openai", "model": "gpt-5.6", "appliedAt": "..." }`

The daemon validates the provider is registered, authorized, and available before applying; an invalid selection returns a structured `providerUnauthorized` or `providerNotFound` error rather than silently falling back.

## Required event

```json
{
  "jsonrpc": "2.0",
  "method": "event.providerChanged",
  "params": { "sessionId": "sess-1", "seq": 60, "provider": "openai", "model": "gpt-5.6" }
}
```

## Required safeguards

- Provider credentials are configured and stored entirely server-side (e.g. daemon config/environment), never accepted as an RPC parameter from the TUI.
- Switching providers mid-operation is rejected with a structured error; switching is only permitted between operations.
- An unavailable/misconfigured provider is reported with a clear `status`/`reason` in `provider.list`, never a silent omission.

## Exit criteria

- `provider.list` correctly reflects configured/available providers.
- `provider.setActive` changes the provider used by the next `session.sendMessage` on that session.
- No credential material ever appears in an RPC response, event, or log.
- Adding a second working provider adapter requires no TUI code change.

---

# 29. Phase P7-3 — Subagent Lifecycle & Orchestration RPC

## Goal

Expose the P7-0 operation tree over RPC so the TUI can display, and the user can control, individual subagents.

## Required RPC methods

### `agent.spawn`

Only ever called by the daemon's own orchestration logic in response to a `session.sendMessage` — **not directly callable as an arbitrary user action**, since spawning is a runtime planning decision, not a UI action. Included here for contract completeness and testing.

### `agent.list`

Request: `{ "sessionId": "sess-...", "operationId": "op-..." }`
Result: `{ "agents": [ { "agentId": "backend", "role": "coder", "state": "RUNNING" }, { "agentId": "frontend", "role": "coder", "state": "RUNNING" }, { "agentId": "tests", "role": "tester", "state": "REQUESTED" } ] }`

### `agent.cancel`

Request: `{ "sessionId": "sess-...", "agentId": "backend", "reason": "user_cancelled" }`
Result: `{ "agentId": "backend", "canceled": true }`

Cancels only that agent's operation subtree — siblings and parent are unaffected (Section 26 cascade rules).

### `agent.inspect`

Request: `{ "sessionId": "sess-...", "agentId": "backend" }`
Result: recent events/log excerpt for that agent's operation, using the same `session.history`-style cursor model.

## Required events

```json
{ "method": "event.agentSpawned",   "params": { "sessionId": "sess-1", "seq": 70, "agentId": "backend",  "parentOperationId": "op-1", "role": "coder" } }
{ "method": "event.agentCompleted", "params": { "sessionId": "sess-1", "seq": 90, "agentId": "backend",  "status": "completed" } }
```

## Exit criteria

- `agent.list` accurately reflects the live operation tree at any point.
- `agent.cancel` on one node never affects a sibling or the parent.
- `agent.inspect` returns a coherent, sequence-ordered log for a single agent.
- Reconnect/replay correctly reconstructs the full tree state, not just the top-level operation.

---

# 30. Phase P7-4 — TUI Surfaces for Coding, Providers, and Subagents

Build only after P7-0 through P7-3 are proven against a real or mock daemon without the UI, exactly as the P6 core TUI (Section 16) was built only after the P6 client vertical slice worked headlessly.

## Coding surface

```text
> implement the login endpoint

● Planning
● Reading existing auth code
● Editing src/auth.py
● Running tests
✓ Tests passed

Files changed:
  src/auth.py
  tests/test_auth.py
```

Required behavior: each `event.agentStep`/`event.toolCall`/`event.toolResult` updates one line in place rather than spamming the scrollback; an `AWAITING_APPROVAL` state renders a clear approve/deny prompt and blocks further input for that operation only.

## Provider surface

```text
:provider

Provider
────────────────────
● Anthropic
  OpenAI
  Ollama
```

Selecting an option calls `provider.setActive` and reflects the applied provider/model in the session status bar. An unavailable provider is shown greyed out with its `reason`, never hidden silently.

## Subagent surface

```text
● Main agent
  ├─ ✓ Planner
  ├─ ● Backend agent      running
  ├─ ● Frontend agent     running
  └─ ○ Test agent         waiting
```

Required behavior: selecting a node offers cancel/inspect; cancelling a node sends `agent.cancel` scoped to that `agentId` only; the tree updates live from `event.agentSpawned`/`event.agentCompleted` and from `agent.list` on reconnect.

## Rendering rules

Same discipline as Section 16: pure rendering, no blocking calls, coalesced updates, auto-scroll only at bottom, preserved scroll position on tree growth.

## Exit criteria

A user can run a coding task, watch tool/plan/diff/test status render live, approve a gated action, switch provider between tasks, and watch a multi-agent task's tree update and be individually cancellable — entirely from the TUI, with zero direct execution happening client-side.

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
