# StackMind CLI/TUI — Final Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`  
**Target branch:** `feat/p6-open-source-tui`  
**Document status:** **FINAL / READY FOR IMPLEMENTATION**  
**Scope:** P6 governed CLI/TUI client foundation followed by P7 autonomous multi-role engineering delivery  
**Primary source documents:** `STACKMIND_CLI.md`, `PLAN_P7_PREREQUISITES.md`, `PLAN_TUI_v7.md`  
**Implementation principle:** The TUI is a client; StackMind is the governance/orchestration runtime; the existing `AgentRunner`/Harness is the single governed execution path.

---

## 0. Authority, Truth, and Document Roles

This plan establishes the implementation order and authority model for the work.

### 0.1 Document authority

The project uses three documentation layers:

| Document | Authority | Purpose |
|---|---|---|
| `pyproject.toml` | **Authoritative** | Package version, dependencies, entry point |
| `docs/runtime-truth/P6-P7-baseline.md` | **Authoritative current-state** | What actually exists on the target branch |
| `PLAN_STACKMIND_CLI_FINAL.md` | **Authoritative implementation plan** | What will be implemented and in what order |
| `STACKMIND_CLI.md` | **Target-state / product description** | User-facing architecture and intended capabilities |
| `README.md` | **Shipped-behavior documentation** | Must describe only implemented behavior |

Until `PRE-P7-0` is completed, `STACKMIND_CLI.md` must be treated as a target-state document, not an implementation baseline.

No implementation decision may rely on a capability being described as "GA", "shipped", or "available" in prose unless the authoritative runtime baseline confirms it.

### 0.2 Version authority

The package version is read from the project's actual version source.

Documentation and RPC responses must not hard-code a release version.

`StackMind package version` and `JSON-RPC protocol version` are separate values.

### 0.3 Status vocabulary

Capability status in current-state documentation must use:

- `IMPLEMENTED`
- `PARTIALLY_IMPLEMENTED`
- `PLANNED`
- `DEPRECATED`
- `INCORRECT`

No planned API is documented as shipped.

---

# 1. Product Objective

Build a production-quality StackMind CLI with an OpenCode-like terminal experience that acts as the **user-facing control plane** for the StackMind runtime.

The TUI does not execute code, call providers directly, or own governance.

StackMind provides:

- Contracts
- Work Orders
- Operation lifecycle
- Agent Role routing
- Knowledge Compiler / Knowledge API
- Harness execution
- verification
- D025 policy
- approvals
- audit
- persistence
- reconnect/replay

Execution backends perform the actual agent/model work.

## 1.1 Canonical user workflow

Normal delivery requires three user interactions:

```text
Interaction 1
User submits initial project objective.

Interaction 2
Architecture / StackMind Orchestrator produces PLAN.md.
User approves or rejects the plan.

Autonomous execution
StackMind creates, dispatches, governs, verifies, and promotes Work Orders.

Interaction 3
System presents final project delivery and completion summary.
```

A mandatory D025 or policy approval may interrupt autonomous execution, but that is an exceptional governance gate rather than the normal workflow.

## 1.2 Canonical architecture

```text
USER
  ↓
STACKMIND TUI
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
   ├── Agent Backend
   │    ├── Codex
   │    ├── AGY
   │    └── Claude
   └── Model Backend
        └── Ollama / local model runtime
```

## 1.3 Terminology

| Concept | Definition |
|---|---|
| **TUI** | User-facing terminal interface |
| **StackMind Runtime** | Governance, orchestration, Knowledge API, Harness, lifecycle, verification, audit |
| **Agent Role** | Logical responsibility such as Architecture, Backend, Frontend, Q/A, GitOps |
| **Execution Backend** | Runtime used to perform work for an Agent Role |
| **Agent Backend** | Agent-native execution system such as Codex, AGY, Claude |
| **Model Backend** | Model-serving runtime such as Ollama/local models |
| **Provider** | Connectivity/configuration and credential source associated with an Execution Backend; not a logical role and not separately RPC-addressable |
| **Work Order** | Durable governed assignment derived from an approved plan |
| **Operation** | Runtime execution/attempt of work, with lifecycle and cancellation |
| **Contract** | Identity, scope, permissions, budgets, and policies governing work |
| **Subagent** | A child runtime Operation executing a child Work Order under a parent Operation |
| **Plan** | A durable planning revision describing project intent and Work Order decomposition |

---

# 2. Non-Negotiable Architecture Rules

1. **TUI is a client, never the runtime.**
2. **StackMind is the governance authority.**
3. **The existing `AgentRunner`/Harness is the single authoritative governed execution path.**
4. **No second daemon coding engine may be introduced.**
5. **No direct backend/provider calls from the TUI.**
6. **No direct tool execution from the TUI.**
7. **All privileged actions cross the StackMind runtime boundary.**
8. **Agent Roles are logical responsibilities, not vendor identities.**
9. **Execution Backend selection is runtime configuration, not a live chat command.**
10. **Provider credentials remain daemon-side.**
11. **Cancellation is operation-scoped.**
12. **Cancelling one operation never terminates its session or unrelated siblings.**
13. **Parent cancellation cascades to non-terminal children.**
14. **Child Contracts can never exceed parent Contract authority.**
15. **Work Order identity and Operation identity remain distinct.**
16. **A retry preserves `work_order_id` and creates a new `operation_id`.**
17. **Every tool action uses the same authorization → execution → verification → audit boundary.**
18. **D025 applies to every agent, backend, and subagent.**
19. **Knowledge retrieval is Contract-gated.**
20. **One canonical event sequence is used for replay/reconnect.**
21. **Reconnect reconstructs observable state rather than inventing a new state.**
22. **No Work Order is complete while a required child is non-terminal or failed.**
23. **No backend receives authority bypass because it is local, trusted, or well-known.**
24. **Normal execution after plan approval is autonomous.**
25. **The TUI does not decide authorization.**
26. **P7 does not begin until P6 is fully accepted.**
27. **No prerequisite phase silently becomes a P7 implementation phase.**

