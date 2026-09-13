# StackMind TUI — Final Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`  
**Target branch:** `feat/p6-open-source-tui`  
**Status:** Implementation-ready plan  
**Scope:** P6 TUI integration only  
**Primary rule:** Do not create a second StackMind runtime inside the TUI.
**Delivery rule:** Prove the runtime cancellation fix before building the interactive TUI surface.

---

## 1. Objective

Build a production-quality interactive StackMind terminal UI as a separate client frontend over the existing StackMind daemon.

The final architecture is:

```text
┌──────────────────────────────┐
│          StackMind TUI       │
│  terminal rendering/input    │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│     StackMindTuiAdapter      │
│ typed client / RPC transport │
└──────────────┬───────────────┘
               │ JSON-RPC 2.0
               │ request/response
               │ + notifications
               ▼
┌──────────────────────────────┐
│         LocalDaemon          │
├──────────────────────────────┤
│ JsonRpcProtocol              │
│ SessionManager               │
│ Policy / Contract boundary   │
│ ToolGateway / sandbox       │
│ SessionStore / Event journal │
└──────────────────────────────┘
```

The daemon remains authoritative for:

- sessions;
- lifecycle;
- authorization;
- policy and contracts;
- tool execution;
- cancellation;
- persistence;
- audit/event history.

The TUI owns only presentation, input, local UI state, and client-side transport state.

---

# 2. Current Codebase Baseline

The implementation must start from the actual code in `feat/p6-open-source-tui`, not from assumptions.

Relevant runtime modules identified during review:

| File | Current responsibility | P6 change |
|---|---|---|
| `validators/kernel/daemon/server.py` | `ThreadingHTTPServer`, `/health`, `/rpc`, `/mcp` | Harden lifecycle/transport integration; add notification-capable connection path |
| `validators/kernel/daemon/protocol.py` | JSON-RPC validation and lifecycle methods | Add TUI RPC contract, typed errors, handshake, request dispatch |
| `validators/kernel/daemon/manager.py` | Session ownership, operations, cancellation, journaling | Separate operation cancellation from terminal session cancellation; add execution lifecycle |
| `validators/kernel/daemon/events.py` | In-process replayable events with sequence numbers | Make event cursor semantics canonical for replay/resume and bridge events to live clients |
| `validators/kernel/daemon/storage.py` | Atomic JSON persistence for sessions/events | Preserve persistence abstraction; prevent event growth from blocking request handling |
| `validators/kernel/session.py` | Rich session lifecycle enum/state transitions | Align RPC-visible lifecycle with the richer runtime state machine |
| `validators/kernel/boundary.py` | Provider/runtime operation authorization boundary | Keep as the privileged execution boundary |
| `tests/test_daemon_runtime.py` | Daemon/session/event/cancellation/recovery tests | Extend for operation execution, streaming, cancellation and reconnect |
| `pyproject.toml` | Python package/runtime dependencies | Add only dependencies justified by selected TUI/transport architecture |
| `README.md` | TUI claims and user-facing commands | Rewrite claims to match shipped behavior |

Important current-state observations:

1. The daemon currently exposes `/rpc` over `ThreadingHTTPServer`.
2. JSON-RPC currently covers session lifecycle/event retrieval but does not provide the complete prompt/turn API required by the TUI.
3. `event.list` is replay/polling, not true server-push streaming.
4. Current session cancellation is terminal session cancellation; it is not equivalent to cancelling one in-flight operation.
5. `events.py` already provides monotonic event sequencing and replay semantics. Reuse that model instead of inventing a second ordering system.
6. `session.py` contains richer lifecycle semantics than the currently exposed daemon RPC.
7. Persistence is atomic JSON and is acceptable for the first P6 implementation, but event persistence must remain replaceable.
8. Existing tests cover important daemon behavior, but there is no complete end-to-end interactive TUI test path.

---

# 3. Non-Negotiable Architecture Rules

