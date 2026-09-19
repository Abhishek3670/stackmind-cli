# PLAN_P7_PREREQUISITES.md

# StackMind P7 — Prerequisite & Readiness Plan

**Repository:** `Abhishek3670/stackmind-cli`  
**Target branch:** `feat/p6-open-source-tui`  
**Purpose:** Establish the technical, architectural, security, and operational prerequisites that must be proven before implementation of `PLAN_P7.md`.  
**Status:** Prerequisite plan  
**Scope:** P7 readiness only — no autonomous-delivery feature implementation

---

## 1. Objective

P7 is intended to extend StackMind from a governed CLI/TUI foundation into an autonomous multi-role engineering delivery runtime.

Before P7 implementation begins, the repository must prove that its existing runtime can safely support:

- governed Work Orders;
- runtime Operations and parent/child operation trees;
- autonomous execution through the existing Harness;
- Agent Role → Execution Backend bindings;
- concurrent Work Order execution with isolated workspaces;
- cancellation and retry semantics;
- plan approval/rejection and revision;
- durable persistence and reconnect;
- backend credential isolation;
- bounded autonomous execution.

This document is a **readiness gate**, not the P7 implementation plan.

The prerequisite work must not quietly become P7 implementation. Any capability that requires substantial new runtime behavior should be explicitly recorded as a P7 dependency and scheduled in `PLAN_P7.md`.

---

# 2. Current-State Truth

The prerequisite phase starts by reconciling the intended product documentation with the actual branch.

The current daemon implementation already provides:

- `SessionManager`;
- operation IDs and cancellation `Event`s;
- JSON-RPC session lifecycle methods;
- replayable sequenced runtime events;
- atomic JSON persistence;
- the governed `AgentRunner` Harness;
- Knowledge API integration;
- pre/post contract validation;
- staged workspace validation;
- D025 evaluation;
- write locking and verification.

However, the current daemon/TUI surface does not yet provide the complete P6/P7 execution path described by the target documentation.

The current `SessionManager.cancel_session()` also terminates the session after signalling the active operation. The current protocol does not provide the planned prompt/turn API, and the current TUI adapter replays `event.list` rather than consuming a true push stream.

Therefore:

> **P7 must not assume that the documented v3.2.0 GA TUI/runtime behavior already exists.**

P7 readiness depends on a reconciled P6 baseline.

---

# 3. P7 Entry Criteria

P7 implementation is blocked until all of the following are true:

1. The repository's current-state documentation matches the actual shipped code.
2. P6 operation-scoped cancellation is implemented and tested.
3. The daemon/client protocol and reconnect model are stable enough to carry P7 events.
4. The existing Harness is confirmed as the single authoritative execution path.
5. Agent Role, Execution Backend, and Provider terminology is frozen.
6. Workspace isolation and promotion semantics are explicitly defined.
7. Autonomous execution budgets and retry/revision limits are defined.
8. Daemon authentication and credential handling are specified.
9. Persistence growth, compaction, schema versioning, and recovery are defined.
10. The TUI/runtime implementation language and packaging model are explicitly decided.
11. A headless P7 integration proof is designed and accepted as the first P7 milestone.
12. No unresolved blocker remains in the P7 readiness checklist.

---

# 4. PRE-P7-0 — Repository & Documentation Truth Reconciliation

## Goal

Create one authoritative statement of what exists on `feat/p6-open-source-tui` and what remains planned.

## Tasks

### 4.1 Reconcile `STACKMIND_CLI.md`

Classify every major claimed capability as:

- **Implemented**
- **Partially implemented**
- **Planned**
- **Deprecated**
- **Incorrect**

At minimum reconcile:

- `stackmind tui`;
- `:new`;
- `:status`;
- `:approve`;
- `:reject`;
- `:cancel`;
- `:diff`;
- `:matrix`;
- operation-scoped cancellation;
- streaming;
- HITL approval;
- prompt/turn execution;
- daemon transport;
- session history;
- capability negotiation.

### 4.2 Reconcile runtime documentation

Verify documentation against:

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

### 4.3 Establish documentation authority

Define:

- current release behavior;
- target/P6 behavior;
- P7 target behavior.

