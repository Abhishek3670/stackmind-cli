# StackMind Agent Runtime Roadmap

**Document:** `STACKMIND_AGENT_RUNTIME_ROADMAP.md`  
**Date:** 2026-09-07  
**Basis:** StackMind Agent Runtime Readiness & Architectural Prerequisites + current architectural recommendations  
**Primary objective:** Evolve StackMind from an external governance/knowledge CLI into a first-class, provider-agnostic agent runtime in which StackMind owns the execution boundary, workspace, verification, and learning evidence.

---

## 1. Executive Recommendation

### Verdict

**GO — but only Kernel-First.**

Do **not** begin by building a polished TUI, a large daemon, or multi-agent orchestration. The current research establishes that StackMind has a strong knowledge/learning foundation but an immature execution runtime: unsandboxed live-shell execution, no concrete production provider, no tool-interception boundary, weak session semantics, and governance paths that can be bypassed by native IDE tooling.

The correct target is:

```text
                 CLIENT SURFACES
       ┌──────────────────────────────────────┐
       │ StackMind CLI / TUI / IDE / MCP / API │
       └──────────────────┬───────────────────┘
                          │
                          ▼
              ┌──────────────────────┐
              │  STACKMIND RUNTIME    │
              │  KERNEL / DAEMON      │
              ├──────────────────────┤
              │ Session Manager      │
              │ Identity / Auth      │
              │ Contract Engine      │
              │ Operation Journal    │
              │ Tool / Policy Gate   │
              │ Provider Gateway     │
              │ Sandbox Supervisor   │
              │ Verification         │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ KNOWLEDGE + LEARNING │
              │ existing StackMind   │
              │ v3.x assets          │
              └──────────────────────┘
```

The governing architectural rule should be:

> **The provider/model may reason about the workspace, but StackMind owns authorization and side effects.**

That means an agent should request `read_file`, `write_file`, `run_command`, `query_graph`, etc. from StackMind rather than directly owning unrestricted filesystem or shell access.

---

## 2. What This Roadmap Optimizes For

The roadmap is designed around five non-negotiable properties:

1. **Enforceability** — policy must sit on every path capable of producing a governed side effect.
2. **Authentic evidence** — verification and learning must use actual observations, not synthetic success fields or regex-only approximations.
3. **Provider independence** — Codex, Claude, AGY, OpenAI-compatible APIs, Anthropic, and local providers should be replaceable behind one gateway.
4. **Workspace ownership** — StackMind controls where and how agent changes occur.
5. **Incremental adoption** — the first runtime milestone must be useful and testable without requiring a complete TUI or multi-agent platform.

---

# 3. Roadmap at a Glance

| Phase | Name | Priority | Primary Outcome | Suggested Gate |
|---|---|---:|---|---|
| P0 | Runtime Kernel Contract | **P0** | Define the authoritative runtime primitives | All side effects have a kernel-level request model |
| P1 | Secure Execution Kernel | **P0** | Scratch workspace + policy/tool enforcement | No governed execution writes to live repo |
| P2 | One Real Provider | **P0** | Connect one production model/provider | Real provider completes a task through StackMind tools |
| P3 | Verification + Authentic Experience | **P1** | Trustworthy observation, review, learning evidence | Every completed task produces evidence-backed results |
| P4 | Local Runtime Daemon | **P1** | Stateful multi-turn runtime | CLI becomes a thin client to a persistent session |
| P5 | IDE / MCP Integration | **P1** | Make StackMind the easiest agent path in IDEs | IDE agent operations stay inside governed boundary |
| P6 | TUI / Interactive Supervision | **P2** | Human-facing runtime interface | TUI observes and controls the real runtime, not mocks |
| P7 | Multi-Agent Runtime | **P3** | Multiple supervised agents | Agents share governed resources, not unrestricted workspace access |

### Recommended order

```text
P0 Runtime Contract
        ↓
P1 Secure Execution Kernel
        ↓
P2 One Real Provider
        ↓
P3 Verification + Experience
        ↓
P4 Local Daemon / Sessions
        ↓
P5 IDE + MCP
        ↓
P6 TUI
        ↓
P7 Multi-Agent
```

This intentionally differs from a UI-first roadmap. The runtime should exist before the interface that displays it.

---

# 4. Phase P0 — Runtime Kernel Contract