1. **TUI is a client, not a runtime.**
2. **No direct provider calls from TUI.**
3. **No direct tool execution from TUI.**
4. **No direct filesystem mutation from TUI except client-local UI state/cache where explicitly approved.**
5. **All privileged actions cross the daemon RPC boundary.**
6. **UI components must never import daemon internals.**
7. **The adapter must be framework-independent.**
8. **OpenTUI/Ink/Blessed must not leak into backend modules.**
9. **JSON-RPC 2.0 is the logical RPC contract.**
10. **Request IDs are mandatory for long-running operations.**
11. **Streaming is a first-class protocol feature.**
12. **Cancellation is request/operation-scoped unless the user explicitly closes/terminates a session.**
13. **Event sequence numbers are the canonical ordering/replay cursor.**
14. **Client reconnect must be able to recover missed events.**
15. **All externally supplied text and tool output is untrusted terminal content.**
16. **No raw ANSI/CSI/control sequences from model/tool output may directly control the terminal.**
17. **Protocol version and capabilities are negotiated explicitly.**
18. **No silent retry of non-idempotent operations.**
19. **Every new runtime behavior gets automated tests before the corresponding UI depends on it.**
20. **Do not remove working daemon behavior merely to simplify the TUI.**

---

# 4. Target User Flow

The primary vertical slice is:

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

Cancellation path:

```text
user presses cancel
    ↓
request.cancel(requestId)
    ↓
daemon marks operation cancellation requested
    ↓
runtime observes cancellation
    ↓
operation terminates
    ↓
terminal cancellation event
    ↓
session remains resumable
```

Reconnect path:

```text
connection lost
    ↓
TUI enters reconnecting state
    ↓
reconnect
    ↓
handshake
    ↓
session.history / event replay from last sequence
    ↓
deduplicate already-rendered events
    ↓
return to live stream
```

---

# 5. Phase P6-0 — Targeted Runtime Preflight

P6-0 is intentionally **small and time-boxed**. The repository/runtime has already been inspected; this phase exists only to re-confirm the facts that P6-1 depends on before changing cancellation semantics.

## Goal

Validate the current cancellation and operation paths immediately before the lifecycle refactor.

## Time box

Target: **30–60 minutes**.

Do not spend this phase designing the TUI, choosing a new transport, evaluating OpenTUI, or polishing documentation.

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

# 6. Phase P6-1 — Correct the Runtime Operation Model

This is the most important backend change.

## Problem

The current `SessionManager.cancel_session()` cancels the active operation and then transitions the session itself to terminal `CANCELLED`.

That behavior is unsuitable for an interactive chat UI where:

> cancel this response

must mean:

> stop the current operation but keep the session available for another message.

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

**Do not start the TUI vertical slice until this gate passes.**

