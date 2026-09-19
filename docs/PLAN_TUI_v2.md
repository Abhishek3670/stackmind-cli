# StackMind TUI Integration — Implementation Plan

## 1. Objective

Build a modern interactive StackMind terminal UI (TUI) as a **separate client frontend** that communicates with the existing StackMind daemon through a stable RPC boundary.

The TUI must not contain a second StackMind runtime. The daemon remains the trusted runtime for sessions, policy enforcement, tool execution, and persistence.

```text
┌─────────────────────┐
│      StackMind TUI  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ StackMindTuiAdapter │
│  typed TS client    │
└──────────┬──────────┘
           │ JSON-RPC 2.0
           ▼
┌─────────────────────┐
│     LocalDaemon     │
├─────────────────────┤
│ SessionManager      │
│ Policy/Contract     │
│ ToolGateway/Sandbox │
│ SessionStore        │
└─────────────────────┘
```

---

## 2. End State

At release, a user should be able to launch StackMind in the terminal and:

- create and resume sessions;
- browse previous sessions;
- send messages and receive streamed assistant output;
- cancel an in-flight response or tool operation;
- view tool output and policy failures as structured UI events;
- scroll and navigate large conversations efficiently;
- recover gracefully from daemon failures and protocol mismatches;
- use the system without the TUI ever bypassing daemon-side security controls.

The result should be an OpenCode/OpenTUI-style interactive frontend over the existing StackMind runtime, rather than a rewrite of the backend.

---

## 3. Non-Negotiable Architecture Rules

1. **TUI is a client.** It owns presentation and user interaction only.
2. **Daemon is trusted.** Authorization, policy enforcement, and privileged execution remain server-side.
3. **RPC is the integration boundary.** UI components must not call backend internals directly.
4. **Adapter is framework-independent.** OpenTUI/Ink/etc. must sit above `StackMindClient` rather than leak into backend code.
5. **Streaming is first-class.** Do not retrofit streaming after the UI is built.
6. **Cancellation is first-class.** Every long-running request must be correlated by request ID and cancellable where supported.
7. **Protocol versions are explicit.** Client and daemon must negotiate compatibility.
8. **No untrusted code execution in the TUI.** Tool execution happens through the daemon sandbox only.
9. **Every milestone must be independently testable and reversible.**

---

## 4. Phase 0 — Repository & Architecture Validation

### Goal

Replace all assumptions from the original audit with facts from the actual StackMind repositories.

### Tasks

Inspect and document the real:

- `LocalDaemon` entry point;
- `SessionManager` methods and lifecycle;
- policy / contract enforcement;
- `ToolGateway` and sandbox boundaries;
- session persistence;
- existing CLI entry points;
- existing `StackMindTuiAdapter` scaffold, if present;
- current RPC/HTTP transport;
- current authentication/authorization;
- current streaming and cancellation behavior.

Build an integration matrix:

| Capability | Existing backend | Existing RPC | Missing work |
|---|---|---|---|
| Create session | Verify | Verify | Fill gap |
| Resume session | Verify | Verify | Fill gap |
| List sessions | Verify | Verify | Fill gap |
| History | Verify | Verify | Cursor/pagination if needed |
| Send message | Verify | Verify | Streaming if needed |
| Tool execution | Verify | Verify | Structured events |
| Cancellation | Verify | Verify | Add `request.cancel` |
| Health/version | Verify | Verify | Add negotiation |

### Exit criteria

- All actual module/function locations are documented.
- Existing endpoints and gaps are known.
- No implementation decision depends on an unverified placeholder.

---

## 5. Phase 1 — Lock the RPC Contract

### 5.1 Protocol

Use **JSON-RPC 2.0** unless Phase 0 discovers a hard architectural blocker. Do not mix JSON-RPC and gRPC as parallel contracts.

Use a bidirectional, framed transport so server-to-client streaming notifications are reliable.

### 5.2 Transport decision

Preferred local transport:

- Unix domain socket on Linux/macOS;
- named pipe on Windows.

Fallback:

- localhost TCP with TLS when local IPC is unavailable or operationally unsuitable.

WebSocket may be used over the selected transport if it materially simplifies bidirectional framing and implementation.

The transport decision must be documented before server/client implementation is finalized because it affects authentication, CI, packaging, and startup behavior.

### 5.3 Handshake / capability negotiation

Require an explicit client registration or handshake that returns:

- daemon version;
- protocol version;
- permitted capabilities;
- available tool capabilities;
- streaming support;
- cancellation support.

Target endpoint:

```text
health.version
```

Result shape:

```json
{
  "version": "1.0.0",
  "protocolVersion": 1,
  "capabilities": {
    "streaming": true,
    "cancel": true,
    "tools": true
  }
}
```

### 5.4 Core RPC methods

#### `session.create`

```text
params:
  userId?: string
  metadata?: object
  model?: string

result:
  sessionId: string
  createdAt: string
  metadata?: object
```

#### `session.resume`

```text
params:
  sessionId: string

result:
  sessionId: string
  resumedAt: string
  metadata?: object
```

#### `session.list`

```text
params:
  filter?: object
  limit?: number
  offset?: number

result:
  sessions: [{ sessionId, lastUpdated, metadata }]
```

#### `session.history`

```text
params:
  sessionId: string
  cursor?: string
  limit?: number

result:
  events: [HistoryEvent]
  nextCursor?: string
```

`HistoryEvent`:

```text
{ id, role, text?, structured?, ts }
```

where `role` is one of:

```text
user | assistant | tool | system
```

#### `session.close`

```text
params:
  sessionId: string
  reason?: string

result:
  sessionId: string
  closedAt: string
```

#### `session.sendMessage`

```text
params:
  sessionId: string
  text?: string
  structured?: object
  messageId?: string
  options?: { stream?: boolean }

result:
  requestId: string
  accepted: true
```

The response is an immediate acknowledgement. Streaming is delivered through server notifications.

#### `tool.execute`

```text
params:
  sessionId: string
  toolId: string
  input?: object
  options?: object
  requestId?: string

result:
  requestId: string
```

Tool output must be structured as events rather than raw mixed terminal text.

#### `request.cancel`

```text
params:
  requestId: string
  sessionId?: string
  reason?: string

result:
  requestId: string
  canceled: boolean
```

The daemon must emit a final completion/cancellation signal after cancellation takes effect.

### 5.5 Streaming semantics

Use **server notifications keyed by the original request ID**.

Assistant stream event:

```json
{
  "jsonrpc": "2.0",
  "method": "event.streamChunk",
  "params": {
    "requestId": "r-1001",
    "sessionId": "sess-1",
    "seq": 2,
    "role": "assistant",
    "chunkText": " world",
    "complete": true
  }
}
```

Required properties:

- `requestId` — correlates event to the request;
- `seq` — monotonically increasing ordering;
- `complete` — exactly identifies the terminal event;
- optional structured deltas for non-text content.

Cancellation must result in a terminal event and/or explicit `event.cancelAck`.

### 5.6 Tool streaming

Use structured events:

```text
event.toolOutput
  streamType: stdout | stderr | status

event.toolDone
  exitCode?
  resultMetadata?
```

Do not treat arbitrary tool output as trusted ANSI terminal instructions.

### 5.7 Error codes

Reserve application errors in `-32000..-32099`:

```text
-32001 SESSION_NOT_FOUND
-32002 AUTH_DENIED
-32003 TOOL_EXECUTION_DENIED
-32004 POLICY_VIOLATION
-32005 PROTOCOL_MISMATCH
-32006 CANCELLED
```

Every application error should include machine-readable `error.data` alongside a human-readable message.

### Exit criteria

- RPC contract is written down before dependent UI work.
- Server tests cover each method and major failure class.
- Streaming, cancellation, and version negotiation are tested.

---

## 6. Phase 2 — Implement `StackMindTuiAdapter`

### Goal

Provide a typed, transport-aware client API while keeping RPC mechanics out of UI components.

### Target TypeScript API

```ts
interface StreamChunk {
  requestId: string;
  seq: number;
  role: "assistant" | "tool" | "system";
  text?: string;
  structured?: unknown;
  complete: boolean;
}

interface SessionMeta {
  sessionId: string;
  createdAt: string;
  metadata?: unknown;
}

interface StackMindClient {
  connect(signal?: AbortSignal): Promise<void>;
  disconnect(): Promise<void>;
  health(): Promise<{
    version: string;
    protocolVersion: number;
    capabilities: Record<string, boolean>;
  }>;

  createSession(userId?: string, metadata?: unknown): Promise<SessionMeta>;
  listSessions(filter?: unknown, limit?: number, offset?: number): Promise<SessionMeta[]>;
  resumeSession(sessionId: string): Promise<void>;
  closeSession(sessionId: string, reason?: string): Promise<void>;
  sessionHistory(
    sessionId: string,
    cursor?: string,
    limit?: number
  ): Promise<{ events: unknown[]; nextCursor?: string }>;

  sendMessage(
    sessionId: string,
    text?: string,
    options?: { stream?: boolean },
    signal?: AbortSignal
  ): AsyncIterable<StreamChunk>;

  executeTool(
    sessionId: string,
    toolId: string,
    input?: unknown,
    options?: unknown,
    signal?: AbortSignal
  ): AsyncIterable<unknown>;

  cancelRequest(requestId: string): Promise<{
    requestId: string;
    canceled: boolean;
  }>;
}
```

