# Deep Research Task: StackMind TUI / CLI / API Agent Runtime

## Objective

Conduct a deep, evidence-driven architectural study of the current StackMind codebase to determine whether StackMind is technically and architecturally ready to evolve from a CLI utility / governance framework into a **first-class agent runtime with its own CLI/TUI/API**, where external agent/model providers such as Codex and Antigravity (AGY) are integrated behind StackMind rather than operating directly on the project workspace.

The proposed target architecture is:

```text
                         USER
                           │
                           ▼
                  ┌──────────────────┐
                  │     StackMind     │
                  │   CLI / TUI / API │
                  └────────┬─────────┘
                           │
                  Agent Session Manager
                           │
             ┌─────────────┼─────────────┐
             │             │             │
          Identity      Contract       Context
             │             │             │
             └─────────────┼─────────────┘
                           ▼
                  Provider / Agent Gateway
                    │                 │
                    ▼                 ▼
                 Codex API          AGY API
                    │                 │
                    └────────┬────────┘
                             ▼
                       Agent Intent
                             │
                             ▼
                 ┌──────────────────────┐
                 │ StackMind Tool /     │
                 │ Enforcement Gateway  │
                 └──────────┬───────────┘
                            │
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
               Files      Shell       Git
                 │          │          │
                 └──────────┼──────────┘
                            ▼
                         Sandbox
                            │
                            ▼
                  Independent Verification
                            │
                            ▼
                    Experience Store
                            │
                            ▼
                  Procedural Learning
```

The goal is **not** to implement this architecture.

The goal is to determine:

1. What StackMind already has.
2. What is actually production-capable today.
3. What architectural prerequisites are missing.
4. Which existing components can be reused.
5. Which components must be redesigned.
6. Which assumptions in the current architecture become invalid once StackMind owns the agent runtime.
7. What the minimum viable architecture should be before implementation starts.
8. Whether the proposed direction is technically sound, and what risks or alternative architectures should be considered.

---

# Research Rules

## 1. Code is authoritative

Treat the current repository implementation as the source of truth.

Do not assume that README files, AGENTS.md, design documents, plans, comments, tests, or architectural claims are implemented.

For every important capability, classify it as:

- ✅ IMPLEMENTED
- 🟡 PARTIAL / FOUNDATION EXISTS
- 🔵 DOCUMENTED ONLY
- 🟠 PROPOSED / NOT IMPLEMENTED
- 🔴 BLOCKED
- ⚫ DEPRECATED

Do not upgrade a capability merely because a test or documentation claims it exists.

Distinguish:

- "code exists"
- "code is wired into runtime"
- "code is actually enforced"
- "code has meaningful tests"
- "code has production-grade semantics"

---

# 2. Establish the exact repository baseline

First determine:

- current branch
- exact HEAD commit
- repository version
- package version
- Python version requirements
- current CLI entry point
- current supported runtime model
- current provider interfaces
- current agent roles
- current `.sync` model
- current harness execution model
- current knowledge layer
- current procedural-learning implementation
- current verification implementation
- current tests and CI state.

Explicitly identify documentation/code version drift.

Do not use an older commit when a newer `main` exists.

---

# 3. Analyze the proposed StackMind Runtime as a system

Study whether StackMind can realistically become:

> the environment in which an agent operates

instead of:

> a CLI that an external IDE agent is expected to voluntarily invoke.

Analyze this distinction carefully.

Determine where the current system relies on:

- AGENTS.md behavioral instructions
- voluntary CLI invocation
- cooperative agents
- direct filesystem access
- direct terminal access
- host IDE permissions
- advisory locks
- caller-provided identity
- caller-provided contracts
- caller-provided verification
- caller-provided approval labels.

Identify every place where an agent can bypass StackMind without StackMind knowing.

---

# 4. Study the runtime architecture

Investigate whether StackMind has suitable foundations for:

## Agent session management

Determine whether the current architecture can represent:

- agent session
- provider
- model
- session ID
- attempt ID
- work order
- contract
- sandbox
- execution state
- tool calls
- observations
- verification result
- final outcome.

Identify missing lifecycle states.

Propose a state machine where useful.

---

# 5. Provider abstraction / Codex / AGY integration

Study the existing `LLMProvider`, agent interfaces, request/response models, CLI provider wiring, and runtime orchestration.

Determine the correct abstraction between:

```text
StackMind
```