The runtime must have a demonstrably correct operation-scoped cancellation model before the TUI is allowed to depend on it.

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
```

---

# 7. Phase P6-2 — Define the Versioned JSON-RPC Contract

Use JSON-RPC 2.0 as the logical contract.

Do not implement gRPC as a parallel P6 protocol.

## Protocol version

Start with:

```text
protocolVersion = 1
```

Protocol version must be independently tracked from StackMind package version.

## Handshake

Expose:

```text
health.version
```

Result:

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

The exact package version must be read from the project version source rather than hard-coded in the RPC handler.

Unsupported protocol versions must return a structured protocol mismatch error.

---

# 8. Phase P6-3 — RPC Methods

Implement the following contract.

## `health.version`

Returns daemon version and protocol capabilities.

No authentication-sensitive information.

---

## `session.create`

Request:

```json
{
  "metadata": {},
  "model": "optional-model"
}
```

Result:

```json
{
  "sessionId": "sess-...",
  "createdAt": "...",
  "metadata": {}
}
```

Only expose fields actually supported by the runtime.

Do not invent a `userId` authorization model if the daemon does not currently have one.

---

## `session.resume`

Request:

```json
{
  "sessionId": "sess-..."
}
```

Result:

```json
{
  "sessionId": "sess-...",
  "resumedAt": "..."
}
```

Resume must not create a second session.

---

## `session.list`

Support bounded results:

```json
{
  "filter": {},
  "limit": 50,
  "offset": 0
}
```

Result:

```json
{
  "sessions": []
}
```

Never return an unbounded session list.

---

## `session.history`

Request:

```json
{
  "sessionId": "sess-...",
  "cursor": "...",
  "limit": 100
}
```

Result:

```json
{
  "events": [],
  "nextCursor": "..."
}
```

History and live events must use a compatible sequence/cursor model.

---

## `session.close`

Explicitly terminates/closes the session.

This is distinct from cancelling a single request.

---

## `session.sendMessage`

Request:

```json
{
  "sessionId": "sess-...",
  "text": "Explain this project",
  "messageId": "...",
  "options": {
    "stream": true
  }
}
```

Immediate result:

```json
{
  "requestId": "req-...",
  "accepted": true
}
```

The daemon must not hold the RPC request open until the entire assistant response finishes.

---

## `tool.execute`

Only expose this if the existing daemon has a valid authorized tool boundary for it.

Request:

```json
{
  "sessionId": "sess-...",
  "toolId": "...",
  "input": {},
  "requestId": "req-..."
}
```

The daemon remains responsible for:

- authorization;
- policy;
- contract;
- sandbox;
- argument validation;
- execution.

---

## `request.cancel`

Request:

```json
{
  "requestId": "req-...",
  "sessionId": "sess-...",
  "reason": "user_cancelled"
}
```

Result:

```json
{
  "requestId": "req-...",
  "canceled": true
}
```

`canceled: true` means cancellation was accepted/requested, not necessarily that execution has already stopped.

The final event confirms actual termination.

---

# 9. Phase P6-4 — Streaming/Event Protocol

Use server-to-client JSON-RPC notifications.

The existing `RuntimeEvent.sequence` model remains the canonical event ordering mechanism.

Do not create a second independent stream counter.

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

Cancellation:

```json
{
  "jsonrpc": "2.0",
  "method": "event.streamChunk",
  "params": {
    "requestId": "req-1",
    "sessionId": "sess-1",
    "seq": 44,
    "role": "system",
    "complete": true,
    "status": "cancelled"
  }
}
```

## Required event properties

- `requestId`;
- `sessionId`;
- monotonic `seq`;
- event type/method;
- terminal status where applicable;
- timestamp where useful;
- structured payload where applicable.

## Reconnect invariant

For a session:

```text
lastReceivedSeq = N
```

the client can request/replay events after `N`.

The server must not emit an event twice as a logical new event merely because a client reconnects.

The client must also tolerate duplicate delivery and deduplicate by stable event identity/sequence.

---

# 10. Phase P6-5 — Tool Event Model

Never treat tool output as arbitrary terminal instructions.

Use structured events:

```text
event.toolStarted
event.toolOutput
event.toolDone
event.toolError
```

`event.toolOutput`:

```json
{
  "requestId": "req-tool-1",
  "sessionId": "sess-1",
  "seq": 50,
  "streamType": "stdout",
  "text": "..."
}
```

Allowed stream types:

```text
stdout
stderr
status
```

Tool completion:

```json
{
  "requestId": "req-tool-1",
  "sessionId": "sess-1",
  "seq": 51,
  "exitCode": 0,
  "status": "completed"
}
```

Binary output must not be written directly to the terminal renderer.

---

# 11. Phase P6-6 — RPC Error Contract

Reserve:

```text
-32000..-32099
```

Initial application errors:

```text
-32001 SESSION_NOT_FOUND
-32002 AUTH_DENIED
-32003 TOOL_EXECUTION_DENIED
-32004 POLICY_VIOLATION
-32005 PROTOCOL_MISMATCH
-32006 CANCELLED
-32007 OPERATION_NOT_FOUND
-32008 INVALID_STATE
-32009 DAEMON_UNAVAILABLE
```

Error shape:

```json
{
  "code": -32001,
  "message": "Session not found",
  "data": {
    "sessionId": "sess-..."
  }
}
```

Never expose raw Python stack traces by default.

Internal logs may contain diagnostic details subject to secret-redaction rules.

---

# 12. Phase P6-7 — Transport Decision

## Default recommendation

Do not replace the existing transport before proving that the TUI requires it.

The logical contract is JSON-RPC regardless of transport.

Evaluate:

1. existing localhost HTTP transport;
2. Unix domain socket on Linux/macOS;
3. Windows named pipe;
4. localhost TCP with authentication/TLS fallback;
5. WebSocket only if it materially simplifies bidirectional notification delivery.

## Decision gate

Keep HTTP if:

- it can support the required streaming architecture cleanly;
- implementation remains cross-platform;
- security is acceptable;
- reconnect semantics are reliable.

Move to local IPC only if it provides a concrete benefit sufficient to justify:

- packaging complexity;
- Windows support;
- CI complexity;
- authentication changes;
- daemon startup/discovery changes.

**Do not introduce sockets merely because they were proposed architecturally.**

---

# 13. Phase P6-8 — `StackMindTuiAdapter`

The UI must depend on a typed client API rather than raw RPC.

Recommended interface:

```ts
interface StackMindClient {
  connect(signal?: AbortSignal): Promise<void>;
  disconnect(): Promise<void>;