Do not describe planned methods as shipped.

## Deliverable

`docs/runtime-truth/P6-P7-baseline.md`

The document must contain a table:

| Capability | Actual branch state | Documented state | Required action |
|---|---|---|---|

## Exit Criteria

- No material contradiction remains between the baseline documentation and implementation.
- Every planned P7 dependency is explicitly marked as future behavior.
- Package/version statements are sourced from the actual version source.

---

# 5. PRE-P7-1 — TUI / Runtime Language & Packaging Decision

## Goal

Eliminate the unresolved frontend toolchain split before P7 depends on it.

## Problem

The existing project is Python-first:

- `stackmind` is a Python Click CLI;
- runtime and TUI code live in Python;
- Rich is already a dependency.

The target plan currently considers TypeScript/OpenTUI, Bun, Zig/native dependencies, and Ink/Blessed.

That is not merely a renderer choice. It changes packaging, CI, installation, release, and process boundaries.

## Required Decision

Choose one:

### Option A — Python-native TUI

Keep:

```text
stackmind
 ├── CLI
 ├── daemon client
 └── TUI
```

Advantages:

- one language;
- one package;
- one CI toolchain;
- simpler installation;
- easier runtime/client integration.

### Option B — Separate TypeScript TUI

Explicitly define:

```text
Python StackMind Runtime
        ↕
JSON-RPC
        ↕
TypeScript TUI process
```

The prerequisite must specify:

- package manager;
- Bun/Node requirement;
- native build requirements;
- installation UX;
- Windows/Linux/macOS support;
- CI;
- release artifacts;
- error handling if the frontend executable is missing;
- version compatibility between Python daemon and TUI.

## Exit Criteria

One architecture is selected and documented.

No P7 implementation may proceed with both choices simultaneously.

---

# 6. PRE-P7-2 — P6 Runtime Operation Readiness

## Goal

Prove the runtime semantics that all P7 operations will depend on.

## Required Model

```text
Session lifecycle
    ≠
Operation lifecycle
```

A single session must be able to contain multiple operations without requiring session termination when one operation is cancelled.

## Required Tests

Prove:

- operation cancellation does not terminate the session;
- a new operation can start after cancellation;
- cancellation is race-safe;
- cancellation and completion cannot produce inconsistent terminal states;
- terminal session cancellation remains available as a distinct action;
- daemon restart does not corrupt recovered session state.

## Concurrency Requirement

Synchronize:

- session lookup/update;
- operation registration;
- cancellation lookup;
- completion;
- event publication;
- persistence snapshots.

Never hold the global manager lock while executing backend/tool work.

## Exit Criteria

P6 cancellation tests are green and the operation lifecycle is stable enough for use by parent/child Work Order execution.

---

# 7. PRE-P7-3 — Harness Authority & Integration Boundary

## Goal

Prove that P7 will have exactly one governed execution path.

The existing `AgentRunner`/Harness must remain the authoritative execution mechanism.

## Required Integration Study

Record the exact boundary between:

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

1. Which `AgentRunner` methods are invoked by the daemon?
2. Which state remains in `.sync/*` and which state lives in daemon persistence?
3. How does `HarnessTask.work_order_id` map to runtime Operations?
4. How does operation cancellation reach an executing Harness run?
5. How do Harness verification/persistence results become runtime events?
6. How are duplicate journals prevented?
7. How is the current `LLMProvider` generalized without creating a second execution model?

## Single-Source Rule

Do not allow:

```text
CLI → AgentRunner
```

and:

```text
Daemon → New coding engine
```

to become two independently governed execution paths.

Any refactor must preserve the existing governance semantics:

- Contract;
- Knowledge scope;
- staging;
- D025;
- write lock;
- verification;
- evidence;
- persistence.

## Deliverable

`docs/runtime-truth/P7-harness-boundary.md`

## Exit Criteria

- One authoritative Harness entry path is identified.
- Operation cancellation can be propagated into it.
- Work Order identity and Operation identity are unambiguous.
- No planned P7 component requires a duplicate governance engine.

---

# 8. PRE-P7-4 — Work Order / Operation Identity Contract

## Goal

