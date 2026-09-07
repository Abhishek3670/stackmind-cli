# StackMind v3 Plan
## From Knowledge Graph to Agent Governance Platform

**Version:** 3.0 Draft (revised)
**Status:** Strategic Roadmap
**Focus:** Python Ecosystem First

---

# Vision

StackMind is not a code knowledge graph.

The graph is not the product. The graph is the substrate.

The product is this: **AI agents lose track of who they are, what they were asked to do, and what they are allowed to touch — because they reconstruct their understanding of a codebase from scratch, in an unbounded way, every session.** StackMind compiles the codebase once into a deterministic model, and uses that model to give every agent a **stateful contract**: a bounded identity, a bounded task, and a bounded scope, enforced at query time — not requested by convention and hoped for.

Knowledge graph tools already exist and are well-funded and widely adopted (CodeGraph, CodeGraphContext). They compete on token efficiency and retrieval speed for a single agent. StackMind does not compete there. StackMind competes on **governance**: making it structurally impossible for an agent to act outside its contract, regardless of how it was prompted, manipulated, or confused mid-session.

---

# Mission

Compile Python repositories into deterministic engineering knowledge, and use that knowledge to **bind every agent to a contract it cannot exceed.**

Every compile should answer:

- What exists?
- How is everything connected?
- What changed?
- What will break?
- Who depends on this?
- **Which agent is allowed to touch this, and under what contract?**

---

# Design Principles

## Deterministic

Same repository. Same graph. Same IDs. Always.

---

## Incremental

Never rebuild the world. Compile only what changed.

---

## Persistent

Knowledge survives across sessions. Developers. AI. CI. Everyone consumes the same compiled knowledge.

---

## Explainable

Every relationship must be traceable back to source code. No hallucinated edges.

---

## Enforceable (new)

A scope boundary that can be silently ignored is not a boundary — it is a suggestion.

Every contract must be checked at the point of access (the Knowledge API), not at the point of prompting. If a query, edit, or context bundle falls outside an agent's contract, the API refuses it. It does not warn and proceed.

Fail closed, not open.

---

## Language Agnostic Core

Python is the first frontend. The compiler architecture should support future frontends without redesigning the core.

---

# Core Architecture

```
Repository
      │
      ▼
Compiler Frontend
      │
      ▼
Intermediate Representation (IR)
      │
      ▼
Registry
      │
      ▼
Knowledge Graph
      │
      ▼
Projections
      │
      ▼
Knowledge API
      │
      ▼
Contract Layer   ← new: every call is checked against an agent's contract here
      │
      ▼
Harness Runtime
```

Only the compiler frontend changes per language. Everything else remains identical.

The Contract Layer is the actual product. Everything above it exists to make the Contract Layer possible.

---

# Phase 0 — Foundation (Built)

Goal: a correct, deterministic compiler. Not the product — the substrate the product needs.

**Status: mostly done. Freeze scope here. Do not keep expanding breadth before Phase 1 exists.**

## 0.1 Python Compiler

- modules, classes, functions, imports, call graph
- symbol resolution, incremental compilation, rename detection, graph stability

## 0.2 Framework Compilers (already built — stop adding new ones for now)

- FastAPI: routes, dependencies, middleware, auth, request/response models
- Django: URL routing, views, models, signals, middleware
- SQLAlchemy: ORM models, foreign keys, relationships
- Alembic: migration history, schema evolution
- Celery: tasks, queues, scheduling
- Pydantic: validation graph

These exist to give contracts something precise to bind to (e.g. "this agent may touch the `billing` router and its callers, nothing else"). They are infrastructure for Phase 1, not a standalone pitch. Do not add an 8th framework compiler until Phase 1 exists and needs it.

---

# Phase 1 — Agent Governance Runtime (was Phase 4 — now the priority)

Goal: turn the compiled graph into an enforced boundary around every agent.

This is the differentiated part. Build this next, before Phase 2 or 3.

## 1.1 The Contract

Every agent session starts with a contract, not a prompt. A contract is a structured, inspectable artifact — not a convention the agent is asked to follow.

A contract specifies:

- **Identity** — which agent, which role, which work order it is executing
- **Scope** — a boundary expressed in graph terms: allowed nodes, allowed subgraphs (e.g. module + callers to depth N), read-only vs. read-write edges
- **Budget** — token budget, time budget, max edits, max files touched
- **Task** — the specific work order this session exists to complete

Example shape:

```yaml
contract:
  agent_id: agent-codex-07
  work_order: WO-142
  identity:
    role: implementer
    reports_to: senior-architect
  scope:
    allow:
      - module: billing.invoices
        depth: 2          # billing.invoices + its direct callers/callees
      - module: billing.tests
        depth: 0
    deny:
      - module: auth.*
      - module: infra.migrations
    write: read-write      # vs read-only elsewhere
  budget:
    max_files_touched: 6
    max_tokens: 40000
    expires_at: 2026-07-22T18:00:00Z
```