  health(): Promise<{
    version: string;
    protocolVersion: number;
    capabilities: Record<string, boolean>;
  }>;

  createSession(metadata?: unknown): Promise<SessionMeta>;
  listSessions(
    filter?: unknown,
    limit?: number,
    offset?: number
  ): Promise<SessionMeta[]>;

  resumeSession(sessionId: string): Promise<void>;
  closeSession(sessionId: string, reason?: string): Promise<void>;

  sessionHistory(
    sessionId: string,
    cursor?: string,
    limit?: number
  ): Promise<{
    events: unknown[];
    nextCursor?: string;
  }>;

  sendMessage(
    sessionId: string,
    text: string,
    options?: { stream?: boolean },
    signal?: AbortSignal
  ): AsyncIterable<StreamChunk>;

  executeTool(
    sessionId: string,
    toolId: string,
    input?: unknown,
    signal?: AbortSignal
  ): AsyncIterable<unknown>;

  cancelRequest(
    requestId: string
  ): Promise<{
    requestId: string;
    canceled: boolean;
  }>;
}
```

## Adapter responsibilities

- transport connection;
- handshake;
- request ID generation;
- request/response correlation;
- server notification routing;
- event sequencing;
- replay/reconnect;
- typed conversion;
- structured error mapping;
- `AbortSignal` → `request.cancel`;
- bounded buffering;
- safe retry policy;
- capability checks.

## Retry rules

Safe:

- handshake;
- health query;
- idempotent history/list requests.

Not automatically safe:

- `session.create`;
- `session.sendMessage`;
- `tool.execute`.

Use request/message IDs and explicit idempotency support before retrying non-idempotent operations.

---

# 14. Phase P6-9 — OpenTUI Feasibility Gate

Evaluate OpenTUI before building the full UI.

Test:

- build requirements;
- Bun requirement;
- Zig/native dependencies;
- Windows support;
- macOS support;
- Linux support;
- CI reproducibility;
- packaging;
- terminal input;
- resize;
- scrolling;
- keyboard handling;
- rendering responsiveness.

Benchmark representative load:

```text
500 tokens/sec
30 seconds
continuous streaming
large conversation
large tool output
```

Measure:

- input latency;
- rendering latency;
- CPU;
- memory;
- event backlog;
- dropped/coalesced chunks.

Decision:

```text
OpenTUI
   ↓ if blocked
Ink
   ↓ if blocked