**Priority:** P0  
**Objective:** Establish the authoritative internal contract for every agent execution before implementing more surfaces.

## Build

### 4.1 Session model

Introduce an explicit `AgentSession` model with at least:

- `session_id`
- `attempt_id`
- lifecycle state (`CREATED`, `RUNNING`, `WAITING`, `PAUSED`, `VERIFYING`, `COMPLETED`, `FAILED`, `CANCELLED`)
- provider identity
- contract identity/version
- workspace identity
- timestamps
- cumulative budget/usage state

### 4.2 Identity and authorization

Separate these concepts:

- **provider identity** — which external provider is being used
- **agent/session identity** — which logical actor owns the run
- **human identity** — who authorized the session
- **authorization policy** — what that actor may do

A session token or HMAC can authenticate a local runtime session, but it should not be treated as the complete authorization system.

### 4.3 Contract engine

Build a tolerant ingestion layer:

```text
LLM-friendly YAML / JSON
        ↓
Normalizer
        ↓
Canonical immutable AgentContract
        ↓
Semantic validation
        ↓
Fail-closed runtime evaluator
```

The normalizer should tolerate common LLM output variations while the canonical internal representation remains strict.

The active contract should become immutable for the lifetime of an attempt.

### 4.4 Operation model

Every side effect should become a first-class `Operation` with:

- `operation_id`
- `session_id`
- `attempt_id`
- actor/provider identity
- operation type
- requested target
- authorization decision
- execution result
- timestamps
- parent/correlation ID

This becomes the spine for auditability, verification, replay, debugging, and later learning.

### 4.5 Runtime boundary

Define the invariant:

> No provider implementation may directly mutate the project workspace without passing through the StackMind operation boundary.

This is the architectural answer to the current IDE bypass problem.

## Reuse

Keep and build on:

- existing contract models
- knowledge graph/compiler
- snapshot/diff primitives
- experience/skill data models
- existing lock concepts where useful

## Do not do yet

- TUI
- multi-agent orchestration
- heavy container/VM architecture
- provider-specific business logic spread throughout the harness

## Exit Gate

**Kernel Contract Gate:** A test can create a session, attach a canonical contract, issue an operation request, authorize/deny it, and persist a complete operation record without directly touching the live workspace.

---

# 5. Phase P1 — Secure Execution Kernel

**Priority:** P0  
**Objective:** Eliminate the current live-workspace execution risk.

The research identified direct `subprocess.run(..., shell=True)` against the live workspace as the highest-priority vulnerability. The current D025 regex layer is not a substitute for filesystem/process isolation. fileciteturn29file6L452-L470

## Build

### 5.1 Scratch workspace

Start with the least complex robust mechanism:

```text
authoritative repo
      │
      └── git worktree / scratch workspace
                 │
                 └── agent operations
```

Each session gets its own scratch workspace. The live branch is not the agent's working directory.

### 5.2 Tool Gateway

Initial tool surface:

- `read_file`
- `write_file`
- `run_command`
- `query_graph`

Every tool request must pass through:

```text
request
  ↓
identity
  ↓
contract scope
  ↓
budget/policy
  ↓
target resolution
  ↓
sandbox/tool execution
  ↓
operation record
```

### 5.3 Process containment

For the first Windows-focused implementation, do not require heavyweight Docker/VM infrastructure. Use worktree isolation for change safety and add OS-level process containment where practical.

The research specifically recommends lightweight worktrees first and Windows Job Objects / Linux bubblewrap as the process-isolation direction. fileciteturn29file8L564-L570

### 5.4 Remove raw runner authority

Refactor/replace the current batch runner so that raw shell execution is not an alternate execution path.

The existing report explicitly recommends replacing the harness runner, Echo provider, Canary verifier, and regex-based D025 execution gate as runtime components. fileciteturn29file3L228-L234

### 5.5 Review/commit boundary

Changes should flow:

```text
scratch
  ↓
verification
  ↓
StackMind review result
  ↓
explicit commit/merge authority
  ↓
live branch
```

The agent should not silently merge its own work into the authoritative branch.

## Exit Gate

**Secure Execution Gate:** A real command that attempts to modify an out-of-scope path or the live repository is blocked by the runtime boundary rather than by model compliance or regex filtering.

---

# 6. Phase P2 — One Real Provider