and:

```text
Codex / AGY / other agent providers
```

Answer:

- Should Codex/AGY behave as model providers?
- agent providers?
- execution backends?
- external agent runtimes?
- protocol adapters?
- tool-using workers?
- combinations of the above?

Determine what should remain provider-specific versus StackMind-owned.

Investigate whether the current provider abstraction is sufficient for:

- streaming
- tool calls
- structured actions
- multi-turn execution
- interruptions
- cancellation
- retries
- token/cost accounting
- model identity
- provider failures
- context windows
- asynchronous execution
- partial execution.

---

# 6. CLI/TUI architecture

Study the existing Click CLI and determine whether it should become the primary StackMind runtime interface.

Evaluate:

- Click architecture
- command grouping
- state/session handling
- interactive mode
- non-interactive mode
- streaming output
- agent conversations
- tool invocation visibility
- approval prompts
- diffs
- verification results
- sandbox status
- event timeline
- log inspection
- concurrent sessions.

Determine whether the current CLI should:

A. remain a traditional command-oriented CLI,

B. gain an interactive TUI,

C. become a combined CLI/TUI runtime,

D. expose a local daemon/API while CLI/TUI become clients.

Recommend one architecture and explain why.

---

# 7. API architecture

Determine whether StackMind needs a first-class local API / daemon.

Investigate possible boundaries such as:

```text
stackmind CLI
       │
       ▼
StackMind Runtime API
       │
       ├── Session Manager
       ├── Contract Engine
       ├── Tool Gateway
       ├── Sandbox Manager
       ├── Provider Gateway
       ├── Knowledge API
       ├── Verification
       └── Learning
```

Evaluate:

- in-process API
- local HTTP API
- Unix socket / named pipe
- JSON-RPC
- gRPC
- CLI subprocess protocol.

Do not choose technology merely because it is popular.

Choose based on StackMind's actual runtime requirements.

---

# 8. Sandbox architecture

This is one of the highest-priority research areas.

Determine what StackMind would need to own a trustworthy execution sandbox.

Investigate:

- filesystem isolation
- process isolation
- shell execution
- network isolation
- environment isolation
- resource limits
- CPU limits
- memory limits
- command timeout
- child process handling
- signal propagation
- cancellation
- filesystem snapshots
- rollback
- git isolation
- secret isolation
- credential handling
- host/guest boundaries
- Windows compatibility
- Linux/WSL compatibility
- macOS implications where relevant.

Compare possible implementation strategies:

- temp workspace copy
- OS process sandboxing
- containers
- Docker/Podman
- bubblewrap
- Firejail
- Windows Job Objects
- Windows Sandbox
- WSL isolation
- VM-based sandbox
- external sandbox providers.

Do not assume a filesystem copy alone is a security sandbox.

Explicitly distinguish:

**change isolation**

from:

**process/security isolation**.

Determine the minimum sandbox necessary for an initial StackMind runtime.

---

# 9. Tool Gateway

Determine how StackMind should expose:

- read_file
- write_file
- edit_file
- search
- grep
- shell
- git
- test
- graph query
- knowledge retrieval
- skill retrieval.

Study whether tool execution should be:

```text
Provider → StackMind Tool Gateway → Policy → Sandbox → Tool
```

rather than:

```text
Provider → host IDE native tools
```

Define the trust boundary.

Determine whether every side effect can realistically pass through this gateway.

Identify which operations cannot be controlled if the external provider is running inside a third-party IDE.

---

# 10. Contract architecture

Study the current Contract system deeply.

Determine:

- how contracts are created
- how they are validated
- how they are stored
- how identity is associated
- how expiry works
- how scope is evaluated
- how budget is represented
- whether budgets are enforced
- whether contracts can be modified after activation
- whether contracts are content-addressed
- whether contracts can be forged or replaced
- whether contracts are authoritative for all runtime operations.

Evaluate the proposed:

```text
LLM-friendly contract input
        ↓
Normalizer
        ↓
Canonical Contract
        ↓
Semantic validation
        ↓
Identity binding
        ↓
Immutable contract
```

architecture.

Determine whether schema normalization should occur at the API layer, CLI layer, parser layer, or domain layer.

---

# 11. Identity and authority

Study the existing CEO → Claude → Gemma → Worker authority model.

Determine how that model translates into a real runtime.

Investigate the difference between:

```text
actor = "claude"
```