---

# 3. Final Architectural Decisions

These are decisions, not future questions.

## 3.1 TUI language and packaging

**Decision: Python-native TUI.**

The P6/P7 client remains inside the Python package.

```text
stackmind
 ├── CLI
 ├── daemon client
 └── TUI
```

Do not introduce a TypeScript/Bun/Zig renderer path for this milestone.

### Renderer rule

Use the existing Python/Rich rendering foundation for the P6 proving UI.

A higher-level Python TUI framework may be introduced later only if it materially improves P7 interaction complexity. Such a change must remain Python-native and must not change the package/process architecture.

`@opentui/react`, Bun, Zig, Ink, and Blessed are **out of scope** for this implementation plan.

## 3.2 Transport

**Decision: HTTP + JSON-RPC 2.0.**

Do not introduce gRPC or a second application transport.

The transport may expose a notification-capable stream using the existing HTTP server architecture, subject to the P6 transport validation gate.

P7 uses the same transport.

## 3.3 Workspace isolation

**Decision: one Git worktree per concurrent Work Order.**

The canonical project is never the autonomous worker's write directory.

```text
Canonical Project / repository
       │
       ├── worktree WO-001
       ├── worktree WO-002
       ├── worktree WO-003
       └── integration/promotion workspace
```

Each Work Order records:

- base commit SHA
- candidate branch/ref
- workspace path
- parent Work Order, if applicable
- Contract
- execution Operation
- promotion status

The existing advisory write lock remains useful for serialized critical sections but is **not** the concurrency strategy.

## 3.4 Promotion

Promotion is serialized against the canonical project.

```text
Work Order workspace
      ↓
Harness verification
      ↓
candidate commit / candidate diff
      ↓
fresh canonical-base conflict check
      ↓
serialized promotion
      ↓
canonical branch
```

Rules:

- no force-push style mutation of canonical history from the Work Order loop;
- no direct copying of unverified files into canonical;
- conflict detection occurs before promotion;
- conflict resolution happens outside the worker workspace;
- a stale candidate is rebased/merged in an isolated integration workspace;
- unresolved conflict fails or re-plans the Work Order;
- successful sibling Work Orders are not automatically rolled back merely because a later sibling fails;
- project completion remains blocked until all required Work Orders pass.

## 3.5 Autonomous limits

Initial default limits are explicit and configurable:

| Control | Default |
|---|---:|
| Plan revisions | 3 |
| Work Order retries | 2 |
| Operation timeout | 30 min |
| Tool calls / Operation | 100 |
| Commands / Operation | 50 |
| Subagents / parent | 8 |
| Maximum subagent depth | 2 |
| Token budget | backend-configured; Harness default remains bounded |
| Cost budget | backend-configured; required when backend reports monetary usage |

Budgets are enforced by the runtime, not merely displayed in the TUI.

A budget exhaustion is a deterministic terminal condition.

## 3.6 Plan revision policy

```text
DRAFT
  ↓
AWAITING_APPROVAL
  ├── APPROVED
  └── REJECTED
          ↓
       REVISE
          ↓
     new plan version
          ↓
   AWAITING_APPROVAL
```

Initial default: maximum three revisions after rejection.

Exhausting the revision budget places the project into a terminal `PLAN_BLOCKED` state and requires explicit re-start/reconfiguration rather than an infinite loop.

## 3.7 Work Order failure policy

A required Work Order failure means:

```text
Project ≠ COMPLETE
```

Successful siblings may remain valid promotion candidates.

The Architecture / StackMind Orchestrator may re-plan the failed Work Order or dependent Work Orders.

Automatic rollback of already-promoted successful siblings is not the default.

Rollback is required only when verification determines that a later failure invalidates the already-promoted project state or a policy explicitly requires transactional rollback.

---

# 4. Pre-Implementation Gate A — Repository and Documentation Truth

## PRE-P7-0

**Time box: 30–60 minutes.**

Create:

`docs/runtime-truth/P6-P7-baseline.md`

Reconcile `STACKMIND_CLI.md` against:

- `validators/kernel/daemon/manager.py`
- `validators/kernel/daemon/protocol.py`
- `validators/kernel/daemon/server.py`
- `validators/kernel/daemon/events.py`
- `validators/kernel/daemon/storage.py`
- `validators/kernel/tui/adapter.py`
- `validators/kernel/tui/client.py`
- `validators/harness/runner.py`
- `cli/main.py`
- `pyproject.toml`

At minimum classify:

- `stackmind tui`
- TUI commands
- operation cancellation
- streaming
- HITL approval
- prompt/turn execution
- session history
- capability negotiation
- daemon transport
- `:diff`
- `:matrix`

### Deliverable

```text
docs/runtime-truth/P6-P7-baseline.md
```

Required table:

| Capability | Actual branch state | Existing documentation | Required action |
|---|---|---|---|

### Exit gate

PASS only when:

- no material current-state contradiction remains;
- release version comes from the real version source;
- planned APIs are marked planned;
- `STACKMIND_CLI.md` is explicitly understood as target-state documentation.

---

# 5. Pre-Implementation Gate B — Language and Packaging

## PRE-P7-1

**Time box: 30 minutes.**

Record the final decision:

> Python-native TUI; same Python package and process architecture as the CLI/runtime client.

No parallel TypeScript renderer is permitted.

### Exit gate

PASS when:

- packaging is defined;
- CI requires no second frontend toolchain;
- P6-2 onward assumes Python-native client architecture;
- OpenTUI feasibility evaluation has been removed as a language decision.

---

# 6. P6 — Runtime and Client Foundation

P6 is the foundational milestone.

P6 must establish a correct daemon/client vertical slice before any P7 autonomous delivery capability is implemented.

## P6-0 — Targeted Runtime Preflight

**Time box: 30–60 minutes.**

Verify only:

- `SessionManager.cancel_session()`
- `begin_operation()`
- `complete_operation()`
- active cancellation handle ownership
- session transition behavior
- operation journal behavior
- emitted events
- persistence/recovery
- existing cancellation tests
- actual caller owning runtime work

Answer:

1. Where is an Operation created?
2. Where is its cancellation handle stored?
3. What path currently makes a session terminal?
4. Which component actually observes cancellation?
5. Which records/events define terminal Operation state?

Deliverable: short implementation note.

No TUI design or backend design belongs here.

---

## P6-1 — Operation Lifecycle + Cooperative Cancellation

This is a **runtime correctness milestone**, not merely a manager patch.

### Required model

```text
Session lifecycle
      ≠
Operation lifecycle
```

A session may remain `RUNNING`, `WAITING`, or `PAUSED` after an Operation is cancelled.

### Required operation states

```text
REQUESTED
AUTHORIZED
RUNNING
COMPLETED
FAILED
CANCEL_REQUESTED
CANCELLED
```

### Required API

```python
begin_operation(
    session_id,
    operation_name,
    metadata,
    parent_operation_id=None,
    work_order_id=None,
    contract_scope=None,
) -> operation_id, cancellation_handle
```

```python
cancel_operation(
    operation_id,
    cascade=False,
) -> cancellation acknowledgement
```

```python
complete_operation(
    operation_id,
    result=None,
) -> terminal state
```

### Cancellation semantics

`cancel_operation()` must:

1. validate the Operation;
2. mark `CANCEL_REQUESTED`;
3. signal the cancellation mechanism;
4. journal the cancellation request;
5. allow the executing runtime to observe cancellation;
6. emit the corresponding lifecycle event;
7. transition to `CANCELLED` only after execution actually terminates;
8. preserve the Session.

Cancellation accepted is not equivalent to execution already stopped.

### Race rules

The implementation must guarantee:

```text
CANCEL_REQUESTED + worker exits
    -> CANCELLED
```

not:

```text
CANCEL_REQUESTED + worker exits
    -> COMPLETED
```

unless the Operation was already terminal before the cancellation request.

### Synchronization

Synchronize:

- session lookup/update;
- Operation registration;
- cancellation lookup;
- completion;
- parent/child registration;
- event publication;
- persistence snapshots.

Never hold the manager lock while performing backend/tool work.

### Cooperative Harness cancellation

The Harness must accept a cancellation signal from the owning Operation.

Cancellation checkpoints are required:

```text
before task discovery
before Knowledge retrieval
after Knowledge retrieval
before Contract validation
before backend invocation
before each tool action
between tool actions
before staged write
before promotion
before verification
before persistence/finalization
```

For blocking backend calls or subprocesses that do not support immediate interruption:

```text
cancel requested
      ↓
worker observes cancellation at next safe checkpoint
      ↓
execution exits
      ↓
Operation becomes CANCELLED
```

Backend-specific immediate cancellation may be used where safely supported.

### P6-1 acceptance

Tests prove:

- cancelling an Operation does not terminate the Session;
- a new Operation can start afterward;
- cancellation is race-safe;
- cancellation propagates into `AgentRunner`;
- cancellation is observed at documented checkpoints;
- completion and cancellation cannot corrupt state;
- restart preserves valid state;
- terminal session cancellation remains distinct.

---

## P6-2 — Versioned JSON-RPC Contract

Use JSON-RPC 2.0.

Start with:

```text
protocolVersion = 1
```

P7 adds capabilities without creating a second transport.

### Handshake

```text
health.version
```

P6 response contains:

- package version
- protocol version
- supported capabilities

P7 capability fields are additive.

Package version must be read from the real version source.

Protocol mismatch must return a typed error.

---

## P6-3 — RPC Methods

Define and implement the P6 RPC surface:

```text
health.version
session.create
session.get / session.resume
session.list
session.history
session.close
session.sendMessage
tool.execute
request.cancel
```

Rules:

- reads may be retried where safe;
- non-idempotent requests must never be silently retried;
- `request.cancel` targets an Operation/request, not the entire Session;
- RPC responses contain sanitized structured data.

P7 later adds:

```text
plan.approve
plan.reject
backend.list
role.list
role.configureBackend
agent.list
agent.cancel
agent.inspect
```

There is no `provider.*` RPC family.

---

## P6-4 — Streaming / Event Protocol

Use server-to-client notifications over the selected notification-capable HTTP connection.

The existing RuntimeEvent sequence remains canonical.

Every event has:

```text
sessionId
seq
type
terminal/non-terminal status
timestamp
```

Assistant output uses an incremental stream event.

Tool events use the shared stream.

P7 plan/backend/agent events use the same envelope and sequence.

### Reconnect invariant

Replaying from the last known sequence must reconstruct the same observable history as a continuously connected client.

No second event counter is introduced.

---

## P6-5 — Tool Event Model

Every governed tool invocation emits:

```text
event.toolCall
event.toolResult
```

Result state must distinguish:

- started/running;
- success;
- failure;
- cancelled;
- denied;
- approval required.

Structured fields are preferred to free-form strings.

P7 Harness tool actions reuse these events.

---

## P6-6 — Typed RPC Error Contract

Errors must never expose raw stack traces.

Minimum categories:

```text
invalid_params
session_not_found
operation_not_found
policy_denied
tool_failure
protocol_mismatch
request_cancelled
timeout
internal_error
```

P7 adds:

```text
backend_not_found
backend_unavailable
backend_unauthorized
role_binding_not_configured
role_binding_rejected
plan_not_awaiting_approval
plan_revision_limit
work_order_conflict
work_order_retry_exhausted
subagent_scope_exceeds_parent
subagent_not_found
budget_exhausted
```

Every error includes a stable machine-readable category.

---

## P6-7 — Transport Implementation

Retain HTTP + JSON-RPC.

Implement the smallest notification-capable connection mechanism justified by the actual client/daemon architecture.

Do not introduce a second transport.

The chosen connection mechanism must support:

- incremental events;
- disconnect detection;
- reconnect;
- replay from sequence;
- bounded connections;
- clean shutdown.