another proven TUI framework
```

The adapter must remain unchanged by this decision.

Record the decision in an ADR.

---

# 15. Phase P6-10 — Core TUI

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

- text input;
- Enter to send;
- streaming assistant output;
- user/assistant/tool/system roles;
- scrolling;
- keyboard navigation;
- focus management;
- resize handling;
- cancellation shortcut;
- connection status;
- request status;
- new-message indicator when scrolled away from bottom.

## Rendering rules

- pure rendering where possible;
- no blocking network calls in render code;
- no heavy synchronous JSON processing during rendering;
- coalesce tiny stream chunks;
- auto-scroll only when user is already at bottom;
- preserve scroll position when historical content is inserted.

---

# 16. Phase P6-11 — Session Management UI

Implement:

```text
:new
:resume
:close
```

and the corresponding keyboard/UI controls.

Session browser must show:

- session ID;
- last activity;
- metadata where available;
- lifecycle state where available.

Support:

- create;
- list;
- select;
- resume;
- close;
- refresh.

## Large history

Do not load unlimited history into memory.

Use:

```text
session.history(cursor, limit)
```

and virtualized rendering for large sessions.

---

# 17. Phase P6-12 — Reconnect & Fault UX

Explicit UI states:

```text
CONNECTED
CONNECTING
RECONNECTING
OFFLINE
PROTOCOL_MISMATCH
SESSION_UNAVAILABLE
REQUEST_RUNNING
CANCELLING
ERROR
```

Handle:

- daemon not started;
- daemon crash;
- connection reset;
- RPC timeout;
- malformed event;
- protocol mismatch;
- session missing;
- policy denial;
- tool failure;
- cancellation;
- daemon restart.

## Reconnect behavior

1. preserve local UI state;
2. show reconnecting status;
3. reconnect;
4. renegotiate capabilities;
5. recover active session;
6. replay from last known sequence;
7. deduplicate;
8. resume live notifications.

Never silently replay a potentially non-idempotent user request.

---

# 18. Phase P6-13 — Security Hardening

## Trust boundary

```text
TUI
 ↓
RPC
 ↓
daemon authorization
 ↓
policy/contract
 ↓
ToolGateway
 ↓