Freeze the durable identity model before implementing operation trees.

## Required Definitions

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
    = runtime used to perform the work
```

## Required Invariants

- One Work Order may have multiple Operations over its lifetime.
- Retries create a new Operation.
- Retried Work Orders retain `work_order_id`.
- Operations have globally unique runtime identity within the daemon/project.
- Child Operations may exist under parent Operations.
- A child Operation never gets broader authority than its parent.

## Required State Model

```text
REQUESTED
AUTHORIZED
RUNNING
COMPLETED
FAILED
CANCEL_REQUESTED
CANCELLED
```

## Exit Criteria

The identity and state model is represented consistently in:

- schemas;
- daemon persistence;
- events;
- Harness integration;
- RPC;
- tests.

---

# 9. PRE-P7-5 — Workspace Isolation, Merge & Promotion Contract

## Goal

Define how concurrent Work Orders interact with the project filesystem.

This is a mandatory prerequisite because P7 intends multiple roles to work autonomously and potentially concurrently.

## Required Model

```text
Canonical Project
      │
      ├── Work Order workspace A
      ├── Work Order workspace B
      ├── Work Order workspace C
      └── ...
```

Each Work Order must have a clear isolation boundary.

## Define

### Workspace ownership

- Which directory/copy belongs to an Operation?
- Can multiple Operations write the same workspace?
- Can a child share a parent's workspace?
- How are temporary workspaces cleaned up?

### Promotion

Define:

```text
execute
  ↓
validate
  ↓
produce candidate
  ↓
conflict check
  ↓
promote
```

Promotion must be serialized against the canonical project.

### Conflict handling

Explicitly define what happens when:

- two Work Orders modify the same file;
- two Work Orders modify related files;
- one promotion changes assumptions of another Work Order;
- a child finishes against a stale parent workspace.

### Failure handling

Define whether failed Work Orders:

- remain isolated;
- may be retried;
- are discarded;
- trigger re-plan;
- require Q/A review.

## Minimum Rule

The existing advisory write lock must not be treated as the complete concurrency solution.

A lock serializes critical sections; it does not define semantic merge/conflict behavior.

## Exit Criteria

A written workspace/promotion specification exists and includes at least:

- isolation;
- promotion serialization;
- conflict detection;
- conflict resolution;
- cleanup;
- retry semantics.

---

# 10. PRE-P7-6 — Autonomous Execution Control Contract

## Goal

Define bounded autonomous behavior before allowing autonomous delivery.

## Required Limits

At minimum specify:

```text
Plan revision limit
Work Order retry limit
Operation timeout
Token budget
Cost budget
Tool-call budget
Command budget
Subagent count/depth
```

## Plan Rejection Loop

Define a finite policy for:

```text
PLAN
 ↓
REJECT
 ↓
REVISE
 ↓
RESUBMIT
```

The loop must have a terminal failure condition.

## Retry Policy

Define:

- retryable failures;
- non-retryable failures;
- maximum retries;
- backoff;
- new Operation identity;
- preserved Work Order identity;
- state after exhausted retries.

## Parent Failure Policy

Define what happens when:

```text
Backend  ✓
Frontend ✓
QA       ✗
```

Specifically determine:

- whether sibling results remain promotion candidates;
- whether successful siblings may already be canonical;
- when rollback is required;
- whether the parent becomes `FAILED`;
- whether the Architecture Agent re-plans.

## Exit Criteria

The behavior is deterministic enough to test without relying on human judgment.

---

# 11. PRE-P7-7 — Role / Execution Backend Contract

## Goal

Freeze the routing model before implementing adapters.

## Required Model

```text
Agent Role
    ↓
Execution Backend
    ↓
