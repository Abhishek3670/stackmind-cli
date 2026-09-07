# StackMind P6 — Open-Source TUI Adoption & Integration

## Mission

Implement **Phase P6 of StackMind** by adopting and integrating an existing open-source agent TUI rather than building a terminal UI from scratch.

P0–P5 are considered complete and verified. The runtime now provides:

- Runtime contract and state machine
- Secure scratch-workspace execution
- Tool Gateway and execution boundary
- Real provider gateway
- Authentic verification and evidence
- Local JSON-RPC runtime daemon
- Governed MCP / IDE integration

P6 must build the **interactive human-facing terminal experience on top of that runtime**.

The TUI must be a **client of StackMind**, not a second agent runtime.

---

# 1. Core Architectural Principle

The target architecture is:

```text
┌──────────────────────────────────────────────────────────┐
│                    USER / DEVELOPER                      │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────┐
│        ADOPTED OPEN-SOURCE TUI                           │
│        (OpenCode / Pi / Crush candidate)                 │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────┐
│              STACKMIND TUI ADAPTER                       │
│  Maps TUI events/actions to StackMind Runtime operations │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────┐
│                STACKMIND RUNTIME DAEMON                  │
│                                                          │
│ Session │ Contract │ Policy │ Tool │ Provider │ Sandbox  │
│ Verification │ Evidence │ Knowledge │ Learning           │
└──────────────────────────────────────────────────────────┘
```

The adopted TUI must **never become the authority for**:

- filesystem access
- shell execution
- contract evaluation
- sandbox policy
- verification
- experience eligibility
- learning eligibility
- session state
- provider authorization

Those remain owned by StackMind.

---

# 2. P6 Objectives

P6 has six objectives:

### P6.1 — Evaluate TUI Candidates

Perform a technical evaluation of at least these three candidates:

1. **OpenCode**
2. **Pi**
3. **Crush**

Do not select based solely on visual quality.

Evaluate:

- architecture
- TUI/runtime separation
- licensing
- extensibility
- session model
- provider abstraction
- tool architecture
- event model
- streaming support
- cancellation
- approval/HITL support
- diff presentation
- workspace/session management
- API/client architecture
- build/dependency complexity
- ability to replace direct filesystem/process execution
- ability to connect to StackMind's daemon

Produce a short `P6_TUI_CANDIDATE_EVALUATION.md`.

---

# 3. Candidate Selection

Use a weighted score rather than subjective preference.

Recommended scoring:

| Criterion | Weight |
|---|---:|
| StackMind runtime compatibility | 25% |
| Separation of UI and execution runtime | 20% |
| Extensibility / adaptability | 15% |
| Session & interaction model | 10% |
| Tool / provider integration flexibility | 10% |
| Licensing | 10% |
| UX quality | 5% |
| Maintenance / ecosystem health | 5% |

The selected candidate must be technically defensible.

Do not select a candidate simply because it looks better.

---

# 4. Hard Requirement: No Runtime Bypass

This is the most important P6 requirement.

The resulting TUI must not create a path like:

```text
TUI
  → subprocess
  → filesystem
```

or:

```text
TUI
  → direct shell
```

or:

```text
TUI
  → provider
  → uncontrolled tools
```

Instead:

```text
TUI
  → StackMind Adapter
  → Runtime Daemon
  → Contract / Policy
  → Tool Gateway
  → Sandbox
  → Verification
```

Any candidate whose architecture fundamentally requires bypassing this model must be rejected or heavily isolated.

---

# 5. P6.2 — Build the StackMind TUI Adapter

Once the winning candidate is selected, create a thin adapter layer.

The adapter should translate TUI actions into StackMind runtime calls.

Examples:

```text
:new
   → session.create

:resume
   → session.get

:pause
   → session.pause

:cancel
   → session.cancel

user prompt
   → provider/session turn

tool request
   → StackMind Tool Gateway

approve
   → StackMind authorization/review operation

show diff
   → StackMind workspace/review API

show verification
   → StackMind verification/evidence API
```

Do not place business logic in the adapter that belongs in the runtime.