**Priority:** P0  
**Objective:** Prove the complete loop using exactly one production provider before expanding provider breadth.

The current runtime uses `EchoLLMProvider`; the research found no production OpenAI/Anthropic/Gemini/Codex/Ollama integration in the analyzed runtime. fileciteturn29file2L214-L218

## Recommendation

Implement **one** provider adapter first. Do not build five adapters simultaneously.

The adapter should support:

- streaming
- multi-turn messages
- structured tool calls
- cancellation
- timeouts
- provider errors
- usage/token accounting
- contract budget awareness

The provider gateway should translate provider-specific tool-call formats into StackMind `OperationRequest` objects.

The research identifies the same missing pieces: streaming, multi-turn dialogue, function/tool-call emission, cancellation, and provider error taxonomy. fileciteturn29file7L426-L448

## Provider architecture

```text
Provider Adapter
      ↓
Provider Gateway
      ↓
Agent Intent / Tool Call
      ↓
StackMind Tool Gateway
```

### Important design rule

Do not let provider SDK types leak into the core runtime. The kernel should depend on StackMind-native request/response models.

## Exit Gate

**Real-Agent Gate:** A real external model receives context, reasons over a task, invokes StackMind tools, edits only a scratch workspace, and completes a simple work order end-to-end.

---

# 7. Phase P3 — Verification + Authentic Experience Capture

**Priority:** P1  
**Objective:** Make completed work trustworthy enough to become evidence for future learning.

The current code has verification structures, experience storage, mining, skill storage, and retrieval, but the research found important evidence-quality gaps. The existing experience capture can contain synthetic/generalized observations rather than complete per-command real outputs, and verification includes heuristic dimensions. The learning system therefore should not be promoted to autonomous authority until execution observations are real.

## Build

### 7.1 Authentic observation

Capture, per operation:

- command/request
- exit code
- stdout
- stderr
- files touched
- before/after hashes
- actual diff
- duration
- resource/usage information where available
- contract decision
- verification result

### 7.2 Verification pipeline

Retain the useful layered structure:

```text
Structural
    ↓
Historical Replay
    ↓
Sandbox Canary / Real Execution Check
    ↓
Final Verification Decision
```

Replace simulated/regex-only canary behavior with execution inside a controlled sandbox.

### 7.3 Trust dimensions

Verification dimensions should be derived from evidence wherever possible rather than caller-provided booleans.

### 7.4 Experience eligibility

A record should become `LEARNING_ELIGIBLE` only when its evidence satisfies the required trust policy.

This prevents a central failure mode: a successful-looking execution becoming training data even though the runtime did not independently observe what happened.

## Exit Gate

**Evidence Gate:** Every successful run has a complete operation trace, authentic output/diff data, independently evaluated verification, and a deterministic rule explaining whether the record is eligible for learning.

---

# 8. Phase P4 — Local Runtime Daemon + Stateful Sessions

**Priority:** P1  
**Objective:** Turn the kernel into a persistent runtime rather than a collection of one-shot CLI commands.

The research recommends a local daemon as the key architectural prerequisite so CLI, TUI, and IDE clients become thin clients over one stateful runtime. fileciteturn29file6L414-L422

## Build

- local daemon
- JSON-RPC or REST/HTTP protocol
- session lifecycle API
- cancellation/resume
- event stream
- session persistence
- operation journal
- runtime health endpoint
- lock/sandbox ownership inside daemon

## Target interaction

```text
stackmind run
      ↓
connect/start daemon
      ↓
create session
      ↓
attach provider + contract
      ↓
run tool-driven agent loop
      ↓
stream events
```

The Click CLI remains valuable, but as an administration and client surface rather than the runtime itself.

## Exit Gate

**Persistent Session Gate:** A session can survive client reconnects, continue a multi-turn task, stream runtime events, cancel an active operation, and retain its audit trail.

---

# 9. Phase P5 — IDE + MCP Integration

**Priority:** P1  
**Objective:** Make governed StackMind execution the easiest path for agents operating in Cursor, Antigravity, VS Code, and similar environments.

**Dependency:** P6 cannot start until the P5 exit gate passes. This roadmap does not assume P5 is complete today.

MCP is an integration mechanism, not the fundamental trust boundary. The trust boundary remains the StackMind runtime/tool gateway.