sandbox
```

The TUI must never bypass this chain.

## Requirements

- server-side validation;
- server-side authorization;
- capability-scoped handshake;
- least-privilege local IPC permissions where applicable;
- optional short-lived bearer authentication where required;
- no credentials in logs;
- no trust in client-supplied authorization claims;
- no Node `vm` presented as a security boundary;
- OS/container/VM isolation for untrusted execution.

## Terminal sanitization

AI and tool output is untrusted.

Default renderer must:

- allow normal printable text;
- allow newline/tab;
- neutralize control characters;
- neutralize raw CSI/escape sequences;
- only permit ANSI formatting generated by a trusted renderer path.

Never render arbitrary tool output as terminal control instructions.

Security tests must cover:

- ANSI injection;
- escape-sequence injection;
- malicious structured output;
- unauthorized tool execution;
- capability escalation;
- policy bypass;
- sandbox boundary regression.

---

# 19. Phase P6-14 — Concurrency & Persistence

## Server concurrency

Because `ThreadingHTTPServer` can process requests concurrently, audit all shared mutable state.

Ensure synchronization around:

- sessions;
- active operations;
- cancellation;
- event sequencing;
- journal mutation;
- persistence snapshots.

Never perform provider/tool execution while holding a manager-wide lock.

## Persistence

Keep the current atomic-write model for P6 unless benchmarks prove it insufficient.

Requirements:

- atomic replacement;
- consistent session/event snapshots;
- recovery after process restart;
- no partially written state;
- persistence failures surfaced as structured errors.

Prepare the storage interface for future migration away from a single JSON file.

Do not introduce a database solely for the TUI unless required by measured workload.

---

# 20. Phase P6-15 — Performance

Targets:

- responsive input during streaming;
- bounded memory;
- no unbounded event queue;
- no render-loop blocking;
- smooth scrolling for large sessions.

Benchmark:

| Scenario | Required measurement |
|---|---|
| 500 tokens/sec for 30s | input/render latency |
| 500+ messages | memory/scrolling |
| 1000+ visible lines | rendering responsiveness |
| tool-output burst | backpressure |
| repeated session switching | memory stability |
| reconnect during stream | recovery latency |
| concurrent operations | CPU/memory/isolation |

The exact acceptable thresholds must be measured and documented before release rather than invented without baseline data.

---

# 21. Phase P6-16 — Logging

Record:

- RPC method;
- request status;
- latency;
- protocol errors;
- cancellation;
- operation failure;
- reconnect events.

Do not log:

- bearer tokens;
- credentials;
- secrets;
- raw prompt contents by default.

If debug prompt logging is ever added, it must be explicit and clearly documented.

Use log rotation.

---

# 22. Phase P6-17 — Testing Strategy

## Backend unit tests

Add tests for:

- RPC method validation;
- handshake;
- protocol mismatch;
- structured error mapping;
- operation lifecycle;
- operation cancellation;
- session remains resumable after cancellation;
- cancellation/completion race;
- concurrent operations;
- event sequence ordering;
- persistence/recovery.

## RPC integration tests

Prove:

```text
create
→ send
→ ack
→ streamed events
→ completion
```

and:

```text
create
→ send
→ stream
→ cancel
→ terminal cancellation
→ send again
```

## Adapter tests

Use a mock transport to test:

- correlation;
- notifications;
- sequence ordering;
- replay;
- duplicate events;
- malformed events;
- timeout;
- reconnect;
- `AbortSignal`;
- cancellation;
- bounded buffering.

## Real daemon integration

Run against an ephemeral daemon instance.

Prove:

```text
connect
→ handshake
→ create
→ message
→ stream
→ cancel/complete
→ history
→ reconnect
→ replay
```

## TUI tests

At minimum:

- launch;
- render;
- input;
- send;
- stream;
- scroll;
- resize;
- cancel;
- reconnect;
- session switching.

Use a virtual/headless terminal where practical.

## Security tests

- ANSI injection;
- malformed structured event;
- tool authorization;
- policy rejection;
- capability mismatch;
- secret redaction.

## Fault injection

Inject:

1. daemon crash mid-stream;
2. connection reset;
3. malformed notification;
4. delayed response;
5. protocol mismatch;
6. cancellation during assistant stream;
7. cancellation during tool execution;
8. persistence failure.

---

# 23. Phase P6-18 — CLI Integration

The intended user commands are:

```text
stackmind daemon start
stackmind tui
```

The TUI must:

- discover/connect to the intended daemon;
- provide actionable daemon-unavailable errors;
- avoid silently starting an unauthorized second runtime;
- exit cleanly;
- preserve session state in the daemon.

Existing CLI commands must continue to work.

---

# 24. Phase P6-19 — Documentation

Update only after implementation behavior is real.

Required documentation:

- README TUI section;
- architecture diagram;
- RPC contract;
- protocol version;
- transport;
- authentication;
- capability negotiation;
- keyboard shortcuts;
- session management;
- cancellation behavior;
- reconnect behavior;
- troubleshooting;
- security model;
- framework/build requirements;
- CHANGELOG.

Remove or qualify any README statement that claims functionality not covered by tests.

---

# 25. PR Breakdown

Use small, independently reviewable PRs.

| PR | Scope | Exit condition |
|---|---|---|
| P6-0 | Repository validation | Actual integration map complete |
| P6-1 | Operation lifecycle/cancellation | Cancelled operation leaves session resumable |
| P6-2 | RPC contract/types/errors | Contract tests pass |
| P6-3 | Handshake/capabilities | Client/server negotiate protocol |
| P6-4 | Streaming event bridge | Real server notifications work |
| P6-5 | History/replay/reconnect | Missed events recover correctly |
| P6-6 | Typed TUI adapter | Adapter works against real daemon |
| P6-7 | OpenTUI feasibility spike | Framework decision recorded |
| P6-8 | Core chat TUI | Real streaming chat works |
| P6-9 | Session browser | Create/list/resume/close works |
| P6-10 | Fault/reconnect UX | Injected failures do not crash UI |
| P6-11 | Security/sanitization | Security suite passes |
| P6-12 | Performance/virtualization | Baselines meet agreed thresholds |
| P6-13 | CI/package/docs | Release candidate reproducibly builds |
| P6-14 | Final acceptance | Full E2E workflow passes |

Do not combine all backend, adapter, and UI changes into one PR.

---

# 26. CI Requirements

Every relevant PR should run:

1. formatting;
2. linting;
3. Python tests;
4. TypeScript type checking;
5. RPC unit tests;
6. adapter unit tests;
7. daemon integration tests;
8. streaming tests;
9. cancellation tests;
10. reconnect tests;
11. security tests;
12. headless TUI smoke tests;
13. native framework build tests where applicable;
14. packaging verification.

Required E2E smoke test:

```text
start daemon
 ↓