---

# 6. P6.3 — StackMind-Native Interaction Model

The TUI should expose StackMind concepts directly where useful.

At minimum, support:

### Session

Display:

- session ID
- state
- active contract
- provider/model
- token usage
- elapsed time
- current attempt

### Agent activity

Display:

- model output
- tool calls
- tool results
- command execution
- errors
- cancellations
- retries

### Contract

Display:

- allowed scope
- denied scope
- read-only status
- budget
- execution policy

### Sandbox

Display:

- workspace location
- sandbox status
- changed files
- process state

### Verification

Display:

- scope verification
- state verification
- AST/code verification
- behavioral verification
- security verification
- outcome verification

### Review

Display:

- diff
- files changed
- verification result
- proposed write-back/commit
- approval state

### Learning

Display where appropriate:

- experience captured
- learning eligibility
- skill/experience status

Do not make the TUI responsible for determining learning eligibility.

---

# 7. P6.4 — Streaming Interaction

The TUI must support a real interactive agent loop.

Required behavior:

```text
User
 ↓
Prompt
 ↓
Runtime session
 ↓
Provider stream
 ↓
TUI incremental rendering
 ↓
Tool request
 ↓
StackMind authorization
 ↓
Tool execution
 ↓
Tool result
 ↓
Provider continuation
 ↓
Verification
 ↓
Final result
```

Support:

- incremental model output
- tool execution events
- progress indicators
- cancellation
- errors
- retry states
- session continuation

The UI must not block on a one-shot synchronous request.

---

# 8. P6.5 — Human Approval / HITL

The TUI must expose StackMind authorization decisions.

Examples:

```text
Agent requests:
WRITE billing/invoice.py

Contract:
ALLOW billing/**

Decision:
✓ Allowed
```

or:

```text
Agent requests:
WRITE production/config.yaml

Contract:
DENY production/**

Decision:
✗ Blocked
```

For operations requiring explicit human approval:

```text
┌──────────────────────────────────────────────┐
│ Approval Required                            │
│                                              │
│ Operation: write_file                        │
│ Path: billing/invoice.py                     │
│ Reason: agent requested modification         │
│                                              │
│ [Approve]   [Reject]   [Inspect Diff]        │
└──────────────────────────────────────────────┘
```

The TUI displays and submits the decision.

It does not independently authorize the operation.

---

# 9. P6.6 — Session Persistence

The adopted TUI must integrate with StackMind's existing session daemon.

The TUI should be able to:

- create sessions
- list sessions
- resume sessions
- pause sessions
- cancel sessions
- inspect previous events
- reconnect after TUI restart

Do not introduce a competing session database unless absolutely necessary.

StackMind remains the source of truth.

---

# 10. P6.7 — Diff and Verification UX

A completed agent task should produce a clear review experience.

Preferred flow:

```text
Agent finished
      │
      ▼
Verification
      │
      ▼
┌───────────────────────────────┐
│ Verification: PASS            │
│                               │
│ Scope       ✓                 │
│ State       ✓                 │
│ AST         ✓                 │
│ Behavioral  ✓                 │
│ Security    ✓                 │
│ Outcome     ✓                 │
└───────────────────────────────┘
      │
      ▼
Review Diff
      │
      ▼
Approve / Reject
      │
      ▼
Write-back
```

Never represent a change as successfully completed solely because the provider said it completed.

StackMind verification remains authoritative.

---

# 11. P6.8 — Provider Independence

The TUI must not hard-code a particular provider.

The runtime should continue to own provider selection.

Example:

```text
StackMind
 ├── OpenAI-compatible provider
 ├── Anthropic-compatible provider
 ├── future providers
 └── local providers
```

The TUI may expose model/provider selection, but selection should be sent to StackMind's Provider Gateway.

---

# 12. P6.9 — Licensing and Upstream Strategy

Before adopting any candidate, explicitly determine:

- license
- fork requirements
- redistribution rights
- attribution requirements
- dependency licenses
- modification requirements
- upstream contribution path

Choose one strategy:

### Strategy A — Fork