and:

```text
authenticated identity
```

Determine what is required for:

- signed contracts
- signed approvals
- trusted provider identity
- session identity
- authorization decisions
- skill promotion
- rollback
- high-risk actions.

Recommend an identity architecture suitable for local-first StackMind.

---

# 12. Verification architecture

Study the existing:

- structural verification
- replay verification
- canary verification
- verification receipts
- trust levels
- verification dimensions.

Determine which parts are genuinely useful and which are currently heuristic.

Explicitly evaluate:

```text
Structural verification
Historical replay
Canary / sandbox execution
Behavioral testing
Outcome verification
Security verification
```

Determine what must happen before an execution can become:

```text
VERIFIED
```

and what must happen before it can become:

```text
LEARNING_ELIGIBLE
```

Do not allow LLM-reported success to count as independent evidence.

---

# 13. Procedural learning implications

Study how the proposed StackMind runtime affects the procedural-learning design.

Determine what metadata must be automatically captured for every runtime operation:

- provider
- model
- identity
- session
- attempt
- contract
- sandbox
- command
- tool
- input
- output
- diff
- tests
- verification
- environment
- outcome
- cost
- timing.

Determine how the new runtime could make the learning system substantially more trustworthy.

Analyze:

```text
Execution
  ↓
Observation
  ↓
Independent verification
  ↓
Experience
  ↓
Pattern mining
  ↓
Candidate skill
  ↓
Evaluation
  ↓
Promotion
```

and identify what is still missing.

---

# 14. Compare the architecture against modern agent runtimes

Research current architectures for:

- Codex-style CLI agents
- Claude Code
- Antigravity / AGY
- Cursor
- Windsurf
- OpenHands
- SWE-agent
- other serious coding-agent runtimes.

Focus specifically on:

- tool execution
- sandboxing
- agent loop
- provider abstraction
- permissions
- context management
- session persistence
- state
- verification
- extensibility
- IDE integration
- local/remote execution.

Do not make superficial feature comparisons.

Extract the architectural patterns that matter for StackMind.

Use primary technical documentation where available.

---

# 15. Research modern sandbox and agent-runtime prerequisites

Use authoritative sources where possible.

Research:

- container isolation
- process sandboxing
- local agent security
- command execution isolation
- filesystem virtualization
- credential isolation
- agent tool authorization
- MCP security boundaries
- long-running agent runtimes
- local daemon architectures
- TUI architectures for developer tools.

Pay particular attention to the difference between:

- policy
- authorization
- containment
- observation
- verification.

---

# 16. Identify architectural prerequisites

Produce a prerequisite matrix.

Example:

| Prerequisite | Current State | Required for Runtime? | Gap | Priority |
|---|---|---:|---|---|
| Agent session model | ... | Yes | ... | P0 |
| Provider gateway | ... | Yes | ... | P0 |
| Tool gateway | ... | Yes | ... | P0 |
| Sandbox | ... | Yes | ... | P0 |
| Identity | ... | Yes | ... | P0 |
| Contract enforcement | ... | Yes | ... | P0 |
| Verification | ... | Yes | ... | P0 |
| Event journal | ... | Yes | ... | P1 |
| API daemon | ... | Maybe | ... | P1 |
| TUI | ... | No | ... | P2 |

Use actual evidence from the codebase.

---

# 17. Determine architectural reuse vs rewrite

Create a matrix:

| Existing Component | Reuse | Refactor | Replace | Reason |
|---|---:|---:|---:|---|

Evaluate at minimum:

- CLI
- KnowledgeAPI
- graph compiler
- contracts
- harness
- retrieval
- snapshot system
- lock
- experience store
- learning
- skill store
- verification
- decay
- `.sync`
- work orders
- shutdown
- init/migrate.

Do not recommend rewriting components merely because they are imperfect.

Prefer incremental evolution when the underlying abstraction is sound.

---

# 18. Define the minimum viable StackMind Runtime

Design the smallest architecture that can safely support:

1. user starts StackMind
2. user selects provider
3. provider receives bounded context
4. provider proposes an action
5. StackMind authorizes it
6. StackMind executes it in an isolated environment
7. StackMind observes the resulting state
8. StackMind verifies the outcome
9. user approves promotion/apply
10. StackMind records trusted experience.

Do not design the entire future platform first.

Identify the smallest trustworthy kernel.

---

