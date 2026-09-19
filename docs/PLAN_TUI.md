# StackMind TUI Integration — Implementation Plan

## 1. Objective

Build a modern interactive StackMind terminal UI (TUI) as a **separate client frontend** that communicates with the existing StackMind daemon over RPC.

### Architectural constraint

Do **not** embed a second StackMind runtime inside the TUI.

The target architecture is:

```text
TUI Client
    │
    ▼
StackMindTuiAdapter
    │
    ▼
LocalDaemon / JSON-RPC
    │
    ├── SessionManager
    ├── Policy / Contract Engine
    ├── ToolGateway / Sandbox
    └── SessionStore / Database
```

The daemon remains the trusted runtime. The TUI is responsible for presentation, interaction, and translating UI actions into RPC requests.

---

## 2. Success Criteria

The integration is complete when:

- The daemon exposes a stable, documented RPC contract for TUI operations.
- A `StackMindTuiAdapter` provides a clean client abstraction over RPC.
- Users can create, resume, list, interact with, and terminate sessions from the TUI.
- Assistant responses can stream into the UI without blocking interaction.
- Chat history supports scrolling and usable keyboard navigation.
- Tool execution remains controlled by the daemon and its sandbox.
- RPC failures, daemon disconnects, cancellations, and timeouts are handled gracefully.
- Client/daemon protocol versions are compatible and detectable.
- CI validates RPC, integration, UI, security, and build behavior.
- `main` remains deployable throughout development.
- Documentation, changelog, versioning, and release artifacts are updated with the feature.

---

## 3. Phase 0 — Repository & Architecture Validation

### Goal

Validate assumptions from the audit against the actual StackMind codebase before changing implementation.

### Tasks

- Locate and inspect:
  - `LocalDaemon`
  - `SessionManager`
  - Policy / Contract Engine
  - `ToolGateway` / Sandbox
  - SessionStore / persistence
  - existing CLI entry points
  - existing `StackMindTuiAdapter` scaffolding
- Identify actual method names, transports, ports, schemas, and lifecycle behavior.
- Determine whether RPC already exists and which endpoints are missing.
- Confirm current authentication and authorization behavior.
- Confirm how streaming, cancellation, and asynchronous events are currently represented.
- Record actual file/module locations in implementation docs.

### Exit criteria

- Actual backend integration points are documented.
- Assumptions from the audit are marked as confirmed or rejected.
- No implementation depends on placeholder method or file names.

---

## 4. Phase 1 — Stabilize the RPC Contract

### Goal

Create a stable API boundary between the TUI and StackMind runtime.

### Required methods

At minimum:

```text
session.create
session.resume
session.sendMessage
session.history
session.list
session.close
tool.execute
```

Add cancellation / interrupt support where required.

### Contract requirements

Define:

- JSON-RPC version
- request/response schemas
- parameter types
- result types
- error codes
- authentication requirements
- protocol version
- streaming semantics
- cancellation semantics
- timeout behavior

### Example

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session.sendMessage",
  "params": {
    "sessionId": "sess-abc123",
    "text": "Hello"
  }
}
```

Streaming events should have an explicit completion state, for example:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "role": "assistant",
    "text": "Hello...",
    "complete": false
  }
}
```

### Tests

- Unit-test every RPC handler.
- Test invalid parameters.
- Test missing sessions.
- Test policy failures.
- Test tool failures.
- Test connection errors.
- Test stream completion and interruption.
- Test protocol/version mismatch.

### Exit criteria

- RPC contract is documented.
- Required methods work end-to-end.
- RPC tests pass in CI.
- Breaking changes require an explicit protocol/version update.

---

## 5. Phase 2 — Implement `StackMindTuiAdapter`

### Goal

Hide RPC transport details behind a typed client interface.

### Core interface

```ts
interface StackMindClient {
  createSession(userId?: string): Promise<string>;
  sendMessage(
    sessionId: string,
    text: string
  ): AsyncIterable<{ text: string; role: string }>;
  listSessions(): Promise<string[]>;
  resumeSession(sessionId: string): Promise<void>;
}
```

Extend this interface with:

- session history
- close/terminate
- tool execution
- cancellation
- health/version checks
- structured error handling

### Adapter responsibilities

- Manage RPC connection lifecycle.
- Marshal UI requests into RPC requests.
- Validate responses.
- Convert streamed responses into async events.
- Translate daemon errors into UI-safe errors.
- Keep transport details out of UI components.

### Tests

Build integration tests against a mock or test daemon:

```text
UI input
   ↓
Adapter
   ↓
RPC request
   ↓
Mock daemon
   ↓
RPC response/stream
   ↓
Adapter
   ↓
UI event
```

### Exit criteria

A minimal text-mode client can:

1. create a session,
2. send a message,
3. receive streamed output,
4. display the response,
5. resume a session,
6. close a session.

---

## 6. Phase 3 — Choose and Integrate the TUI Framework

### Preferred direction

Evaluate **OpenTUI / `@opentui/react`** first because the target design is OpenCode-compatible and requires:

- component-based rendering
- flexbox-style layout
- scrollable regions
- keyboard handling
- interactive widgets

### Decision gate

Before committing to OpenTUI, verify:

- Bun requirements
- Zig/native build requirements
- supported operating systems
- CI compatibility
- packaging/distribution complexity
- development experience with the existing StackMind TypeScript/Node stack
- runtime performance during streaming

### Fallback

If OpenTUI is impractical, evaluate a pure TypeScript alternative such as Ink or Blessed.

The adapter must remain framework-independent so the RPC layer does not need to change when the UI framework changes.

### Exit criteria

A framework decision is documented with:

- rationale
- build/runtime requirements
- CI impact
- packaging impact
- fallback plan

---

## 7. Phase 4 — Build the Core UI

### Initial screens

#### Chat session view

```text
┌───────────────────────────────────────────────┐
│ StackMind / Session: sess-abc123              │
├───────────────────────────────────────────────┤
│ User: Hello                                   │
│                                               │
│ Assistant: ...streaming response...           │
│                                               │
│                                               │
├───────────────────────────────────────────────┤
│ > Type your message...                        │
└───────────────────────────────────────────────┘
```

### Required capabilities

- message history
- streaming assistant output
- scrolling
- text input
- submit on Enter
- focus management
- keyboard shortcuts
- terminal resize handling
- clear user/assistant distinction

### Later capabilities

- sidebar
- session browser
- settings/info panel
- optional mouse interactions

### Exit criteria

A user can complete a normal chat session entirely from the TUI without falling back to the legacy CLI.

---

## 8. Phase 5 — Session Management UI

### Tasks

Implement:

- list sessions
- select a previous session
- resume a session
- display session metadata where available
- close/terminate a session
- confirm destructive actions

### Tests

- multiple sessions
- selection/navigation
- resume success
- missing/deleted session
- session termination
- concurrent sessions

### Exit criteria

Users can move between existing sessions without restarting the application.

---

## 9. Phase 6 — Error Handling, Cancellation & Offline Behavior

### Tasks

Handle:

- daemon unavailable
- connection reset
- RPC timeout
- malformed response
- protocol mismatch
- policy denial
- tool execution failure
- interrupted generation
- user cancellation

### UX expectations

Errors should be visible and actionable without dumping raw stack traces into the chat.

Long-running operations must be cancellable where the backend supports cancellation.

### Tests

Use fault-injection tests for:

- daemon shutdown during streaming
- delayed responses
- dropped connections
- malformed events
- cancelled tool execution

---

## 10. Phase 7 — Security Hardening

### Trust boundary

The TUI must **not** become a new code-execution environment.

All privileged execution remains in the daemon / ToolGateway / Sandbox.

### Requirements

- Authenticate TUI → daemon where appropriate.
- Use localhost/socket restrictions where applicable, but do not assume local access is automatically trusted.
- Sanitize terminal-rendered content.
- Prevent arbitrary ANSI/escape-sequence injection where unintended.
- Validate RPC arguments.
- Enforce authorization server-side.
- Keep code execution inside real isolation.
- Do not use Node's native `vm` module as a security boundary.
- Prefer containers, subprocess isolation, Firecracker-style VMs, or other dedicated sandboxing mechanisms already approved by the backend architecture.

### Security testing

- input/injection tests
- terminal escape-sequence tests
- unauthorized RPC attempts
- sandbox boundary tests
- static/dependency analysis
- security regression tests

### Exit criteria

No TUI feature can bypass daemon policy or directly execute untrusted code on the host.

---

## 11. Phase 8 — Performance & Scalability

### Targets

The TUI should remain responsive during:

- streamed AI responses
- large chat histories
- tool output
- multiple sessions
- terminal resizing

### Tasks

- Benchmark large histories (target: ~1000 messages/lines).
- Avoid rendering the entire history unnecessarily.
- Use pagination or virtual scrolling when needed.
- Keep heavy work off the Node event loop.
- Use worker threads/processes for CPU-heavy client tasks if required.
- Apply stream backpressure.
- Bound memory used by retained chat data.

### Exit criteria