---

## P6-8 — `StackMindTuiAdapter`

The adapter is the boundary between rendering and RPC.

Responsibilities:

- connection lifecycle;
- request encoding/validation;
- typed responses;
- notification handling;
- sequence/replay cursor;
- reconnect;
- daemon errors → UI-safe errors.

Adapter methods remain thin wrappers.

No governance logic belongs inside the adapter.

---

## P6-9 — Python-Native Renderer Gate

This is a renderer decision **inside Python**, not a language decision.

Default:

```text
Rich rendering
```

A richer Python framework may be introduced only if required by interaction complexity.

The adapter must remain renderer-independent.

No TypeScript/Bun/Zig/OpenTUI path is evaluated or introduced in this milestone.

---

## P6-10 — Core Interactive TUI

Build the minimal proving surface.

Required:

- text input;
- Enter-to-send;
- incremental assistant output;
- user/assistant/tool/system message roles;
- scrolling;
- keyboard navigation;
- cancellation shortcut;
- connection status;
- request status;
- reconnect state;
- sequence-aware history.

Rendering code must not perform blocking network calls.

---

## P6-11 — Session Management UI

Implement:

```text
:new
:resume
:close
```

Session browser displays:

- session ID;
- lifecycle state;
- last activity;
- metadata;
- resumability.

History is bounded/virtualized.

Never load unbounded history into memory.

---

## P6-12 — Reconnect and Fault UX

Handle:

- daemon unavailable;
- connection reset;
- RPC timeout;
- malformed response;
- protocol mismatch;
- policy denial;
- tool failure;
- interrupted generation;
- cancellation.

Reconnect:

```text
disconnect
   ↓
reconnecting
   ↓
handshake
   ↓
replay from last seq
   ↓
deduplicate
   ↓
live stream
```

No raw stack traces reach the user.

---

## P6-13 — P6 Security Hardening

The TUI is not a security boundary.

Requirements:

- daemon-side authorization;
- backend credentials remain daemon-side;
- RPC input validation;
- terminal output sanitization;
- ANSI/control sequence filtering;
- safe handling of model/tool output;
- no direct tool execution in the TUI;
- no Node `vm`-style sandbox assumptions;
- loopback-only daemon binding by default.

### Credential rule

Credentials must never appear in:

- RPC responses;
- RuntimeEvents;
- TUI output;
- standard logs;
- error messages.

---

## P6-14 — Concurrency and Persistence

### Manager concurrency

Mutable manager state is synchronized.

The global manager lock is never held during backend/tool execution.

### Persistence

Atomic JSON remains acceptable for the P6 baseline.

Persist:

- Sessions;
- Operations;
- event sequence;
- journals;
- recovery state.

Define schema version now.

Persistence implementation remains replaceable behind a stable abstraction.

---

# 7. P6 Gate — Definition of Done

P6 passes only when all are true:

### Runtime

- Operation and Session lifecycles are distinct.
- Operation cancellation is operation-scoped.
- Cancellation reaches `AgentRunner`.
- Session survives cancelled Operation.
- Cancellation races are safe.
- Restart/recovery is deterministic.

### Protocol

- Versioned JSON-RPC contract is implemented.
- Streaming notifications work.
- Event sequence is canonical.
- Replay/reconnect works.
- Typed errors are used.

### TUI

- TUI is only a client.
- Minimal interactive conversation works.
- Cancellation works.
- Reconnect works.
- terminal output is sanitized.

### Governance

- Tool calls cross the governed boundary.
- Contracts remain authoritative.
- D025 remains authoritative.
- No second coding engine exists.

### CI

The full existing test suite plus new P6 tests passes.

---

# 8. P6 Acceptance Scenario

```text
1. Start StackMind daemon.
2. Launch StackMind TUI.
3. Complete health/version handshake.
4. Create a session.
5. Send a prompt.
6. Receive assistant output incrementally.
7. Observe structured tool activity where applicable.
8. Cancel an in-flight Operation.
9. Confirm the Session remains usable.
10. Send another message.
11. Close the TUI.
12. Restart the daemon.
13. Relaunch the TUI.
14. Resume the previous Session.
15. Replay missed history/events.
16. Continue the conversation.
17. Trigger a daemon/connection failure.
18. Verify deterministic reconnect behavior.
19. Verify terminal escape/control sequences are sanitized.
20. Run the complete CI suite.
```

P7 cannot begin unless all 20 pass.

---

# 9. P7 — Autonomous Multi-Role Delivery

P7 is a separate product milestone.

P7 is not a UI enhancement to P6.

It introduces:

- durable Work Orders;
- parent/child Operations;
- plan lifecycle;
- Agent Role routing;
- Execution Backend abstraction;
- autonomous Harness dispatch;
- isolated Work Order workspaces;
- promotion/conflict handling;
- subagent orchestration;
- bounded autonomous control;
- project-delivery TUI surfaces.

---

# 10. PRE-P7-3 — Harness Authority Boundary

**Time box: 30–60 minutes.**

Create:

`docs/runtime-truth/P7-harness-boundary.md`

Record the exact boundary:

```text
Daemon
  ↓
Operation
  ↓
Work Order
  ↓
AgentRunner
  ↓
Execution Backend
```

Answer:

1. Which `AgentRunner` entry point is authoritative?
2. Which state remains under `.sync/*`?
3. Which state is daemon-owned?
4. How does `work_order_id` map to Operations?
5. How does Operation cancellation reach the Harness?
6. How do Harness results become RuntimeEvents?
7. How is duplicate journaling prevented?
8. How is `LLMProvider` generalized without another execution engine?

### Exit gate

PASS only when there is exactly one governed execution path.

The old CLI Harness path may be refactored or wrapped; it may not remain semantically divergent from the daemon path.

---

# 11. PRE-P7-4 — Work Order / Operation Identity Contract

**Time box: 30–60 minutes.**

Definitions:

```text
Plan
  = planning revision

Work Order
  = durable governed assignment

Operation
  = runtime execution/attempt

Agent Role
  = logical responsibility

Execution Backend
  = runtime performing the work
```

### Invariants

- one Work Order may have multiple Operations;
- retries create new Operations;
- retry preserves `work_order_id`;
- Operations have unique runtime IDs;
- child Operations have parent IDs;
- child authority is a subset of parent authority.

This model must be represented consistently in:

- schemas;
- persistence;
- events;
- Harness;
- RPC;
- tests.

---

# 12. PRE-P7-5 — Workspace and Promotion Mechanism

**Hard gate.**

This phase is not complete with documentation alone.

## Required mechanism

Use Git worktrees.

```text
canonical repository
      │
      ├── WO-001 worktree
      ├── WO-002 worktree
      └── WO-003 worktree
```

Every Work Order records:

```text
work_order_id
operation_id
base_commit
candidate_ref
workspace_path
```

## Execution

```text
create worktree
    ↓
run AgentRunner
    ↓
Contract + D025 + verification
    ↓
candidate commit
    ↓
fresh canonical-base conflict check
    ↓
serialized promotion
```

## Conflict policy

### Same file

Conflict is explicit.

Do not silently choose either side.

### Related files

Fresh-base verification must determine whether the candidate assumptions are still valid.

### Stale parent

Child completion against an obsolete parent base requires revalidation.

### Unresolved conflict

State:

```text
CONFLICTED
```

Then:

- do not promote;
- preserve candidate workspace for inspection;
- allow Architecture re-plan or controlled retry;
- create a new Operation for a retry.

### Cleanup

Successful promotion cleans its Work Order worktree.

Failed/conflicted Work Orders retain their workspace until the retention policy permits cleanup or an explicit discard occurs.

### Exit gate

PASS only when an automated test demonstrates:

1. two Work Orders execute concurrently;
2. both have separate worktrees;
3. both produce candidates;
4. one can promote;
5. the second detects a stale/conflicting base;
6. no silent data loss occurs.

---

# 13. PRE-P7-6 — Autonomous Execution Control

**Time box: 60–90 minutes.**

Define and enforce:

- plan revision limit;
- Work Order retry limit;
- Operation timeout;
- token budget;
- cost budget where measurable;
- tool-call budget;
- command budget;
- subagent count;
- subagent depth.

Each limit must have:

```text
configured value
current usage
remaining budget
terminal behavior when exceeded
```

Budgets belong to the governed runtime.

## Retry policy

Retryable:

- transient backend timeout;
- transient daemon/transport failure;
- transient workspace lock conflict.

Non-retryable:

- Contract denial;
- D025 denial;
- invalid Work Order;
- unauthorized backend;
- deterministic schema violation;
- scope escalation.

Retry creates:

```text
same work_order_id
new operation_id
new execution history
```

No silent retries for non-idempotent user RPC.

## Parent failure

A parent becomes `FAILED` when required children fail or cannot be completed within policy.

Successful siblings remain inspectable and may remain promoted.

Architecture may re-plan affected work.

---

# 14. PRE-P7-7 — Agent Role / Execution Backend Contract

**Time box: 60 minutes.**

Canonical routing:

```text
Agent Role
    ↓
Execution Backend
    ↓
backend configuration
```

### Role set

```text
Architecture / StackMind Orchestrator
Backend
Frontend
Q/A
GitOps
```

These are configurable logical roles.

### Backend types

```text
Agent Backend
  ├── Codex
  ├── AGY
  └── Claude

Model Backend
  └── Ollama / local models
```

### Backend configuration

At minimum:

- backend ID;
- backend type;
- endpoint/base URL if applicable;
- credential reference;
- model;
- capabilities;
- timeout;
- budget policy.

Credentials themselves never cross the TUI/RPC boundary.

### Backend rules

- no silent fallback;
- backend identity is captured in the Operation;
- backend cannot alter Contract authority;
- backend failures become structured failures;
- backend adapters cannot bypass Harness/governance;
- running Work Orders cannot be silently rebound.

---

# 15. PRE-P7-8 — Plan Lifecycle Contract

**Time box: 60 minutes.**

Required states:

```text
DRAFT
AWAITING_APPROVAL
REJECTED
APPROVED
SUPERSEDED
PLAN_BLOCKED
```

Rules:

- each revision has a unique plan ID/version;
- each revision links to its predecessor;
- old revisions remain inspectable;
- rejection creates no Work Orders;
- rejection feedback is persisted;
- Architecture consumes the feedback;
- a revised plan gets a new version;
- only `AWAITING_APPROVAL` plans may be approved;
- Work Orders are created only after approval;
- approving an invalid state returns a typed error.

---

# 16. PRE-P7-9 — Security and Trust Boundary

**Time box: 60–90 minutes.**

## Daemon

P7 default:

```text
bind = 127.0.0.1
```

Remote binding is out of scope.

Authentication uses a daemon-local token.

Token requirements:

- generated with secure randomness;
- stored in a daemon-private file;
- POSIX mode equivalent to `0600`;
- Windows equivalent ACL restriction;
- never included in logs/events/responses;
- not passed through user-visible TUI configuration.

## Request limits

Define a maximum request size.

Initial maximum:

```text
1 MiB
```

Oversized requests fail before application processing.

## Transport validation

For any persistent stream/upgrade:

- validate Host;
- validate Origin where applicable;
- reject unexpected upgrade forms;
- bound active connections;
- define disconnect cleanup.

## Authorization

All of these remain daemon-controlled:

- tools;
- backend selection;
- Work Order creation;
- subagent spawn;
- cancellation;
- plan approval;
- promotion.

## Terminal safety

Sanitize:

- model output;
- backend output;
- tool output;
- subagent output;
- error text.

Security tests must cover:

- credential leakage;
- unauthorized requests;
- invalid inputs;
- Contract scope escalation;
- terminal escape injection;
- approval bypass;
- active Work Order backend rebinding.

---