### Adapter responsibilities

- connection lifecycle;
- request/response correlation;
- server notification routing;
- typed conversion;
- bounded buffering/backpressure;
- `AbortSignal` → `request.cancel` mapping;
- retries only where semantics are safe;
- structured error mapping;
- protocol capability checks.

### Backpressure rule

Never allow an unbounded in-memory stream queue. Coalesce small chunks when possible and bound the amount of retained output.

### Exit criteria

The adapter can prove an end-to-end flow:

```text
create session
    ↓
send message
    ↓
receive ack + streamed notifications
    ↓
render chunks
    ↓
cancel request
    ↓
receive terminal cancellation event
```

---

## 7. Phase 3 — OpenTUI Feasibility Gate

Evaluate OpenTUI / `@opentui/react` first because the target UX requires component rendering, layout, scrolling, keyboard interaction, and potentially mouse input.

### Gate questions

#### Build/runtime

- Does OpenTUI require Bun and/or Zig at build time?
- Are they required at runtime?
- Can local development remain straightforward?
- Do native dependencies complicate packaging?

#### CI

- Can Ubuntu CI runners build reliably?
- What is required on Windows/macOS?
- Are cross-platform binaries needed?
- Can the project reproduce builds deterministically?

#### Performance

Run a representative benchmark:

```text
1 message/sec
500 tokens/sec
30 seconds
streaming continuously
```

Measure:

- input latency;
- frame/render responsiveness;
- CPU;
- memory;
- dropped/coalesced events.

#### Decision

Choose OpenTUI if it meets the build, CI, and responsiveness gates without creating disproportionate release complexity.

Fallback order:

```text
OpenTUI
  ↓
Ink
  ↓
Blessed / another proven terminal UI framework
```

The adapter must not change when the framework changes.

### Exit criteria

A short architecture decision record names the chosen framework and documents the evidence behind the decision.

---

## 8. Phase 4 — Core TUI

### Required screens

#### Chat view

```text
┌─────────────────────────────────────────────────────────┐
│ StackMind • Session sess-abc123                         │
├─────────────────────────────────────────────────────────┤
│ You                                                     │
│ Explain this project                                    │
│                                                         │
│ StackMind                                                │
│ Here is how the system works...                         │
│ [streaming]                                              │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ > Type your message...                                  │
└─────────────────────────────────────────────────────────┘
```

### Required capabilities

- scrollable history;
- streaming output;
- text input;
- Enter to submit;
- focus management;
- terminal resize handling;
- keyboard navigation;
- clear user/assistant/tool/system presentation;
- cancellation shortcut (Ctrl+C and/or Esc according to framework conventions);
- new-message indicator when user has scrolled away from the bottom.

### Rendering strategy

- Keep renderers pure and fast.
- Avoid heavy synchronous JSON parsing inside render cycles.
- Coalesce very small stream chunks where appropriate.
- Auto-scroll only while the user is at the bottom.

### Exit criteria

A normal StackMind chat session is usable without falling back to the legacy CLI.

---

## 9. Phase 5 — Session Management UI

Implement:

- list sessions;
- search/filter if supported by backend;
- select and resume;
- show metadata/timestamps when available;
- close/terminate with confirmation.

### Large-history strategy

Virtualize the message list once the number of messages becomes large. Use history cursors for lazy loading rather than always loading the entire session into memory.

Maintain a small in-memory mapping between visible UI rows and history cursors/IDs so older history can be fetched on demand.

### Exit criteria

Users can move between existing sessions without restarting the TUI.

---

## 10. Phase 6 — Errors, Offline Mode & Cancellation

### Handle explicitly

- daemon unavailable;
- daemon crash;
- connection reset;
- RPC timeout;
- malformed event;
- protocol mismatch;
- session not found;
- policy denial;
- tool failure;
- cancelled request.

### UX rules

- Show concise, actionable errors.
- Do not dump raw stack traces into the conversation by default.
- Preserve the last known session state where possible.
- Make reconnect/retry semantics explicit rather than silently duplicating requests.

### Fault injection

Test:

1. daemon crash mid-stream;
2. malformed stream event;
3. dropped connection;
4. delayed response;
5. protocol mismatch;
6. cancellation while assistant response is streaming;
7. cancellation during tool execution.

