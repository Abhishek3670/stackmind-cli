# StackMind — 5-Agent Responsibility & Access Matrix

**Status:** Implemented (2026-10-08) — enforcement mapping in [§19 Implementation Map](#19-implementation-map-code-enforced)  
**Purpose:** Define the responsibilities, capabilities, tool privileges, context access, write boundaries, authority, and hard restrictions for all StackMind agents.

---

## 1. Agent Architecture

```text
                         ┌───────────────────┐
                         │    CEO / USER     │
                         │ Product Direction │
                         └─────────┬─────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │ Claude            │
                         │ Senior Architect  │
                         │ Plan / Govern     │
                         └─────────┬─────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  │                │                │
                  ▼                ▼                ▼
          ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
          │    Codex     │ │    Gemini    │ │    Gemma     │
          │   Backend    │ │   Frontend   │ │      QA      │
          └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                 │                │                │
                 └────────────────┼────────────────┘
                                  ▼
                         ┌───────────────────┐
                         │    Local-LLM      │
                         │ GitOps / Release  │
                         └───────────────────┘
```

### Core Principle

> **Shared knowledge, isolated authority.**

All agents may understand the project through StackMind's read-oriented knowledge layer, but no agent automatically inherits another agent's write or mutation privileges.

---

# 2. Master Responsibility Matrix

| Dimension | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|---|---|---|---|---|
| Canonical Role | Senior Architect | Backend Lead | Frontend Lead | QA / Security Lead | GitOps / Release Lead |
| Primary Objective | Architecture, planning, governance | Backend implementation | Frontend implementation | Verification and quality | Repository release |
| Product Planning | **Authority** | Consulted | Consulted | Consulted | Informed |
| Architecture | **Authority** | Consulted | Consulted | Consulted | Informed |
| Work Orders | **Create / Update** | Execute | Execute | Verify | Execute release WO |
| Contracts | **Author** | Consume | Consume | Verify | Consume |
| Backend Code | ❌ | **Write** | ❌ | ❌ | ❌ |
| Frontend Code | ❌ | ❌ | **Write** | ❌ | ❌ |
| Test Code | ❌ | Backend tests | Frontend tests | **Write / Own** | ❌ |
| QA Verdict | ❌ | ❌ | ❌ | **Authority** | ❌ |
| Git Staging | ❌ | ❌ | ❌ | ❌ | **Authority** |
| Git Commit | ❌ | ❌ | ❌ | ❌ | **Authority** |
| Git Tag | ❌ | ❌ | ❌ | ❌ | **Authority** |
| Git Push | ❌ | ❌ | ❌ | ❌ | **Authority** |
| Release | ❌ | ❌ | ❌ | Verify | **Execute** |
| Architecture Change | **Authority** | ❌ | ❌ | ❌ | ❌ |
| Scope Change | **Authority** | ❌ | ❌ | ❌ | ❌ |
| Contract Override | ❌ | ❌ | ❌ | ❌ | ❌ |
| Self-Approval | ❌ | ❌ | ❌ | ❌ | ❌ |
| Cross-Agent Code Modification | ❌ | ❌ | ❌ | ❌ | ❌ |

---

# 3. Claude — Senior Architect

## Mission

Convert user/product intent into an executable, bounded, and verifiable implementation plan.

### Responsibilities

- Understand the complete repository architecture.
- Analyze dependencies and blast radius.
- Mine existing implementation patterns.
- Create and update `PLAN.md`.
- Decompose work into Work Orders.
- Author fail-closed Contracts.
- Resolve architectural dependencies.
- Define acceptance criteria.
- Coordinate implementation boundaries.
- Maintain governance artifacts.

### Write Scope

```text
PLAN.md
.sync/work-orders/**
.sync/contracts/**
.sync/decisions/**
.sync/inbox/**
```

### Tool Access

| Capability | Access |
|---|:---:|
| StackMind Graph | ✅ |
| Semantic Search | ✅ |
| Context Retrieval | ✅ |
| Impact Analysis | ✅ |
| Dependency Analysis | ✅ |
| Data Flow Analysis | ✅ |
| File Read | ✅ |
| Repository Discovery | ✅ |
| Git Inspection | ✅ |
| Work Order Creation | ✅ |
| Work Order Update | ✅ |
| Plan Mode | ✅ |
| Contract Authoring | ✅ |
| Contract Verification | ✅ |
| Scope Verification | ✅ |
| Command Execution | Inspect (§10) |
| Application Code Writing | ❌ |
| Test Writing | ❌ |
| Browser | ❌ |
| QA Verdict | ❌ |
| Git Mutation | ❌ |

`Inspect` = command execution is offered only in the architecture-investigation palette for read-only diagnosis (`validators/kernel/providers/gateway.py`, `ARCHITECTURE_INVESTIGATION_TOOL_NAMES`); the planning and authoring palettes do not offer it, and every invocation still passes the hard interpreter denylist and resource sandbox. The architect's own contract scope keeps writes confined to governance artifacts.

### Hard Restrictions

```text
DENY:
- Application source modification
- Frontend modification
- Backend implementation
- Test implementation
- Git commit
- Git push
- Release creation
- QA approval bypass
- Contract enforcement bypass
```

---

# 4. Codex — Backend Lead

## Mission

Implement backend functionality strictly within the assigned Work Order and Contract.

### Responsibilities

- REST/gRPC APIs.
- Backend business logic.
- Database models and queries.
- Backend services.
- Internal backend scripts.
- Backend integration.
- Backend unit/integration tests where assigned.
- Backend validation and local verification.

### Write Scope

```text
src/**
app/**
api/**
database/**
```

Only paths explicitly authorized by the active Work Order and Contract are writable.

### Tool Access

| Capability | Access |
|---|:---:|
| StackMind Graph | ✅ |
| Semantic Search | ✅ |
| Context Retrieval | ✅ |
| Impact Analysis | ✅ |
| Dependency Analysis | ✅ |
| Data Flow Analysis | ✅ |
| File Read | ✅ |
| Repository Discovery | ✅ |
| Git Inspection | ✅ |
| Backend Code Writing | ✅ |
| Backend Refactoring | ✅ |
| Command Execution | ✅ |
| Tests | ✅ |
| Lint | ✅ |
| Typecheck | ✅ |
| Security Scan | ✅ |
| Browser | ❌ |
| Contract Authoring | ❌ |
| QA Verdict | ❌ |
| Work Order Closure | ❌ |
| Git Commit | ❌ |
| Git Push | ❌ |
| Release | ❌ |

### Hard Restrictions

```text
DENY:
- Frontend/UI modification
- Contract modification
- Architecture modification
- Product-scope modification
- Git mutation
- Work Order self-closure
- QA self-approval
- Modification outside assigned scope
```

---

# 5. Gemini — Frontend Lead

## Mission

Implement the browser/UI experience within the assigned frontend Work Order and validate it through browser-based verification.

### Responsibilities

- HTML.
- CSS.
- DOM structure.
- Client-side JavaScript.
- UI/UX.
- Responsive design.
- Browser behavior.
- Frontend integration.
- Frontend tests where assigned.
- Browser validation.

### Write Scope

```text
index.html
styles/**
scripts/**
public/**
views/**
```

Only paths explicitly authorized by the active Work Order and Contract are writable.

### Tool Access

| Capability | Access |
|---|:---:|
| StackMind Graph | ✅ |
| Semantic Search | ✅ |
| Context Retrieval | ✅ |
| Impact Analysis | ✅ |
| Dependency Analysis | ✅ |
| File Read | ✅ |
| Repository Discovery | ✅ |
| Git Inspection | ✅ |
| Frontend Code Writing | ✅ |
| Frontend Refactoring | ✅ |
| Command Execution | ✅ |
| Tests | ✅ |
| Lint | ✅ |
| Typecheck | ✅ |
| Security Scan | ✅ |
| Browser Open | ✅ |
| Browser Screenshot | ✅ |
| Browser Click | ✅ |
| Contract Authoring | ❌ |
| Backend Modification | ❌ |
| Database Modification | ❌ |
| API Implementation | ❌ |
| QA Verdict | ❌ |
| Git Commit | ❌ |
| Git Push | ❌ |
| Release | ❌ |

### Fail-Closed Rule

```text
Browser/Test Environment Broken
             │
             ▼
          BLOCKED
             │
             ▼
       Do not bypass gate
       Do not fake validation
```

---

# 6. Gemma — QA / Security Lead

## Mission

Independently determine whether implementation satisfies the Work Order, Contract, acceptance criteria, tests, and security requirements.

Gemma is a **verification authority**, not an implementation fallback.

### Responsibilities

- Test creation.
- Test execution.
- Regression testing.
- Contract verification.
- Scope verification.
- Security auditing.
- Browser validation.
- Static analysis.
- Quality gates.
- Verdict generation.
- Change requests.

### Write Scope

```text
tests/**
.sync/inbox/claude/*-verdict.md
.sync/reviews/**
```

### Tool Access

| Capability | Access |
|---|:---:|
| StackMind Graph | ✅ |
| Semantic Search | ✅ |
| Context Retrieval | ✅ |
| Impact Analysis | ✅ |
| Dependency Analysis | ✅ |
| File Read | ✅ |
| Repository Discovery | ✅ |
| Git Inspection | ✅ |
| Test Writing | ✅ |
| Test Execution | ✅ |
| Lint | ✅ |
| Typecheck | ✅ |
| Security Scan | ✅ |
| Browser Open | ✅ |
| Browser Screenshot | ✅ |
| Browser Click | ✅ |
| Contract Verification | ✅ |
| Scope Verification | ✅ |
| Submit Verdict | ✅ |
| Request Changes | ✅ |
| Approve Work Order | ✅ |
| Application Code Modification | ❌ |
| Contract Authoring | ❌ |
| Architecture Modification | ❌ |
| Git Mutation | ❌ |
| Release | ❌ |

### Verdict States

```text
PASS
NEEDS_CHANGES
BLOCKED
```

### Critical Rule

If a test fails because of an application defect:

```text
Gemma
  │
  ├── Diagnose
  ├── Document
  └── NEEDS_CHANGES
          │
          ▼
    Responsible Developer
          │
          ▼
        Fix
          │
          ▼
        Gemma
          │
          ▼
      Re-validate
```

Gemma must **never repair application code to make its own verdict pass**.

This rule is runtime-enforced end to end: the QA system prompt mandates finalizing a defect finding as a `NEEDS_CHANGES` verdict plus a `blocked` decision naming the defective deliverable (`validators/harness/runner.py`, `QA_SYSTEM_PROMPT`), and the supervisor routes the finding back to the owning developer work order for bounded rework, holding the QA work order until every rework target re-completes (`validators/kernel/daemon/supervisor.py`, `_route_qa_defect_to_developers` / `qa_rework_targets`).

---

# 7. Local-LLM — GitOps / Release Lead

## Mission

Perform controlled repository mutation and release operations after implementation and QA gates have been satisfied.

### Responsibilities

- Repository staging.
- Commit creation.
- Version management.
- Changelog generation.
- Tag creation.
- Release preparation.
- Release creation.
- Git push.
- Release Work Order execution.

### Write Scope

```text
CHANGELOG.md
VERSION
VERSION.md

pyproject.toml    # version fields only
package.json      # version fields only
```

### Git Authority

```text
git_stage
git_commit
git_tag
git_push
create_release
```

Local-LLM is the exclusive agent permitted to perform these operations.

### Tool Access

| Capability | Access |
|---|:---:|
| StackMind Graph | ✅ |
| Semantic Search | ✅ |
| Context Retrieval | ✅ |
| File Read | ✅ |
| Repository Discovery | ✅ |
| Git Inspection | ✅ |
| Command Execution | ✅ |
| Git Stage | **✅** |
| Git Commit | **✅** |
| Git Tag | **✅** |
| Git Push | **✅** |
| Create Release | **✅** |
| Application Code Modification | ❌ |
| Backend Implementation | ❌ |
| Frontend Implementation | ❌ |
| Test Modification | ❌ |
| Contract Authoring | ❌ |
| Architecture Modification | ❌ |
| Product Scope Modification | ❌ |
| QA Verdict | ❌ |

### Hard Restrictions

```text
DENY:
- Feature implementation
- Backend modification
- Frontend modification
- Test modification
- Architecture modification
- Product scope changes
- Contract modification
- Unauthorized destructive Git operations
```

Destructive Git operations require explicit CEO/User authorization.

---

# 8. Universal Knowledge Layer

All five agents receive read-only access to the project's shared knowledge layer.

| Capability | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|:---:|:---:|:---:|:---:|:---:|
| `query_graph` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `get_context` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `semantic_search` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `knowledge_stats` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `find_callers` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `find_callees` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `impact_analysis` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `dependency_analysis` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `data_flow_analysis` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `read_file` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `read_many_files` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `list_directory` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `glob` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `grep` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `find_symbol` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `find_references` | ✅ | ✅ | ✅ | ✅ | ✅ |
| Git inspection | ✅ | ✅ | ✅ | ✅ | ✅ |
| Contract inspection | ✅ | ✅ | ✅ | ✅ | ✅ |

### Principle

```text
KNOWLEDGE ACCESS ≠ MUTATION AUTHORITY
```

An agent can understand something without being allowed to modify it.

---

# 9. Context and Memory Access Matrix

StackMind should use **selective context retrieval**, not full-context injection.

| Context | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|---:|---:|---:|---:|---:|
| Current User Request | Full | Relevant | Relevant | Relevant | Relevant |
| Assigned Work Order | Full | Full | Full | Full | Release WO |
| Assigned Contract | Full | Full | Full | Full | Relevant |
| Repository Knowledge | Full read | Full read | Full read | Full read | Full read |
| Relevant Architecture | Full | Relevant | Relevant | Relevant | Relevant |
| Relevant Project Memory | Relevant | Relevant | Relevant | Relevant | Relevant |
| Historical Conversations | Retrieval only | Retrieval only | Retrieval only | Retrieval only | Retrieval only |
| User Personal Memory | Relevant only | Relevant only | Relevant only | Relevant only | Relevant only |
| Complete Memory Store | ❌ | ❌ | ❌ | ❌ | ❌ |
| Complete Conversation History | ❌ | ❌ | ❌ | ❌ | ❌ |
| Other Agent Private State | ❌ | ❌ | ❌ | ❌ | ❌ |
| Unauthorized Capability Definitions | ❌ | ❌ | ❌ | ❌ | ❌ |

### Context Compilation

Every LLM invocation should conceptually follow:

```text
Task
  +
Agent Identity
  +
Relevant Memory
  +
Relevant Knowledge
  +
Work Order
  +
Contract
  +
Current Runtime State
  +
Allowed Capabilities
  ↓
Context Compiler
  ↓
LLM
```

The runtime should **not** inject all available information by default.

---

# 10. Tool Privilege Matrix

| Tool Category | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|:---:|:---:|:---:|:---:|:---:|
| Graph / Knowledge | R | R | R | R | R |
| File Discovery | R | R | R | R | R |
| Git Inspection | R | R | R | R | R |
| Planning | RW | — | — | — | — |
| Work Orders | RW | R | R | R | R |
| Contract Inspection | R | R | R | R | R |
| Contract Authoring | RW | — | — | — | — |
| Backend Code | — | RW | — | — | — |
| Frontend Code | — | — | RW | — | — |
| Test Code | — | RW* | RW* | RW | — |
| Command Execution | Inspect | RW | RW | RW | RW |
| Browser | — | — | RW | RW | — |
| QA / Security Gates | — | Execute | Execute | **Authority** | Execute |
| Verdicts | — | — | — | **RW** | — |
| Git Mutation | — | — | — | — | **RW** |
| Release | — | — | — | Verify | **RW** |

`RW*` = only tests directly associated with the agent's assigned implementation scope.

Nuance: implementation workers (codex/gemini) also hold `git_restore` and `git_create_branch` for local worktree hygiene; history-affecting operations (staging, commit, tag, push, release) remain exclusive to Local-LLM as specified above.

---

# 11. Authority Matrix

Use RACI-style governance to distinguish responsibility from authority.

| Decision | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|:---:|:---:|:---:|:---:|:---:|
| Product Requirements | **A** | C | C | C | I |
| Architecture | **A** | C | C | C | I |
| Work Decomposition | **A/R** | I | I | C | I |
| Contract Definition | **A/R** | C | C | C | I |
| Backend Implementation | A | **R** | I | C | I |
| Frontend Implementation | A | I | **R** | C | I |
| Test Implementation | C | R | R | **A** | I |
| QA Decision | I | I | I | **A/R** | I |
| Change Request | C | R | R | **A** | I |
| Git Staging | I | I | I | I | **A/R** |
| Git Commit | I | I | I | I | **A/R** |
| Git Tag | I | I | I | I | **A/R** |
| Release | I | I | I | C | **A/R** |
| Architecture Change | **A/R** | C | C | C | I |
| Scope Change | **A/R** | I | I | C | I |

```text
A = Accountable
R = Responsible
C = Consulted
I = Informed
```

---

# 12. Write Boundary Matrix

This should be enforced by the runtime, not merely described in prompts.

| Path / Resource | Claude | Codex | Gemini | Gemma | Local-LLM |
|---|:---:|:---:|:---:|:---:|:---:|
| `PLAN.md` | RW | R | R | R | R |
| `.sync/work-orders/**` | RW | R | R | R | R |
| `.sync/contracts/**` | RW | R | R | R | R |
| `.sync/decisions/**` | RW | R | R | R | R |
| `.sync/inbox/**` | RW | R | R | RW* | R |
| `src/**` | ❌ | RW | ❌ | ❌ | ❌ |
| `app/**` | ❌ | RW | ❌ | ❌ | ❌ |
| `api/**` | ❌ | RW | ❌ | ❌ | ❌ |
| `database/**` | ❌ | RW | ❌ | ❌ | ❌ |
| `index.html` | ❌ | ❌ | RW | ❌ | ❌ |
| `styles/**` | ❌ | ❌ | RW | ❌ | ❌ |
| `scripts/**` | ❌ | ❌ | RW | ❌ | ❌ |
| `public/**` | ❌ | ❌ | RW | ❌ | ❌ |
| `views/**` | ❌ | ❌ | RW | ❌ | ❌ |
| `tests/**` | ❌ | RW* | RW* | RW | ❌ |
| `CHANGELOG.md` | ❌ | ❌ | ❌ | ❌ | RW |
| `VERSION` | ❌ | ❌ | ❌ | ❌ | RW |
| `VERSION.md` | ❌ | ❌ | ❌ | ❌ | RW |
| `pyproject.toml` | ❌ | ❌ | ❌ | ❌ | Version only |
| `package.json` | ❌ | ❌ | ❌ | ❌ | Version only |
| Git database | ❌ | ❌ | ❌ | ❌ | Controlled |

`RW*` indicates scope-limited writes only.

---

# 13. Agent Lifecycle

```text
                         USER REQUEST
                              │
                              ▼
                     ┌─────────────────┐
                     │ Claude          │
                     │ PLAN            │
                     └────────┬────────┘
                              │
                    Work Order + Contract
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
             ┌─────────┐             ┌─────────┐
             │ Codex   │             │ Gemini  │
             │ Backend │             │Frontend │
             └────┬────┘             └────┬────┘
                  │                       │
                  └───────────┬───────────┘
                              ▼
                         ┌─────────┐
                         │ Gemma   │
                         │ QA      │
                         └────┬────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                  FAIL                 PASS
                    │                   │
                    ▼                   ▼
              NEEDS_CHANGES          Release Gate
                    │                   │
                    └──► Developer      ▼
                                  ┌─────────────┐
                                  │ Local-LLM   │
                                  │ Git /Release│
                                  └─────────────┘
```

---

# 14. Fail-Closed Rules

Every agent must fail closed when an operation is outside its authority.

```text
Unauthorized Tool
        ↓
CAPABILITY_DENIED
```

```text
Unauthorized Path
        ↓
SCOPE_DENIED
```

```text
Missing Work Order
        ↓
WORK_ORDER_REQUIRED
```

```text
Missing Contract
        ↓
CONTRACT_REQUIRED
```

```text
Contract Verification Failed
        ↓
CONTRACT_INVALID
```

```text
QA Gate Failed
        ↓
RELEASE_BLOCKED
```

```text
Required Environment Unavailable
        ↓
BLOCKED
```

Agents must never resolve these failures by silently expanding their own authority.

---

# 15. Runtime Enforcement Model

Permissions must be enforced outside the LLM.

```text
                    LLM
                     │
                     │ Request
                     ▼
              ┌───────────────┐
              │ Policy Engine │
              └───────┬───────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Identity     Scope      Capability
       Check        Check       Check
          │           │           │
          └───────────┼───────────┘
                      ▼
                Contract Check
                      │
                      ▼
                 State Check
                      │
               ┌──────┴──────┐
               │             │
             ALLOW          DENY
               │             │
               ▼             ▼
          Tool Executor   Structured
                          Denial
```

### Security Invariant

> **The LLM may request an operation; only the StackMind runtime can authorize and execute it.**

---

# 16. Recommended Capability Model

Capabilities should be represented independently from agent prompts.

```yaml
agents:

  claude:
    role: architect
    capabilities:
      - knowledge.read
      - repository.read
      - planning.write
      - work_order.create
      - work_order.update
      - contract.create
      - contract.update
    deny:
      - application.write
      - test.write
      - git.mutate
      - release.execute

  codex:
    role: backend
    capabilities:
      - knowledge.read
      - repository.read
      - backend.write
      - backend.execute
      - backend.test
    deny:
      - frontend.write
      - contract.write
      - git.mutate
      - qa.approve

  gemini:
    role: frontend
    capabilities:
      - knowledge.read
      - repository.read
      - frontend.write
      - frontend.execute
      - browser.inspect
      - frontend.test
    deny:
      - backend.write
      - database.write
      - contract.write
      - git.mutate
      - qa.approve

  gemma:
    role: qa
    capabilities:
      - knowledge.read
      - repository.read
      - test.write
      - test.execute
      - security.scan
      - contract.verify
      - scope.verify
      - verdict.submit
    deny:
      - application.write
      - contract.write
      - git.mutate
      - release.execute

  local-llm:
    role: gitops
    capabilities:
      - knowledge.read
      - repository.read
      - git.stage
      - git.commit
      - git.tag
      - git.push
      - release.create
      - version.update
      - changelog.update
    deny:
      - application.write
      - test.write
      - contract.write
      - architecture.write
      - scope.change
```

---

# 17. Core StackMind Invariants

The following should be treated as architectural invariants rather than prompt instructions.

### INV-001 — Knowledge Is Shared

All agents can inspect relevant project knowledge.

### INV-002 — Authority Is Isolated

Knowledge access does not grant mutation authority.

### INV-003 — Scope Is Explicit

Every implementation write must resolve against an active Work Order and Contract.

### INV-004 — Runtime Is Authoritative

The LLM cannot grant itself permissions.

### INV-005 — QA Is Independent

The agent implementing application code cannot provide the final QA approval for that work.

### INV-006 — Git Mutation Is Centralized

Only Local-LLM performs repository mutation and release operations.

### INV-007 — QA Does Not Repair Application Code

Gemma reports defects and requests changes; implementation agents perform the fixes.

### INV-008 — Fail Closed

Missing or invalid governance state results in a block rather than an inferred permission.

### INV-009 — Context Is Selective

Only task-relevant memory, knowledge, state, and capabilities are included in each LLM invocation.

### INV-010 — Capabilities Are Runtime-Enforced

Tool availability and authorization are enforced outside the model.

---

# 18. Final Responsibility Summary

| Agent | Thinks About | Can Change | Can Approve | Cannot Do |
|---|---|---|---|---|
| **Claude** | Architecture / Plan | Governance | Architecture decisions | Application code |
| **Codex** | Backend | Backend | Own implementation | Frontend / Git / QA |
| **Gemini** | Frontend | Frontend | Own browser validation | Backend / Git / QA |
| **Gemma** | Correctness / Security | Tests & verdicts | **QA** | Application code |
| **Local-LLM** | Release / GitOps | Release metadata + Git | Release execution | Features / architecture |

## Governing Principle

```text
                 KNOWLEDGE
                     │
              Shared across agents
                     │
                     ▼
              ┌──────────────┐
              │ StackMind    │
              │ Runtime      │
              └──────┬───────┘
                     │
             Capability Policy
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
      SCOPE       CONTRACT      STATE
        │            │            │
        └────────────┼────────────┘
                     ▼
                 EXECUTION
```

**StackMind should therefore be designed as a capability-controlled multi-agent runtime, not as five LLMs differentiated only by system prompts.**

---

# 19. Implementation Map (code-enforced)

Every authority row above resolves to a runtime enforcement point, not a prompt instruction. The policy catalog is machine-checked against this matrix by `tests/test_agent_matrix_enforcement.py`.

| Matrix requirement | Enforcement point |
|---|---|
| Role → capability catalog (§10, §16) | `validators/kernel/identity.py` — `get_role_policy` (:47); policy is human-assigned and never derived from provider identity (:29) |
| Runtime-is-authoritative flow (§15) | `validators/kernel/boundary.py` — `RuntimeBoundary.submit` (policy check → contract evaluation → journal, both outcomes); 100% of tool calls traverse it (`validators/kernel/tools.py`, `_authorize`) |
| Knowledge layer shared read-only (§8, INV-001/002) | Universal base set in `get_role_policy` (:54-121); knowledge tools return read-only envelopes (`validators/knowledge/api.py`); contract-scoped read filtering (`is_node_in_scope`) |
| Work orders / contracts authored by Claude only (§2, §10) | `validators/harness/authoring_gate.py` — `validate_author_role` (:97): workers can never author governed artifacts; enforced in-turn at `ToolGateway.write_file` (`validators/kernel/tools.py`) |
| Scope is explicit per WO (INV-003) | Frozen `AgentContract` (`validators/kernel/contract.py`); deny-beats-allow evaluator (:96-204); compiled worker task contract renders the same scope the gates enforce (`validators/harness/task_contract.py`) |
| Cross-agent deliverable protection (§2) | `TaskOwnership.check_write` — peer work-order deliverables are read-only in-turn (`validators/harness/task_contract.py`), attached per worker turn by the harness runner |
| Self-approval impossible / verdict authority (INV-005) | Verdict ops (`submit_verdict`/`request_changes`/`approve_work_order`) exist only in gemma's policy; **verdict-channel write gate** in `ToolGateway` (`_verdict_channel_denial`): `.sync/inbox/claude/`, `.sync/qa/verdicts/`, and verdict/review-named files under `.sync/reviews/` require verdict (gemma) or governance (claude) authority — covered for both `write_file` and `apply_patch` |
| QA does not repair application code (INV-007) | `QA_SYSTEM_PROMPT` doctrine (`validators/harness/runner.py`) + supervisor defect routing `_route_qa_defect_to_developers` with `qa_rework_targets` hold-back (`validators/kernel/daemon/supervisor.py`) |
| Git mutation centralized (INV-006) | All `git_stage/commit/tag/push`, `create_release`, `rollback_release` operations exist only in local-llm's policy (`identity.py` :254-294); `git_push` additionally requires D025's operator-controlled double lock (`tools.py`) |
| Destructive Git ops need CEO/User authorization (§7) | `validators/harness/d025_gate.py` (backup-before/verify-after sequencing) + `git_push` operator env/receipt lock (`validators/kernel/tools.py`) |
| No release without QA (§13 release gate) | `validators/harness/d024_gate.py` — GitOps work orders block until a gemma APPROVED verdict exists; pre-checked before execution and re-checked under the runtime lock |
| Application-code isolation from QA | Gemma's policy withholds `apply_patch`/`delete_file`/`move_file`/git mutations; its contract scope is bounded to the declared test suite and verdict channels (`validators/harness/authoring_compiler.py` injects the QA verdict channel) |
| Fail-closed codes (§14) | `CONTRACT_SCOPE_DENIED` / `CONTRACT_EXPIRED` / `CONTRACT_FILE_BUDGET_EXCEEDED` (`validators/harness/contract_gate.py`), `CAPABILITY_DENIED` semantics as structured `PermissionError` denials with `explain_denial` introspection, `RELEASE_BLOCKED` via `D024ViolationError` |
| Command execution hardening (all roles) | Hard interpreter denylist with no contract override (`validators/kernel/interpreter_denylist.py`), secret-scrubbed resource-limited sandbox (`validators/kernel/sandbox.py`), staged-scratch execution only |
| Selective context (§9, INV-009) | Curated per-phase tool palettes + `request_tools` expansion (`validators/kernel/providers/gateway.py`, `get_curated_tools_for_phase`), token-budgeted `assemble_context` (`validators/knowledge/api.py`), capped and injection-sanitized retrieval (`validators/harness/retrieval.py`) |
| Write boundary per path (§12) | Per-WO contract allow/deny rules evaluated post-execution against *observed* files (`validators/harness/contract_gate.py`, `verify_post_execution`), in-turn via the kernel contract evaluator and `TaskOwnership`; the role-domain defaults in §12 are the architect's authoring responsibility, expressed as contract scopes |
| Human authority above all agents | Plan approval gate (`AWAITING_APPROVAL`, `supervisor.py`), operator resume (`_prepare_run_for_resume`), D025 receipt lock, skill promotion governor (`validators/skill/governor.py`) |

### Verification

- `tests/test_agent_matrix_enforcement.py` — machine-checks the policy catalog rows above and the verdict-channel gate behavior (`write_file` + `apply_patch`).
- `tests/test_lifecycle_supervisor.py`, `tests/test_qa_defect_routing.py` — lifecycle, QA verdict, and defect-routing authority flows.
- `tests/test_d024_qa_gate.py`, `tests/test_tool_hardening.py` — release gate and tool-boundary hardening.