# 17. PRE-P7-10 — Persistence and Recovery Contract

**Time box: 60–90 minutes.**

Atomic JSON remains the initial implementation.

Introduce:

```text
schemaVersion
```

from the beginning.

## Persist

- sessions;
- Operations;
- Work Orders;
- parent/child relationships;
- event sequence;
- audit records;
- plan versions;
- role/backend bindings;
- workspace metadata;
- recovery state.

## Recovery rule

After restart:

```text
observable state before restart
        =
observable reconstructed state after restart
```

An in-flight Operation must not be falsely marked completed.

Initial recovery policy:

```text
RUNNING at crash
    ↓
RECOVERY_REQUIRED / WAITING
```

Automatic resume is allowed only when the operation is explicitly idempotent and its recovery contract permits it.

## Compaction

Do not allow unbounded `daemon-state.json` growth.

Target behavior:

```text
active state
+
recent event journal
+
archived event segments
```

Initial compaction trigger may be implementation-configurable, with a default based on file size/event count.

Event sequence continuity must survive compaction.

---

# 18. PRE-P7-11 — Headless P7 Integration Proof

This is the first real P7 milestone.

**No visual UI is required.**

Automated scenario:

```text
1. Create project/session.
2. Architecture execution produces PLAN.md.
3. PLAN.md enters AWAITING_APPROVAL.
4. Approve via daemon RPC.
5. One real Work Order is created.
6. Work Order maps to an Agent Role.
7. Agent Role maps to an Execution Backend.
8. Work Order runs through AgentRunner/Harness.
9. Tool actions cross the governed boundary.
10. Verification executes.
11. One child Operation is created.
12. Child Operation is cancelled independently.
13. Parent Operation remains alive.
14. Reconnect occurs.
15. Operation tree is reconstructed.
16. Work Order reaches the correct terminal state.
```

The proof must use the real governed Harness boundary.

It must not create a separate “P7 daemon coding engine” purely for the test.

### Exit gate

The proof is reproducible and automated.

Until this passes, P7 UI work does not begin.

---

# 19. P7-0 — Governed Agent Roles and Operation Tree

Extend the P6 Operation model.

Required model:

```text
Session
  └── Operation
        ├── work_order_id
        ├── operation_id
        ├── parent_operation_id
        ├── agent role
        ├── execution backend
        ├── Contract scope
        └── children
```

A child Operation **may execute a child Work Order**.

A child Operation is not itself synonymous with a Work Order.

### Manager API

```python
begin_operation(
    session_id,
    operation_name,
    metadata,
    parent_operation_id=None,
    work_order_id=None,
    contract_scope=None,
)
```

```python
cancel_operation(operation_id, cascade=True)
```

```python
list_children(operation_id)
```

### Cascade rules

- parent cancellation cascades to non-terminal children;
- child cancellation does not affect parent/siblings;
- parent waits for child terminality before terminal completion;
- no parent completion if required child failed;
- no child authority wider than parent;
- retry preserves Work Order identity;
- new attempt gets new Operation identity.

### Exit criteria

Automated tests prove:

- child creation;
- parent cascade cancellation;
- independent child cancellation;
- Contract narrowing;
- parent completion blocking;
- restart recovery;
- retry identity behavior.

---

# 20. P7-1 — Harness Integration, Work Order Execution, and Plan Approval

This is the central P7 runtime milestone.

Required path:

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
AgentRunner
      ↓
Execution Backend
      ↓
Tools / workspace
      ↓
D025 + validation
      ↓
verification
      ↓
candidate
      ↓
promotion
```

### Plan RPC

```text
plan.approve
plan.reject
```

`plan.approve`:

- accepts only `AWAITING_APPROVAL`;
- creates persistent Work Orders;
- returns the created Work Order IDs.

`plan.reject`:

- accepts only `AWAITING_APPROVAL`;
- stores feedback;
- creates no Work Orders;
- emits a plan rejection event;
- causes the Architecture Agent to produce a new revision within the revision budget.

### Harness semantics

Preserve the existing governed loop:

```text
Receive Work Order
→ validate Contract
→ assemble Knowledge API context
→ invoke execution backend
→ validate backend result
→ validate staged diff
→ enforce D025
→ verify
→ persist
→ emit audit/events
→ update Work Order/Operation
```

Completion requires verification, not merely a successful backend response.

### Exit criteria

- approved plan creates Work Orders;
- rejected plan creates none;
- rejection → revision → re-submission works;
- Work Orders use the existing Harness;
- Contract is enforced;
- Knowledge API is gated;
- D025 is enforced;
- verification gates promotion;
- state is persisted and observable;
- representative Work Order completes without repeated user direction.

---

# 21. P7-2 — Execution Backend Abstraction

Complete the actual branch preflight before implementation.

The preflight result is part of this phase's record.

### Required interface

```text
start operation
send/continue work
stream events
request/resolve approval
cancel
inspect
report completion/failure
```

### Backend contract

Every backend adapter maps into the same governed Harness boundary.

At least two real backends must be exercised by acceptance.

The same Agent Role must be able to change backend without changing TUI code.

### Rebinding

`role.configureBackend` is rejected if the role has a non-terminal Work Order.

Rebinding is allowed between assignments.

No live Work Order changes backend identity.

### Exit criteria

- two real adapters pass a common contract test suite;
- at least one Agent Backend and one Model Backend are exercised where practical;
- no credential leaks;
- backend failure semantics are deterministic;
- rebinding guard works.

---

# 22. P7-3 — Work Order Dispatch and Subagent Orchestration

Architecture generates the Work Order graph.

Specialized roles execute it.

```text
Architecture
      ↓
Work Order graph
      ↓
Backend / Frontend / Q/A / GitOps
      ↓
child Operations
      ↓