### Exit criteria

Failure conditions are represented as deterministic UI states rather than process crashes.

---

## 11. Phase 7 — Security Hardening

### Trust boundary

The TUI must **never directly execute untrusted code**.

All tool/code execution goes through:

```text
TUI → RPC → daemon policy → ToolGateway → sandbox
```

### Requirements

- server-side argument validation;
- server-side authorization;
- capability-scoped handshake;
- local IPC permissions where applicable;
- short-lived bearer token option where stronger authentication is required;
- least-privilege session storage permissions;
- no secrets/tokens in logs;
- no Node `vm` as a security boundary;
- OS/container/VM-level isolation for untrusted execution.

### Terminal sanitization

All AI/tool text must be considered untrusted before rendering.

Default renderer behavior:

- preserve normal printable characters, newline, and tab;
- neutralize control characters;
- neutralize raw CSI sequences such as `ESC [`;
- only permit explicit ANSI formatting from a trusted rendering path.

Binary tool output must never be emitted directly to the terminal renderer.

### Security tests

- ANSI/escape injection;
- malicious structured output;
- unauthorized `tool.execute`;
- policy violation handling;
- capability escalation attempts;
- sandbox boundary regression tests.

### Exit criteria

No TUI feature bypasses daemon-side policy or causes untrusted code to execute on the host.

---

## 12. Phase 8 — Performance & Scalability

### Targets

The TUI remains responsive during:

- long streamed responses;
- high token throughput;
- large sessions;
- large tool output;
- repeated session switching;
- terminal resizing.

### Implementation requirements

- virtualize long histories;
- lazy-load history using cursors;
- bound stream buffers;
- coalesce small streaming chunks;
- avoid synchronous work in the render loop;
- move CPU-heavy client work to worker threads/processes where justified.

### Benchmark scenarios

| Scenario | Measurement |
|---|---|
| 1 msg/sec + 500 tokens/sec for 30s | input/render latency |
| 500+ messages | memory + scrolling |
| 1000+ rendered lines | frame/render responsiveness |
| concurrent sessions | CPU + memory + isolation |
| tool output bursts | backpressure + dropped/coalesced events |

### Exit criteria

Performance baselines and acceptable thresholds are documented before release.

---

## 13. Phase 9 — Logging & Telemetry

### Local logging

Record enough information to debug RPC failures without leaking secrets:

- request/response status;
- method name;
- latency;
- protocol errors;
- cancellation/failure events.

Use log rotation.

Never log:

- bearer tokens;
- credentials;
- sensitive prompt contents unless explicitly configured for debugging.

### Telemetry

Any telemetry should be opt-in and non-PII. At minimum, prefer aggregate version and success/failure information rather than message contents.

---

## 14. Phase 10 — CI/CD & Git Workflow

### Branching

Use GitHub Flow / short-lived feature branches:

```text
main
 │
 ├── feature/rpc-contract
 ├── feature/tui-adapter
 ├── feature/tui-framework
 ├── feature/session-ui
 └── feature/security-hardening
          │
          ▼
         PR
          │
          ▼
         CI
          │
          ▼
        main
```

`main` must remain deployable.

### CI stages

1. format/lint;
2. TypeScript type checking;
3. unit tests;
4. RPC handler tests;
5. adapter tests with mock transport;
6. real daemon integration tests on an ephemeral IPC endpoint;
7. streaming/cancellation tests;
8. headless TUI smoke test in a virtual terminal;
9. fault-injection tests;
10. security tests;
11. OpenTUI/native build tests where applicable;
12. package/build verification.

### Required integration smoke test

CI should prove:

```text
start daemon
  ↓
connect TUI client
  ↓
health/version handshake
  ↓
create session
  ↓
send message
  ↓
receive stream
  ↓
assert visible output
  ↓
cancel or complete
  ↓
close session
```

### Exit criteria

Every PR passes the relevant test/build gates, and every merge to `main` leaves a releasable build.

---

## 15. Phase 11 — Documentation & Release

### Documentation

Update:

- README;
- architecture documentation;
- RPC contract documentation;
- TUI usage guide;
- keyboard shortcuts;
- transport/authentication requirements;
- OpenTUI/Bun/Zig requirements if applicable;
- troubleshooting;
- security model;
- CHANGELOG.

### Release checklist

- automated tests green;
- integration/smoke tests green;
- docs updated;
- version bumped;
- CHANGELOG updated;
- package/binary artifacts produced;
- fresh-machine installation tested;
- cross-platform requirements verified;
- release tag created.