## Build

### 9.1 StackMind MCP server

Expose StackMind operations rather than generic unrestricted filesystem access.

Examples:

- `stackmind.read_file`
- `stackmind.write_file`
- `stackmind.run_command`
- `stackmind.query_graph`
- `stackmind.get_context`
- `stackmind.get_contract`
- `stackmind.submit_for_review`

### 9.2 IDE adapter guidance

The integration should communicate clearly:

```text
Native IDE tools → unmanaged
StackMind tools → governed
```

### 9.3 Unmanaged mode

Do not pretend the IDE can be physically controlled merely by installing MCP.

Define two explicit operating modes:

**Governed Mode** — all governed work occurs through StackMind tools and runtime. Learning eligibility is available.

**Unmanaged Mode** — native IDE/file/shell operations are allowed outside the StackMind boundary, but the run is explicitly treated as untrusted and cannot silently enter verified procedural learning.

This is a critical architectural honesty rule.

## Exit Gate

**IDE Boundary Gate:** A supported IDE agent can complete a task using StackMind tools without needing to switch to an external terminal, and the resulting work remains traceable to a governed session.

---

# 10. Phase P6 — Adopt and Integrate an Open-Source TUI

**Priority:** P2  
**Objective:** Avoid rebuilding commodity terminal UI infrastructure from scratch. Select a mature open-source TUI that closely matches StackMind's intended agent experience, then adapt it into a thin client of the StackMind runtime.

### Core principle

> **P6 builds the StackMind experience, not a second agent runtime.**

The TUI must remain a presentation/control client. It must not become an alternate execution authority. By P6, P5 must have established the IDE/MCP integration boundary and the StackMind runtime must own governed operations.

## 10.1 Candidate shortlist

### 1. OpenCode — primary candidate

**Current recommendation: #1**

OpenCode is the strongest overall fit because its current architecture separates CLI, core application services, LLM/provider/tool integration, TUI, session management, storage, and LSP integration. It also provides interactive TUI, multi-provider support, persistent sessions, tool integration, and file-change tracking. citeturn526556search4turn489036search0

**Why it fits StackMind:**

- mature terminal-first coding-agent UX
- session-oriented model
- explicit separation between TUI and core services
- multi-provider architecture
- existing file, diff, tool, and session workflows
- active open-source ecosystem

**P6 strategy:** First determine whether OpenCode's TUI can be detached cleanly from its execution authority and driven by StackMind's Runtime API. If that is feasible, prefer adaptation/forking over greenfield UI development.

### 2. Pi — best deep-adaptation candidate

**Current recommendation: #2**

Pi is particularly attractive for deep customization because its repository separates `pi-agent-core`, `pi-coding-agent`, `pi-ai`, and `pi-tui`. It provides a reusable agent runtime, multi-provider API, coding-agent CLI, and dedicated TUI package, and is MIT licensed. citeturn605357search0turn605357search10

**Why it fits StackMind:**

- strong package-level separation
- dedicated TUI library
- reusable agent/runtime components
- multi-provider architecture
- permissive MIT license

**Critical caveat:** Pi explicitly states that it does not provide a built-in permission system for restricting filesystem, process, network, or credential access. StackMind must therefore remain the security and execution authority; Pi itself must never become the sandbox boundary. citeturn605357search0

### 3. Crush — strong alternative

**Current recommendation: #3**

Crush is a polished terminal coding agent with multi-model support, session-based workflows, MCP, and LSP integration. It is a strong UX/reference candidate and a plausible StackMind client foundation. citeturn526556search1turn526556search5

**Why it fits StackMind:**

- polished coding-agent TUI
- session-based workflow
- provider flexibility
- MCP support
- LSP integration

**Critical caveat:** Crush uses the FSL-1.1-MIT license, so a fork/redistribution strategy requires deliberate licensing review before it is selected as the foundation. citeturn526556search1

## 10.2 Candidate selection process

Do **not** select a candidate based only on visual similarity. Run a focused engineering evaluation against the real StackMind runtime.

```text
OpenCode
Pi
Crush
   │
   ▼
Runtime Separation Audit
   │
   ├── Can the TUI act as a thin client?
   ├── Can direct filesystem execution be removed?
   ├── Can direct shell execution be removed?
   ├── Can StackMind own sessions?
   ├── Can StackMind own approvals?
   ├── Can StackMind own diff/commit authority?
   └── Can all governed tools route through StackMind?
   │
   ▼
StackMind Adapter Prototype
   │
   ▼
UX + performance + maintenance benchmark
   │
   ▼
Final candidate selection
```