## 1.2 Enforcement at the Knowledge API

- `graph context`, `graph query`, `graph callers`, `graph impact`, and any edit operation all take a contract as an argument.
- A request for a node outside the contract's `allow` scope, or inside `deny`, is rejected — not filtered after the fact, not logged-and-allowed.
- A budget overrun ends the session, not the task.

## 1.3 Boot Snapshot Tied to Contract

- At session start, the agent receives: its identity, its task, its scope — derived from the graph and the contract, not restated from memory each time.
- Mid-session drift (an agent "forgetting" its scope over a long session) is irrelevant, because the boundary is enforced structurally at every call, not held in the agent's own context.

## 1.4 Pipeline

```
Task
  ↓
Contract issued (scope derived from graph)
  ↓
Knowledge API (contract-checked)
  ↓
Planner
  ↓
Validator   — checks plan against contract before execution
  ↓
Executor
  ↓
Reviewer    — checks diff against contract after execution
```

Agents never parse the repository directly. They consume compiled knowledge, filtered through their contract.

## 1.5 Capabilities

- scoped context (already partially built via `graph context`)
- impact awareness (already built via `graph impact`)
- contract validation before execution
- fail-closed enforcement on out-of-scope access
- deterministic, auditable "why was this denied" explanations

## 1.6 Grounding: your own incident history

Treat the source-code-loss incident as the founding case study, not an embarrassment to bury. It is evidence for exactly the failure mode this phase exists to prevent: an operation executed outside its intended boundary, with no structural check to stop it. Write it up as "what governance would have caught."

---

# Phase 2 — Repository Intelligence (deferred)

Only pursue after Phase 1's contract layer is real and enforced, and only for artifacts a contract actually needs to reason about scope (e.g. CI/CD mapping so a contract can know what a change will trigger).

- Documentation (README, RFCs, ADRs) → linked to services
- Configuration (pyproject.toml, .env, Docker Compose)
- CI/CD (GitHub Actions, GitLab CI) → workflow → test → deploy mapping
- Testing (pytest, coverage) → symbol → test coverage mapping

---

# Phase 3 — Engineering Intelligence (deferred)

Insight generation on top of the graph. Valuable, but not differentiated — CodeGraph/CGC-adjacent tools already do circular-import detection, dead-code detection, and impact analysis. Only build this once Phase 1 is solid; treat it as a nice-to-have layer on the contract system, not a separate pitch.

- Architecture analysis (circular imports, dead modules, oversized classes)
- Impact analysis (affected endpoints, tests, tasks, docs)
- Repository health reports

---

# Phase 4 — Multi-language Expansion (last)

Only after Python is production-ready **and** the contract/governance layer is proven on Python. Multi-language breadth without a proven governance layer is just rebuilding CodeGraphContext slower and alone.

- TypeScript, JavaScript, Go, Java, Rust, C#
- Compiler core (and Contract Layer) remain unchanged; only the frontend changes

---

# Frontend Interface

Unchanged from prior plan — each language implements a `CompilerFrontend` (discover, parse, resolve, emit_ir). Deprioritized until Phase 1 is proven.

```python
class CompilerFrontend:
    language: str
    def discover_files(...): ...
    def parse(...): ...
    def resolve(...): ...
    def emit_ir(...): ...
```

---

# Example Queries

Governance (new, primary):

```
graph contract show WO-142
graph contract validate WO-142 --op "edit billing/invoices.py"
graph explain-denial WO-142 --node auth.session
graph scope agent-codex-07
```

Existing (substrate, still useful):

```
graph architecture
graph routes
graph endpoint /users
graph impact User
graph model Order
graph tests PaymentService
graph coverage OrderRepository
graph env / graph docker / graph workflows
graph health
graph explain CheckoutFlow
```

---

# Success Criteria

StackMind should answer, without opening source files or trusting an agent's self-report:

✓ Which agent is allowed to touch this file, right now, under which contract?

✓ Was this edit inside or outside the agent's granted scope?

✓ What would this agent need permission for that it doesn't have?

✓ Which endpoints use this service? What breaks if this model changes?

✓ Which services have no tests? Which migrations are missing?

✓ Which modules violate architecture?

The first three are the differentiated ones. The rest are table stakes shared with every other code-graph tool.

---

# Long-Term Vision

StackMind evolves through three stages.

## Stage 1 — Knowledge Graph
Understand source code. (Built.)

## Stage 2 — Governed Agent Runtime
Bind every agent to an enforced, stateful contract derived from that graph. (Next.)

## Stage 3 — Engineering Intelligence Platform
Deterministic engineering knowledge and governance for developers, AI agents, IDEs, CI/CD, and architecture review. (Later — and only credible once Stage 2 is real.)

---

# Final Goal

> Build the system that governs how AI agents are allowed to change software — not just the system that helps them understand it faster.

A repository should not just be a compiled, queryable model of what exists. It should be the enforced boundary that determines what any given agent, at any given moment, is actually allowed to do to it.