### Rollback

A failed release must be recoverable by moving deployment back to the last stable tag or publishing a hotfix. Faulty CI configuration should be reverted separately from product code.

---

## 16. Milestone Order & PR Breakdown

| PR | Scope | Dependency | Exit condition |
|---|---|---|---|
| PR-0 | Repository/architecture audit | None | Real integration matrix completed |
| PR-1 | RPC contract + protocol types | PR-0 | Contract reviewed and versioned |
| PR-2 | Transport + handshake | PR-1 | Client can connect and negotiate capabilities |
| PR-3 | Session RPCs | PR-2 | Create/resume/list/history/close pass tests |
| PR-4 | Streaming + cancellation | PR-3 | Stream + cancel end-to-end passes |
| PR-5 | Typed `StackMindTuiAdapter` | PR-4 | Async iterable client works against real daemon |
| PR-6 | TUI framework feasibility + spike | PR-5 | Framework decision recorded |
| PR-7 | Core chat TUI | PR-6 | Real chat usable in terminal |
| PR-8 | Session management UI | PR-7 | Browse/resume/close works |
| PR-9 | Fault handling + offline UX | PR-8 | Injected failures do not crash UI |
| PR-10 | Security hardening | PR-9 | Security tests pass |
| PR-11 | Performance/virtualization | PR-10 | Benchmark thresholds pass |
| PR-12 | CI/CD + packaging | PR-11 | CI produces validated artifacts |
| PR-13 | Docs + release | PR-12 | Release checklist complete |

Keep each PR reviewable and independently reversible.

---

## 17. Definition of Done

A milestone is done only when:

- implementation is complete;
- relevant tests are present;
- integration behavior is verified;
- failure paths are tested;
- security implications are reviewed;
- documentation is updated;
- CI passes;
- rollback remains possible.

A release is done only when the full end-to-end workflow succeeds on a fresh environment.

---

## 18. Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| RPC contract churn | Versioned JSON-RPC contract + integration tests |
| Streaming race conditions | Request IDs + sequence numbers + terminal event |
| Cancellation ambiguity | Dedicated `request.cancel` + cancellation acknowledgement |
| Client/daemon mismatch | Handshake + protocol version/capability negotiation |
| OpenTUI build complexity | Early feasibility gate + framework-independent adapter |
| Node event-loop blocking | Async I/O + bounded queues + workers for heavy tasks |
| Terminal injection | Safe renderer + control-sequence neutralization |
| Sandbox escape | Daemon-only execution + OS/container/VM isolation |
| Huge chat histories | Cursor-based history + virtualization |
| Tool-output floods | Structured events + backpressure + bounded buffers |
| Local daemon exposure | IPC permissions and/or short-lived authentication |
| Release failures | Reproducible CI + stable-tag rollback |

---

## 19. Immediate Execution Checklist

Start implementation in this order:

### First

- [ ] Audit actual StackMind repository structure.
- [ ] Confirm current daemon/RPC implementation.
- [ ] Confirm session/tool/policy APIs.
- [ ] Decide JSON-RPC transport.
- [ ] Define protocol version.

### Then

- [ ] Add `health.version` / handshake.
- [ ] Define RPC types and error codes.
- [ ] Implement session endpoints.
- [ ] Implement streaming notifications.
- [ ] Implement `request.cancel`.
- [ ] Implement structured tool events.

### Then

- [ ] Implement `StackMindTuiAdapter`.
- [ ] Add async iterable streaming.
- [ ] Add `AbortSignal` cancellation.
- [ ] Add bounded buffering/backpressure.
- [ ] Prove end-to-end streamed chat with no UI framework dependency.

### Then

- [ ] Run OpenTUI feasibility gate.
- [ ] Build the minimal chat screen.
- [ ] Add session browser.
- [ ] Add reconnect/error UX.
- [ ] Add terminal sanitization.
- [ ] Add virtualization and performance optimization.

### Finally

- [ ] Add full CI matrix.
- [ ] Add security/fault-injection tests.
- [ ] Package for supported platforms.
- [ ] Update README/CHANGELOG/version.
- [ ] Run fresh-environment release verification.

---

## 20. Source Basis

This plan is based on the original StackMind TUI integration audit and the subsequent implementation review. The review strengthens the original plan by making transport, handshake/capability negotiation, server-notification streaming, explicit cancellation, structured tool events, adapter typing, bounded buffering, headless TUI testing, fault injection, terminal sanitization, and OpenTUI feasibility gates explicit. The source review recommends moving directly from Phase 0 into the RPC work while reducing ambiguity first.