## 10.3 Selection criteria

| Criterion | Weight | Required result |
|---|---:|---|
| Runtime separation | 25% | TUI can operate without owning execution |
| StackMind enforcement compatibility | 25% | All governed operations route through StackMind |
| Session/event compatibility | 15% | Runtime sessions/events map cleanly |
| UX quality | 15% | Strong agent-coding interaction model |
| Extensibility | 10% | StackMind-specific views/actions can be added cleanly |
| License/ecosystem | 10% | Sustainable fork/embedding/distribution model |

## 10.4 Target integration architecture

```text
                  STACKMIND TUI
             (adapted open-source UI)
                        │
                        ▼
               StackMind Runtime API
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
    Sessions          Events          Actions
       │                │                │
       └────────────────┼────────────────┘
                        ▼
                Tool / Policy Gateway
                        │
                 Contract + Identity
                        │
                     Sandbox
                        │
                  Verification
```

The TUI must **not** call the OS filesystem or shell as a privileged shortcut. It requests actions from StackMind and renders actual runtime events.

## 10.5 Reuse from the selected TUI

- conversation rendering
- streaming output
- keyboard/input handling
- command palette
- session navigation
- diff rendering
- file/project views
- status indicators
- approval dialogs
- terminal rendering primitives where useful

## 10.6 Replace with StackMind

- provider execution authority
- filesystem tools
- shell tools
- permission authority
- workspace selection
- sandbox lifecycle
- contract enforcement
- verification state
- commit/merge authority
- experience/learning eligibility

## 10.7 StackMind-specific TUI capabilities

The adopted TUI should add first-class visibility and control for:

- active contract and scope
- sandbox/worktree status
- tool authorization decisions
- operation timeline
- verification dimensions/results
- diff and changed-file evidence
- experience record status
- skill/learning eligibility
- provider/model/budget information
- human approval checkpoints

## 10.8 Exit Gate

**P6 TUI Gate:** A developer can conduct a complete StackMind session from the terminal UI without switching to another terminal, while every governed file/process operation is authorized, executed, observed, and verified by the StackMind runtime.

The TUI is successful only if it remains **replaceable**. The runtime must continue to function through CLI, API, IDE/MCP, or another future UI without changing governance semantics.

# 12. Phase P7 — Multi-Agent Runtime

**Priority:** P3  
**Objective:** Introduce multiple agents only after single-agent governance is reliable.

Do not start with peer-to-peer autonomy.

Start with a supervised topology:

```text
                 StackMind Supervisor
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
      Planner         Worker         Reviewer
          │              │              │
          └──────────────┴──────────────┘
                         │
                 Shared Kernel APIs
                         │
                Sandbox / Contracts
```

All agents should still operate through the same tool gateway, contract engine, operation journal, and verification pipeline.

## Exit Gate

**Multi-Agent Trust Gate:** Two or more agents can collaborate without receiving unrestricted access to each other's workspace or bypassing the same runtime policy boundary.

---

# 13. Overall Runtime Sequence and P6 Position

```text
P0  Runtime Kernel Contract
      ↓
P1  Secure Execution Kernel
      ↓
P2  One Real Provider
      ↓
P3  Verification + Authentic Experience
      ↓
P4  Local Runtime Daemon + Sessions
      ↓
P5  IDE + MCP Integration
      ↓
P6  Open-Source TUI Adoption
      ↓
P7  Multi-Agent Runtime
```

P5 is therefore **not skipped**. It establishes the runtime interface and IDE integration boundary. P6 consumes that boundary and focuses on terminal UX, human supervision, and StackMind-specific visualization rather than rebuilding execution infrastructure.

# 14. Cross-Cutting Workstreams

These are not separate phases; they must progress alongside the roadmap.

## A. Contract Integrity

- tolerant ingestion
- canonicalization
- strict semantics
- immutable active contract
- fail-closed behavior
- CLI contract creation/linting
- contract versioning
- observed-vs-declared scope checks

The original research identifies contract normalization and fail-closed enforcement as P0 prerequisites. fileciteturn29file3L238-L260