Execution Backends
```

### TUI-facing operations

```text
agent.list
agent.cancel
agent.inspect
```

These expose operation/work status.

Subagent creation is a runtime decision, not a free-form TUI command.

### Required behaviors

- role and Work Order assignment;
- Contract inheritance;
- child operation creation;
- child cancellation;
- parent cancellation;
- result aggregation;
- reconnect reconstruction;
- workspace association.

---

# 23. P7-4 — Autonomous Delivery TUI

Only after headless P7 proof passes.

The TUI becomes project-delivery oriented.

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

### Plan surface

Display:

- plan ID/version;
- status;
- work decomposition;
- dependencies;
- risks;
- validation;
- approval controls;
- revision history.

### Role/backend surface

Read-only view:

```text
Backend Agent
  Role: Backend
  Backend: Codex
  Work Order: WO-002
  State: RUNNING
```

### Operation tree

```text
● Architecture
  ├─ ● Backend / Codex
  ├─ ● Frontend / AGY
  ├─ ○ Q/A / Ollama
  └─ ○ GitOps / Ollama
```

### Completion

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

Normal worker execution does not require repeated user steering.

---

# 24. P7-5 — Agentic Security Hardening

Extend P6 security guarantees to every new surface.

Verify:

- child scope cannot exceed parent;
- credentials do not cross boundaries;
- every tool call remains governed;
- every destructive operation passes D025;
- terminal output is sanitized;
- backend rebind is blocked while work is active;
- budget exhaustion cannot be bypassed;
- plan approval cannot be bypassed;
- subagents cannot create authority outside their parent Contract.

---

# 25. P7-6 — Testing and Fault Injection

Add:

## Operation tree tests

- spawn;
- child cancellation;
- parent cascade;
- race between cancellation and natural completion;
- restart/recovery;
- retry identity.

## Plan tests

- approval;
- rejection with feedback;
- rejection without feedback;
- revision;
- revision limit;
- invalid approval state;
- historical inspectability.

## Harness tests

- Work Order execution;
- Knowledge context;
- Contract gate;
- D025 gate;
- verification;
- workspace candidate;
- promotion;
- conflict detection.

## Backend tests

- common adapter contract;
- unavailable backend;
- timeout;
- authorization failure;
- no credential leakage;
- rebinding rejection.

## Fault injection

- backend crash;
- subprocess timeout;
- child crash;
- parent cancellation racing child completion;
- reconnect during execution;
- concurrent promotion conflict;
- role rebind during completion;
- persistence corruption/recovery path.

---

# 26. P7-7 — Documentation

Documentation is updated only after behavior exists.

Required updates:

- `docs/runtime-truth/P6-P7-baseline.md`
- `STACKMIND_CLI.md`
- `README.md`
- protocol/capability documentation
- plan approval/rejection flow
- Work Order lifecycle
- role/backend configuration
- subagent tree
- cancellation semantics
- workspace/promotion model
- recovery model

No documentation may claim a feature is shipped before its acceptance test passes.

---

# 27. P7-8 — Final Acceptance

P7 is accepted only after the entire extended scenario passes.

```text
21. Configure Architecture, Backend, Frontend, Q/A, and GitOps roles.
22. Configure at least two distinct Execution Backends.
23. Submit one project objective.
24. Architecture creates PLAN.md.
25. PLAN.md reaches AWAITING_APPROVAL.
26. Reject the plan with feedback.
27. Verify no Work Orders were created.
28. Verify a revised plan is generated with a new version.
29. Approve the revised plan.
30. Verify Work Orders are persisted and dispatched.
31. Verify Backend and Frontend execute concurrently in separate worktrees.
32. Trigger a D025-gated action.
33. Verify execution pauses for explicit approval.
34. Approve it and verify execution resumes.
35. Attempt to rebind a running role's backend.
36. Verify the rebind is rejected.
37. Cancel one child/role operation.
38. Verify siblings and parent remain alive.
39. Force a required Work Order failure.
40. Verify project completion is blocked.
41. Re-plan or retry the failed Work Order with a new Operation ID.
42. Introduce a promotion conflict.
43. Verify conflict is detected without silent data loss.
44. Resolve or re-plan the conflict.
45. Reconnect during execution.
46. Reconstruct the complete operation tree.
47. Reconstruct PLAN.md status.
48. Reconstruct role/backend bindings.
49. Complete all required Work Orders.
50. Verify final verification passes.
51. Verify README.md is updated.
52. Verify project delivery summary is emitted.
53. Verify Git status is correct.
54. Verify no credential appeared in RPC responses, events, logs, or TUI.
55. Verify terminal output remains sanitized.
56. Run the complete CI and fault-injection suite.
```

---

# 28. Final Definition of Done

## Architecture

- TUI is only a client.
- StackMind remains the governance authority.
- Agent Roles and Execution Backends are distinct.
- Provider is configuration, not a runtime role.
- Existing Harness remains the single execution authority.

## Runtime

- Session and Operation lifecycles are separate.
- Operation cancellation is cooperative and race-safe.
- Parent/child Operations are durable.
- Retry identity is correct.
- Reconnect is deterministic.

## Work Orders

- Work Orders are durable.
- Work Orders map to Agent Roles.
- Work Orders run through the Harness.
- Completion requires verification.
- Failed required children block false parent success.

## Workspace

- Each concurrent Work Order has an isolated Git worktree.
- Candidate state is based on an explicit commit.
- Promotion is serialized.
- Conflicts are detected.
- No silent overwrite occurs.

## Backends

- Agent Role → Execution Backend routing is stable.
- At least two real backends work.
- No live Work Order backend rebinding.
- Backend credentials remain daemon-side.
- Backend failures are deterministic.

## Autonomous controls

- Plan revisions are bounded.
- Retries are bounded.
- Operation timeouts are bounded.
- Tool/command budgets are enforced.
- Subagent depth/count is bounded.
- Parent failure semantics are deterministic.

## Security

- Loopback daemon by default.
- Authentication is enforced.
- Secrets never cross the TUI/RPC boundary.
- Terminal output is sanitized.
- Contract and D025 authority is never bypassed.
- Approval cannot be bypassed.

## Persistence

- Durable schema version exists.
- Recovery is deterministic.
- In-flight Operations are not falsely completed.
- Event/state growth is bounded.
- Compaction preserves sequence semantics.

## Product

Normal execution follows:

```text
Objective
   ↓