backend-specific configuration
```

Provider information remains configuration associated with the backend.

No `provider.*` RPC namespace is required.

## Required Role Set

```text
Architecture / StackMind Orchestrator
Backend
Frontend
Q/A
GitOps
```

The implementation must keep these as configurable logical roles rather than hard-coded vendor identities.

## Required Backend Classification

### Agent-native backend

Examples:

- Codex;
- AGY;
- Claude.

### Model backend

Examples:

- Ollama;
- local model runtime.

The abstraction must not assume that every backend exposes identical capabilities.

## Required Configuration Fields

At minimum define:

- backend ID;
- backend type;
- endpoint/base URL where applicable;
- credential reference;
- model;
- supported capabilities;
- timeout;
- budget policy.

Credentials themselves never cross the TUI/RPC boundary.

## Exit Criteria

A stable internal interface is documented before adapter implementation begins.

---

# 12. PRE-P7-8 — Plan Lifecycle Contract

## Goal

Define the lifecycle of `PLAN.md` before Work Orders can be generated from it.

## Required States

At minimum:

```text
DRAFT
AWAITING_APPROVAL
REJECTED
APPROVED
SUPERSEDED
```

## Required Rules

- Every revision has a unique plan identity/version.
- Reject never creates Work Orders.
- Reject can carry optional feedback.
- The Architecture Agent consumes the feedback.
- A revision is linked to its predecessor.
- Old plans remain inspectable.
- Approving a non-approvable state produces a structured error.
- Work Order creation occurs only after approval.

## Exit Criteria

The plan lifecycle can be represented durably and reconstructed after reconnect/restart.

---

# 13. PRE-P7-9 — Security & Trust Boundary Specification

## Goal

Turn security principles into implementation requirements.

## Daemon

Define:

- default loopback-only bind;
- authentication mechanism;
- token/credential storage;
- token file permissions;
- request validation;
- maximum request size;
- rate or resource limits where appropriate.

## Transport

If a persistent streaming transport is used, define:

- Host validation;
- Origin validation where applicable;
- upgrade handling;
- connection limits;
- disconnect behavior.

## Credentials

Define:

```text
TUI
  ✗ receives backend credential

RPC response
  ✗ contains backend credential

Runtime event
  ✗ contains backend credential

Logs
  ✗ contain backend credential