# 19. Produce an architecture maturity score

Score the current codebase from 0–10 for:

- Runtime architecture
- CLI maturity
- TUI readiness
- API readiness
- Provider abstraction
- Agent session management
- Sandbox readiness
- Tool gateway
- Contract system
- Identity / authorization
- Verification
- Observability
- State consistency
- Procedural learning
- Security
- Testing / CI
- Documentation / operational readiness
- Cross-platform readiness.

For every score provide evidence and explain why.

Avoid vague ratings.

---

# 20. Produce a Go / No-Go recommendation

At the end, answer:

### Can StackMind safely become the primary agent runtime today?

Choose one:

- GO
- CONDITIONAL GO
- NO-GO

Then explain:

1. What makes the direction viable.
2. What currently prevents it.
3. What must be fixed first.
4. What can safely be prototyped in parallel.
5. What should explicitly NOT be implemented yet.

---

# Required Report Structure

Write the final report as:

# StackMind Agent Runtime Readiness & Architectural Prerequisites

## 1. Executive Summary

## 2. Proposed Runtime Architecture

## 3. Current StackMind Architecture

## 4. Current Codebase Maturity

## 5. Architectural Disconnects

## 6. Runtime / TUI / CLI / API Assessment

## 7. Provider Gateway Assessment

## 8. Sandbox Requirements

## 9. Tool Gateway & Enforcement Boundary

## 10. Contract / Identity / Authorization Assessment

## 11. Verification Architecture

## 12. Procedural Learning Implications

## 13. Existing Components Reusable vs. Refactor vs. Replace

## 14. Prerequisite Matrix

## 15. Trust-Critical Gaps

## 16. Recommended Target Architecture

## 17. Minimum Viable Runtime Kernel

## 18. Phased Implementation Roadmap

## 19. Risks and Failure Modes

## 20. Go / No-Go Decision

## 21. Final Recommendation

---

# Required Status Table

Use this exact format where appropriate:

| Component / Capability | Status | Current Evidence | Proposed Requirement | Gap | Dependency / Blocker | Verification Confidence |
|---|---|---|---|---|---|---|

Statuses must be:

- ✅ IMPLEMENTED
- 🟡 PARTIAL / FOUNDATION EXISTS
- 🔵 DOCUMENTED ONLY
- 🟠 PROPOSED / NOT IMPLEMENTED
- 🔴 BLOCKED
- ⚫ DEPRECATED

Verification confidence:

- High
- Medium
- Low

---

# Evidence Standards

For every major architectural conclusion:

1. Cite the exact source file.
2. Cite the relevant function/class/configuration.
3. Explain what the code actually does.
4. Distinguish implementation from intention.
5. Identify uncertainty explicitly.

Do not cite only README or planning documents when source code exists.

When documentation conflicts with code, report the conflict rather than choosing whichever sounds better.

---

# Important Existing Findings to Re-verify

The investigation should independently verify, rather than blindly accept, these known concerns:

- LLM-suggested shell commands executing against live workspace.
- Lack of real sandbox execution.
- Verification dimensions derived rather than independently measured.
- Skill revalidation / rollback bypassing the normal promotion authority.
- Constructor-bound contract propagation problems.
- Read-only context calls causing filesystem mutation.
- Advisory lock limitations.
- Non-atomic state/projection updates.
- Weak identity binding.
- Heuristic D025 enforcement.
- Learning evidence that can become eligible without sufficiently strong observation.
- Missing provider runtime integration.
- Missing or incomplete CI evidence.
- Version / documentation drift.
- `.sync` persistence model.
- Work-order execution and session lifecycle shortcomings.

These are hypotheses to verify against the current repository, not assumptions to copy into the report.

---

# Research Deliverable

Produce a complete technical report that an architect can use to decide whether to begin implementation of the StackMind-native runtime.

The report should end with:

## "Implementation Readiness Verdict"

Include:

- overall readiness score
- trust readiness score
- runtime readiness score
- sandbox readiness score
- provider integration readiness score
- learning readiness score
- top 5 blockers
- top 5 reusable assets
- recommended Phase 0
- recommended Phase 1
- recommended Phase 2
- explicit NO-GO items.

Do not modify StackMind source code during this research.

Do not implement features.

Do not run destructive commands.

Do not present documentation claims as implementation evidence.

The goal is to determine **what StackMind must become before it can safely own the agent execution environment.**