Maintain a StackMind-specific fork.

### Strategy B — Embed

Use the TUI as a dependency/library where practical.

### Strategy C — Reuse UI framework/components

Reuse the candidate's TUI foundation while implementing StackMind-specific application logic.

Prefer the approach that minimizes long-term divergence while preserving StackMind's runtime authority.

Document the decision.

---

# 13. P6 Deliverables

Produce:

```text
docs/
  P6_TUI_CANDIDATE_EVALUATION.md
  P6_TUI_ARCHITECTURE.md
  P6_TUI_ADOPTION_DECISION.md
```

and the selected implementation, for example:

```text
tui/
  ...
```

or the appropriate structure required by the selected project.

Also add tests for:

- runtime connection
- session lifecycle
- streaming
- tool events
- cancellation
- approval flow
- diff retrieval
- verification display
- reconnect/resume
- error handling

---

# 14. Explicit Non-Goals

Do NOT use P6 to:

- redesign the runtime kernel
- replace the Tool Gateway
- redesign the Provider Gateway
- weaken sandbox enforcement
- move authorization into the UI
- create a second session manager
- bypass MCP/runtime policy
- add multi-agent orchestration
- build a custom sandbox
- introduce unnecessary infrastructure
- rewrite StackMind's knowledge engine

Do not rebuild functionality that P0–P5 already provide unless the investigation proves an integration incompatibility.

---

# 15. P6 Exit Gates

P6 is complete only when all of the following are true.

### Gate 1 — Candidate Selection

One TUI has been selected with documented technical and licensing justification.

### Gate 2 — StackMind Runtime Ownership

The TUI operates entirely through the StackMind runtime interface.

### Gate 3 — No Direct Execution

No agent operation initiated through the TUI can bypass:

```text
Contract
→ Policy
→ Tool Gateway
→ Sandbox
```

### Gate 4 — Interactive Sessions

A user can run a real multi-turn agent session from the TUI.

### Gate 5 — Streaming

Model output and tool activity stream interactively.

### Gate 6 — Human Control

Users can pause, cancel, approve, reject, and inspect operations.

### Gate 7 — Verification

Task completion displays authoritative StackMind verification results.

### Gate 8 — Persistence

The TUI can reconnect to and resume StackMind sessions managed by the daemon.

### Gate 9 — Regression

All existing P0–P5 tests remain green.

### Gate 10 — TUI Independence

The runtime remains usable without the TUI.

For example:

```text
CLI      → Runtime
MCP      → Runtime
Future UI → Runtime
TUI      → Runtime
```

The TUI must be replaceable.

---

# 16. Definition of Done

P6 is successful when a developer can launch:

```bash
stackmind tui
```

and experience StackMind as a first-class agent environment:

```text
┌─────────────────────────────────────────────────────────┐
│ StackMind                                      session  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│ User                                                    │
│ Fix the invoice validation bug.                         │
│                                                         │
│ Agent                                                   │
│ I found the validation path...                           │
│                                                         │
│ ┌─ Tool ──────────────────────────────────────────────┐ │
│ │ read_file billing/invoice.py                         │ │
│ │ ✓ allowed by contract                                │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Agent                                                   │
│ I will update the validator.                            │
│                                                         │
│ ┌─ Tool ──────────────────────────────────────────────┐ │
│ │ write_file billing/invoice.py                        │ │
│ │ ✓ sandboxed                                          │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Verification                                             │
│ ✓ Scope  ✓ State  ✓ AST  ✓ Tests  ✓ Security  ✓ Goal  │
│                                                         │
│ [Review Diff] [Approve] [Reject]                        │
└─────────────────────────────────────────────────────────┘
```

The important outcome is not the visual interface itself.

The outcome is:

> **StackMind becomes the environment in which the agent operates, while the adopted TUI remains only the human interaction surface.**

---

# 17. Engineering Rule

When deciding between implementing something in the TUI and implementing it in StackMind Runtime:

**Put it in StackMind Runtime.**

The TUI should display state and request actions.

The runtime should enforce truth.

Never reverse those responsibilities.