PLAN.md
   ↓
User approval
   ↓
Autonomous Work Orders
   ↓
Verification
   ↓
Promotion
   ↓
Final project + README.md
```

---

# 29. Implementation Order — Final

This is the authoritative order.

```text
PRE-P7-0
Documentation truth
        ↓
PRE-P7-1
Python-native TUI decision
        ↓
P6-0
Targeted runtime preflight
        ↓
P6-1
Operation lifecycle + cooperative Harness cancellation
        ↓
P6-2
Versioned JSON-RPC contract
        ↓
P6-3
RPC methods
        ↓
P6-4
Streaming/event protocol
        ↓
P6-5
Tool events
        ↓
P6-6
Typed errors
        ↓
P6-7
Transport implementation
        ↓
P6-8
Typed TUI adapter
        ↓
P6-9
Python renderer gate
        ↓
P6-10
Core TUI
        ↓
P6-11
Session UI
        ↓
P6-12
Reconnect/fault UX
        ↓
P6-13
Security
        ↓
P6-14
Concurrency/persistence
        ↓
P6 ACCEPTANCE
        ↓
PRE-P7-3
Harness boundary
        ↓
PRE-P7-4
Identity contract
        ↓
PRE-P7-5
Workspace/promotion mechanism + conflict test
        ↓
PRE-P7-6
Autonomous controls
        ↓
PRE-P7-7
Role/backend contract
        ↓
PRE-P7-8
Plan lifecycle
        ↓
PRE-P7-9
Security specification
        ↓
PRE-P7-10
Persistence/recovery contract
        ↓
PRE-P7-11
HEADLESS P7 INTEGRATION PROOF
        ↓
P7-0
Operation tree
        ↓
P7-1
Harness + Work Orders + plan approval
        ↓
P7-2
Execution backend abstraction
        ↓
P7-3
Dispatch + subagents
        ↓
P7-4
Autonomous delivery TUI
        ↓
P7-5
Agentic security hardening
        ↓
P7-6
Testing/fault injection
        ↓
P7-7
Documentation
        ↓
P7-8
FINAL ACCEPTANCE
```

---

# 30. PR Breakdown

## P6

| PR | Scope | Gate |
|---|---|---|
| P6-0 | targeted preflight note | facts confirmed |
| P6-1 | Operation lifecycle + cooperative cancellation | cancellation suite passes |
| P6-2 | versioned protocol | contract tests pass |
| P6-3 | RPC methods | protocol tests pass |
| P6-4 | streaming/events | incremental output + sequence tests pass |
| P6-5 | tool events | tool event tests pass |
| P6-6 | typed errors | error contract tests pass |
| P6-7 | transport | streaming/reconnect transport tests pass |
| P6-8 | TUI adapter | adapter contract tests pass |
| P6-9 | Python renderer decision | chosen renderer documented |
| P6-10 | core TUI | interactive smoke test passes |
| P6-11 | session UI | session workflow passes |
| P6-12 | reconnect UX | fault/replay tests pass |
| P6-13 | security | security suite passes |
| P6-14 | concurrency/persistence | recovery/concurrency tests pass |

## P7

| PR | Scope | Gate |
|---|---|---|
| P7-0 | Operation tree | cascade/scope/recovery tests pass |
| P7-1 | Work Order + Harness + plan approval | headless governed Work Order pass |
| P7-2 | backend abstraction | two adapters + security tests pass |
| P7-3 | dispatch/subagents | tree/reconnect/cancel tests pass |
| P7-4 | autonomous delivery TUI | project-flow smoke test passes |
| P7-5 | agentic security | extended security suite passes |
| P7-6 | testing/fault injection | complete CI coverage passes |
| P7-7 | documentation | docs match shipped behavior |
| P7-8 | final acceptance | full E2E scenario passes |

Never combine backend, subagent, and UI changes into one oversized PR.

---

# 31. Explicit Non-Goals

This plan does not include:

- replacing the entire StackMind runtime;
- replacing the existing Harness;
- introducing a second coding engine;
- introducing a general plugin platform;
- introducing gRPC;
- introducing distributed/multi-user execution;
- enabling unrestricted remote daemon access;
- embedding OpenCode as a runtime;
- introducing a database solely for P7;
- allowing TUI-side provider/backend calls;
- allowing TUI-side tool execution;
- allowing TUI-side subagent spawning;
- automatic live backend switching for a running Work Order;
- unbounded plan revision or retries;
- treating the advisory write lock as a merge strategy;
- speculative TypeScript/Bun/Zig frontend work.

---

# 32. Final P7 Entry Gate

`PLAN_P7` is implementation-ready only when this statement is true:

> **The StackMind P6 runtime is factually documented; the TUI language and packaging model are fixed; operation cancellation is correct and propagates cooperatively into the Harness; the existing Harness is the single governed execution path; Work Order and Operation identities are stable; concurrent Work Orders use isolated worktrees with an explicit promotion/conflict mechanism; autonomous budgets and failure behavior are bounded; Agent Role → Execution Backend routing is specified; plan lifecycle is durable; daemon security is explicit; persistence and recovery are bounded; and the headless P7 integration proof has passed.**

At that point P7 is no longer an architectural experiment.

It is an implementation phase over a proven runtime.

---

# 33. Final Engineering Principle

```text
TRUTH
  before
DECISIONS
  before
RUNTIME CORRECTNESS
  before
CLIENT UI
  before
AUTONOMOUS DELIVERY
  before
DELIVERY DASHBOARD
```

And the most important runtime rule remains:

```text
TUI
 ↓
StackMind Governance
 ↓
Agent Role
 ↓
Execution Backend
 ↓
AgentRunner / Harness
 ↓
Governed Tools
 ↓
Verification
 ↓
Promotion
```

**There is exactly one governed execution path.**