## B. Security

- least privilege
- workspace isolation
- process isolation
- path traversal protection
- secret handling
- provider credential isolation
- authenticated local runtime connections
- audit trail

## C. State Consistency

- atomic writes
- operation journal
- idempotent recovery
- explicit state transitions
- correlation IDs
- crash recovery

The existing report notes that `.sync` multi-file persistence is not fully atomic and that current locking is advisory. fileciteturn29file4L278-L296

## D. Observability

Standard event model:

```text
session.started
attempt.started
contract.loaded
operation.requested
operation.authorized
operation.started
operation.completed
verification.started
verification.completed
experience.recorded
review.requested
commit.approved
session.completed
```

## E. Testing

Add adversarial tests, not only happy-path tests:

- provider attempts out-of-scope write
- provider attempts path traversal
- provider executes destructive command
- provider attempts to touch live branch
- contract parser receives natural LLM-shaped YAML
- provider disconnects mid-tool call
- daemon restarts mid-session
- client disconnects/reconnects
- stale session attempts an operation
- verification receives false caller metadata
- unmanaged IDE edits appear outside StackMind

---

# 15. What to Reuse vs Refactor vs Replace

| Component | Recommendation | Direction |
|---|---|---|
| Knowledge compiler / symbol registry | **KEEP** | Make runtime context provider of truth |
| SQLite / FTS5 retrieval | **KEEP + REFACTOR** | Integrate with session/context policy |
| Experience models/store | **KEEP + REFACTOR** | Require authentic operation evidence |
| Pattern mining | **KEEP + HARDEN** | Mine only from trust-qualified evidence |
| Skill store/versioning/decay | **KEEP + HARDEN** | Centralize lifecycle transitions |
| Workspace snapshot/diff | **KEEP + EXPAND** | Feed authentic verification |
| AgentContract model | **KEEP + REFACTOR** | Add normalized ingestion + immutability |
| Existing Harness Runner | **REPLACE / SPLIT** | Convert to kernel/session components |
| Echo provider | **REPLACE** | Real provider adapter |
| D025 regex gate | **REPLACE AS PRIMARY BOUNDARY** | Sandbox + policy enforcement |
| Canary verifier | **REPLACE** | Real sandbox execution |
| CLI | **KEEP** | Make it a runtime client/admin surface |
| TUI | **DEFER** | Build after daemon/runtime |
| MCP | **ADD LATER** | Thin integration client over runtime |

The research independently reaches the same keep/refactor/replace conclusion for the runner, Echo provider, Canary verifier, and D025 gate. fileciteturn29file3L228-L234

---

# 16. Definition of "Runtime Ready"

StackMind should not declare itself a first-class agent runtime until all of the following are true:

### Runtime boundary

- Provider cannot directly mutate the authoritative workspace.
- All governed side effects cross the StackMind tool/policy gateway.
- Native IDE bypass is explicitly classified as unmanaged.

### Contract boundary

- LLM-shaped input is normalized safely.
- Invalid contracts fail closed.
- Active contracts are immutable for the attempt.

### Execution boundary

- Agent work happens in an isolated scratch workspace.
- Process execution has meaningful containment.
- Destructive command safety does not rely on regexes alone.

### Provider boundary

- At least one real provider works end-to-end.
- Tool calling, streaming, errors, cancellation, and usage are supported.

### Verification boundary

- Results are independently observed.
- Exit codes and outputs are authentic.
- Diffs are real.
- Verification decisions are evidence-derived.

### Learning boundary

- Only trust-qualified experiences become learning evidence.
- Skill promotion has one authoritative state-transition path.
- Learned procedures cannot bypass current contracts.

### Operational boundary

- Session state is durable.
- Operations are correlated and auditable.
- Crash/reconnect behavior is deterministic enough for recovery.

---

# 17. Milestones and Practical Delivery Targets

## Milestone A — Kernel Proof

**Target:** P0 complete.

Deliverable:

```bash
stackmind kernel test
```

A deterministic test demonstrates session → contract → operation → authorization → journal without direct workspace mutation.

## Milestone B — Safe Single-Agent Execution

**Target:** P1 + P2 complete.

Deliverable:

```bash
stackmind run --provider <provider> --contract <contract>
```

A real provider can solve a small task exclusively inside a scratch worktree through StackMind tools.