```

## Terminal Safety

All model/backend/tool/subagent output must be sanitized before terminal rendering.

## Exit Criteria

A security specification exists and has automated tests planned for:

- credential leakage;
- unauthorized requests;
- invalid inputs;
- scope escalation;
- terminal escape injection;
- approval bypass.

---

# 14. PRE-P7-10 — Persistence & Recovery Contract

## Goal

Ensure P7 state remains durable and recoverable without making persistence a hidden scalability failure.

## Existing Baseline

Atomic JSON persistence is acceptable as an initial implementation.

Before P7, define:

- schema version;
- migration strategy;
- compaction;
- retention;
- event archival;
- replay semantics;
- recovery of in-flight Operations;
- recovery of parent/child relationships;
- recovery of Work Order state;
- recovery of plan revision state;
- recovery of role/backend bindings.

## Required Invariant

After restart, the observable project state must be reconstructable from durable state/events.

## Compaction Requirement

Define how `daemon-state.json` avoids unbounded growth.

Possible future implementation:

```text
active state
+
recent event journal
+
archived event segments
```

The exact mechanism may remain implementation-specific, but the behavior must be specified.

## Exit Criteria

Recovery tests cover:

- active operation;
- cancelled operation;
- failed child;
- completed Work Order;
- plan awaiting approval;
- role/backend binding;
- operation tree.

---

# 15. PRE-P7-11 — Headless P7 Integration Proof

Before building the autonomous delivery UI, prove P7 without a TUI.

## Required Scenario

```text
1. Create project/session
2. Architecture execution produces PLAN.md
3. PLAN.md enters AWAITING_APPROVAL
4. Approve via daemon RPC
5. One real Work Order is created
6. Work Order maps to an Agent Role
7. Agent Role maps to one Execution Backend
8. Work Order runs through the existing AgentRunner/Harness
9. Tool actions cross the governed boundary
10. Verification executes
11. One child Operation is created
12. Child Operation can be cancelled independently
13. Parent Operation remains alive
14. Reconnect reconstructs the operation tree
15. Work Order reaches the correct terminal state
```

No visual UI is required for this milestone.

## Purpose

This proves the integration architecture before P7 UI work begins.

The test must not use a mock daemon coding engine created solely for the new path. It should exercise the actual governed Harness boundary.

## Exit Criteria

A reproducible automated integration test passes.

---

# 16. PRE-P7-12 — P7 Readiness Review

Conduct a formal review before opening P7 implementation work.

## Required Review Questions

### Architecture

- Is the StackMind authority boundary still clear?
- Is the TUI still only a client?
- Is the Harness still the single execution authority?
- Are Agent Roles and Execution Backends clearly separated?

### Runtime

- Is cancellation operation-scoped?
- Are parent/child Operations safe?
- Are retries represented correctly?

### Filesystem

- Are Work Order workspaces isolated?
- Is canonical promotion serialized?
- Are conflicts defined?

### Autonomous control

- Are budgets bounded?
- Are retries bounded?
- Are plan revisions bounded?
- Is rollback/failure behavior defined?

### Security

- Is the daemon authenticated?
- Are secrets isolated?
- Are all privileged operations governed?
- Is terminal output sanitized?

### Persistence

- Is recovery deterministic?
- Is event/state growth bounded?
- Is schema migration defined?

### Product scope

- Is P7 clearly separate from P6?
- Is the first P7 milestone headless?
- Is the UI intentionally deferred until the runtime proof passes?

---

# 17. P7 Readiness Scorecard

P7 should be marked:

```text
READY
```

only when every row is `PASS`.

| Gate | Requirement | Status |
|---|---|---|
| PRE-P7-0 | Documentation/current-state reconciliation | ☐ |
| PRE-P7-1 | TUI/runtime language decision | ☐ |
| PRE-P7-2 | Operation-scoped cancellation proven | ☐ |
| PRE-P7-3 | Single Harness authority proven | ☐ |
| PRE-P7-4 | Work Order/Operation identity frozen | ☐ |
| PRE-P7-5 | Workspace/merge/promotion contract | ☐ |
| PRE-P7-6 | Autonomous execution budgets/retries | ☐ |
| PRE-P7-7 | Role/backend contract | ☐ |
| PRE-P7-8 | Plan lifecycle contract | ☐ |
| PRE-P7-9 | Security specification | ☐ |
| PRE-P7-10 | Persistence/recovery specification | ☐ |
| PRE-P7-11 | Headless P7 integration proof | ☐ |
| PRE-P7-12 | Formal readiness review | ☐ |

---

# 18. Explicit Non-Goals

The prerequisite phase must not:

- implement the full P7 autonomous delivery product;
- implement the full multi-role dashboard;
- build multiple execution backend adapters;
- implement unrestricted subagent orchestration;
- introduce a database solely because of P7;
- replace the existing Harness;
- create a second coding/runtime engine;
- redesign StackMind's governance layer wholesale;
- optimize performance without evidence;
- introduce distributed/multi-user execution.

---

# 19. Recommended Implementation Order

```text
PRE-P7-0
Documentation truth
      ↓
PRE-P7-1
Language / packaging decision
      ↓
PRE-P7-2
P6 operation correctness
      ↓
PRE-P7-3
Harness integration boundary
      ↓
PRE-P7-4
Work Order / Operation identity
      ↓
PRE-P7-5
Workspace / promotion model
      ↓
PRE-P7-6
Autonomous control limits
      ↓
PRE-P7-7
Role / backend contract
      ↓
PRE-P7-8
Plan lifecycle
      ↓
PRE-P7-9
Security specification
      ↓
PRE-P7-10
Persistence / recovery
      ↓
PRE-P7-11
Headless P7 integration proof
      ↓
PRE-P7-12
Readiness review
      ↓
===========================
      PLAN_P7
===========================
```

---

# 20. Final P7 Entry Gate

`PLAN_P7.md` may begin implementation only after this statement can be signed off:

> **The StackMind P6 runtime is factually documented, operation cancellation is correct, the existing Harness is the single governed execution path, Work Order and Operation identities are stable, concurrent workspace/promotion semantics are defined, autonomous budgets and failure behavior are bounded, Agent Role → Execution Backend routing is specified, plan lifecycle is durable, daemon security is explicit, persistence/recovery is bounded, and a headless P7 integration proof has passed.**

At that point P7 is no longer an architectural experiment.

It becomes an implementation phase built on a proven runtime foundation.