Performance tests establish acceptable CPU, memory, latency, and rendering behavior.

---

## 12. Phase 9 — CI/CD & Git Workflow

### Branching

Use GitHub Flow:

```text
main
  │
  ├── feature/rpc-contract
  ├── feature/tui-adapter
  ├── feature/opentui-integration
  └── feature/session-ui
        │
        ▼
       PR
        │
        ▼
       CI
        │
        ▼
      merge → main
```

Keep `main` deployable.

### PR requirements

Every PR should include:

- implementation
- relevant tests
- documentation updates when behavior/contracts change
- lint/type checks
- security implications where relevant

### CI stages

1. lint
2. type checking
3. unit tests
4. RPC integration tests
5. TUI integration tests
6. security/static analysis
7. build/package
8. end-to-end smoke test

### Exit criteria

Every merge to `main` produces a validated build.

---

## 13. Phase 10 — Documentation & Release

### Documentation

Update:

- README
- developer architecture docs
- RPC/API documentation
- TUI usage instructions
- troubleshooting
- dependency/build requirements
- security notes
- CHANGELOG

Include practical controls such as:

```text
Arrow keys → navigate history
Enter      → send message
Ctrl+C     → exit
```

### Release

Before tagging:

- all tests green
- documentation complete
- version bumped
- changelog updated
- binaries/packages produced
- fresh-machine installation tested
- Bun/Zig requirements verified if OpenTUI is used

### Rollback

For a failed release:

- revert to the last stable tag, or
- publish a hotfix release.

A faulty CI change should be reverted independently of product code.

---

## 14. Milestone Order

| # | Milestone | Priority | Effort |
|---|---|---|---|
| 0 | Repository & architecture validation | Critical | Low |
| 1 | RPC endpoint stabilization | High | Medium |
| 2 | `StackMindTuiAdapter` prototype | High | Medium |
| 3 | TUI framework integration | High | High |
| 4 | Core chat UI/layout | High | Medium |
| 5 | Session management UI | Medium | Low |
| 6 | Error handling / offline mode | Medium | Low |
| 7 | Security hardening | High | High |
| 8 | Performance tuning | Medium | Medium |
| 9 | CI/CD | High | Low |
| 10 | Documentation & release | Medium | Low |

---

## 15. Definition of Done

A milestone is **Done** only when:

- code is complete,
- tests are passing,
- failure paths are tested,
- documentation is updated,
- CI passes,
- integration behavior is verified,
- no known security regression is introduced,
- rollback remains possible.

---

## 16. Key Risks

| Risk | Mitigation |
|---|---|
| RPC contract instability | Version the protocol and test handlers |
| OpenTUI native build complexity | Validate Bun/Zig early; keep framework isolated |
| Node event-loop blocking | Async I/O, streaming, worker threads where necessary |
| Sandbox escape | Keep execution in isolated backend sandbox |
| Terminal injection | Sanitize rendered output |
| Huge chat histories | Pagination/virtual scrolling |
| Client/daemon version mismatch | Protocol version negotiation |
| Backend disconnects | Timeouts, retries, reconnect/error states |
| Dependency failures | CI verification and documented runtime requirements |

---

## 17. Implementation Rules

1. **Preserve the existing StackMind runtime model.**
2. **Keep the TUI as a client, not a second backend.**
3. **Treat RPC as the primary integration boundary.**
4. **Keep the adapter independent from the chosen TUI framework.**
5. **Keep daemon-side security authoritative.**
6. **Design streaming and cancellation into the protocol early.**
7. **Do not merge untested RPC/UI changes into `main`.**
8. **Prefer reversible PR-sized changes over large rewrites.**

---

## 18. Immediate Next Steps

### Step 1
Audit the actual repository and replace all placeholder module/function names in this plan with confirmed implementation locations.

### Step 2
Write the first version of the RPC contract and enumerate existing vs missing daemon methods.

### Step 3
Implement and test the RPC endpoints before building complex UI features.

### Step 4
Implement `StackMindTuiAdapter` and prove an end-to-end streamed chat flow.

### Step 5
Run the OpenTUI feasibility gate and make the framework decision.

### Step 6
Build the smallest usable chat TUI first, then add session management, errors, security hardening, and performance work incrementally.

---

## Source Basis

This plan is derived from the supplied **StackMind TUI Integration – Executive Summary / audit report**. The source emphasizes a separate TUI client, RPC-based integration, `StackMindTuiAdapter`, OpenTUI/OpenCode compatibility, staged implementation, security isolation, performance, CI/CD, and a reversible Git workflow.