## Milestone C — Evidence-Backed Runtime

**Target:** P3 complete.

Deliverable:

```text
Task
 → operations
 → sandbox
 → diff
 → verification
 → experience
```

The resulting experience can be audited from raw runtime evidence.

## Milestone D — Always-On Local Runtime

**Target:** P4 complete.

Deliverable:

```bash
stackmind daemon start
stackmind session create
stackmind session attach
```

Clients can connect without owning runtime state.

## Milestone E — IDE-Native StackMind

**Target:** P5 complete.

Deliverable:

A supported IDE can use StackMind tools natively without terminal switching, while governed execution remains authoritative.

## Milestone F — Human Runtime Surface

**Target:** P6 complete.

Deliverable:

A TUI provides live supervision of the actual daemon/session.

## Milestone G — Multi-Agent Runtime

**Target:** P7 complete.

Deliverable:

Multiple supervised roles can collaborate through one governed kernel.

---

# 18. Explicit NO-GO Decisions

Until the earlier gates pass, **do not** spend major implementation effort on:

1. **Full Textual TUI first** — it would be a presentation shell around an immature runtime.
2. **Heavy Docker/VM-first architecture on Windows** — begin with practical worktree/process isolation and evolve containment as required.
3. **Peer-to-peer autonomous multi-agent negotiation** — it multiplies the policy and identity problem before single-agent execution is trustworthy.
4. **Large provider matrix** — one real provider is enough to validate the architecture; more adapters come after the boundary is stable.
5. **Autonomous skill promotion from weak evidence** — learning must follow trustworthy execution, not precede it.

The research explicitly recommends delaying TUI, heavy virtualization, and multi-agent P2P work. fileciteturn29file5L382-L388

---

# 19. Recommended Near-Term Backlog

## Sprint 1

- define runtime kernel interfaces
- define session/attempt state machine
- define operation request/response models
- define identity/auth model
- implement operation IDs + journal
- implement tolerant contract normalizer
- make contract loading fail closed
- freeze active contract per attempt

## Sprint 2

- implement scratch worktree manager
- route reads/writes/exec through tool gateway
- remove live-repo raw shell authority from runtime path
- implement path/scope enforcement
- add adversarial security tests
- establish review/commit boundary

## Sprint 3

- integrate one real provider
- implement streaming/tool calls
- implement cancellation/timeouts
- capture real stdout/stderr/exit codes
- connect provider calls to StackMind operation models

## Sprint 4

- implement independent verification pipeline
- make learning eligibility evidence-derived
- repair experience recorder fidelity
- build runtime report/event stream

## Sprint 5+

- daemon/session persistence
- MCP/IDE integration
- TUI
- multi-agent supervision

---

# 20. Final Recommendation

StackMind should **not** evolve by simply adding more commands, more validators, or a nicer interface around the current Harness Runner.

It should evolve around a **Runtime Kernel**.

The existing Knowledge Graph, retrieval, experience, skill, and procedural-learning systems become the intelligence substrate. The missing layer is an authoritative execution substrate that owns:

```text
identity
contracts
authorization
sessions
operations
tools
workspace isolation
process isolation
verification
learning eligibility
```

Once that kernel exists, CLI, MCP, IDE integration, TUI, and multi-agent orchestration become clients/features layered on top of the same trustworthy boundary rather than separate execution paths.

### Strategic end state

```text
                 ┌────────────────────────┐
                 │     User / Developer   │
                 └────────────┬───────────┘
                              │
                  CLI / IDE / TUI / MCP / API
                              │
                              ▼
               ┌─────────────────────────────┐
               │      STACKMIND RUNTIME      │
               │                             │
               │ Sessions + Identity         │
               │ Contract + Policy           │
               │ Tool Gateway                │
               │ Provider Gateway             │
               │ Sandbox Supervisor           │
               │ Verification                 │
               │ Operation Journal            │
               └──────────────┬──────────────┘
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
        Knowledge / Context           Workspace / Tools
        Experience / Skills           Scratch / Git / Exec
                 │                         │
                 └────────────┬────────────┘
                              ▼
                      Evidence-backed
                     procedural learning
```

**The architectural goal is not "a CLI that launches agents." It is "a runtime that agents operate inside."**

That distinction should govern every implementation decision that follows.