connect client
 ↓
health/version
 ↓
create session
 ↓
send message
 ↓
receive streamed output
 ↓
assert output
 ↓
complete or cancel
 ↓
close session
```

---

# 27. Definition of Done

P6 is complete only when all of the following are true:

### Architecture

- TUI is a separate client.
- No second runtime exists in TUI.
- TUI never bypasses daemon policy/tool boundaries.
- Adapter is framework-independent.

### Runtime

- Operation cancellation is separate from session termination.
- Concurrent manager state is synchronized.
- Session recovery works.

### RPC

- Versioned JSON-RPC contract exists.
- Handshake/capabilities work.
- Streaming works.
- Cancellation works.
- Structured errors work.
- Tool events are structured.

### Client

- Typed adapter works against real daemon.
- Reconnect/replay works.
- Buffers are bounded.
- Non-idempotent requests are not silently retried.

### TUI

- Real interactive chat works.
- Streaming renders incrementally.
- Sessions can be created/resumed/switched.
- History scrolls efficiently.
- Cancellation works.
- Resize/input/focus work.

### Security

- No direct privileged execution from TUI.
- Terminal output is sanitized.
- Authorization/policy remains server-side.
- Secrets are not logged.

### Quality

- Backend tests pass.
- RPC integration tests pass.
- Adapter tests pass.
- TUI smoke tests pass.
- Fault injection passes.
- Performance baseline is documented.
- Documentation matches implementation.

---

# 28. Explicit Non-Goals

Do **not** do these as part of P6 unless a concrete blocker is discovered:

- rewrite the entire StackMind runtime;
- replace the existing session architecture wholesale;
- introduce a database solely because the TUI exists;
- introduce gRPC alongside JSON-RPC;
- embed OpenCode as a second runtime;
- allow TUI components to call provider APIs;
- allow TUI components to execute tools;
- add arbitrary remote access;
- build a general distributed multi-user daemon;
- introduce speculative plugin architecture;
- optimize before measuring;
- replace HTTP with sockets without a demonstrated requirement.

---

# 29. Final Acceptance Scenario

A fresh environment must be able to execute:

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

This scenario is the release gate.

---

# 30. Implementation Order

Implement in exactly this dependency order unless a repository fact discovered during P6-0 requires adjustment:

```text
P6-0 Repository validation
        ↓
P6-1 Operation lifecycle + cancellation
        ↓
P6-2 RPC contract + errors
        ↓
P6-3 Handshake/capabilities
        ↓
P6-4 Streaming
        ↓
P6-5 Replay/reconnect
        ↓
P6-6 Typed adapter
        ↓
P6-7 OpenTUI feasibility
        ↓
P6-8 Core TUI
        ↓
P6-9 Session UI
        ↓
P6-10 Fault UX
        ↓
P6-11 Security
        ↓
P6-12 Performance
        ↓
P6-13 CI/package/docs
        ↓
P6-14 Acceptance
```

The key implementation principle is:

> **Prove the daemon/client vertical slice before building the visual interface.**

The first working milestone is therefore not a polished TUI. It is:

```text
Daemon
  ↕
Typed client
  ↕
Create session
  ↕
Send message
  ↕
Stream events
  ↕
Cancel operation
  ↕
Keep session resumable
  ↕
Reconnect + replay
```

Once that contract is proven, the terminal UI becomes a frontend implementation rather than an architectural experiment.
