# StackMind

> **Compiler-Backed Multi-Agent Engineering Runtime**

StackMind is a governed runtime for AI-assisted software engineering.

It combines **multi-agent orchestration, deterministic code intelligence, project knowledge, governed execution, verification, and Git-based delivery** into one runtime.

Instead of allowing an LLM to freely inspect and modify a repository, StackMind gives agents explicit identities, contracts, work orders, execution boundaries, verification gates, persistent runtime state, and access to compiled project knowledge.

```text
                              STACKMIND
                                  │
             ┌────────────────────┼────────────────────┐
             │                    │                    │
             ▼                    ▼                    ▼
       RUNTIME &              KNOWLEDGE             HARNESS &
       GOVERNANCE             COMPILER              VERIFICATION
             │                    │                    │
             ▼                    ▼                    ▼
       Supervisor             Code Graph           Agent Runner
       Sessions               Search/RAG           Contracts
       Work Orders            Analysis             Sandbox
       Contracts              Embeddings           Tool Gates
       State                  Learning             Verification
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  ▼
                           PROJECT WORKSPACE
```

## Why StackMind?

A conventional coding agent typically operates as:

```text
User → LLM → Tools → Repository
```

StackMind introduces a governed execution layer:

```text
User
 │
 ▼
TUI / CLI
 │
 ▼
Daemon
 │
 ▼
Lifecycle Supervisor
 │
 ├── Planning
 ├── Approval
 ├── Work-order authoring
 ├── Dependency-aware dispatch
 ├── Agent execution
 ├── QA / verification
 ├── Integration review
 └── GitOps
 │
 ▼
Completed Product
```

Alongside execution, StackMind continuously provides structured knowledge about the project:

```text
Source Code
    │
    ▼
Knowledge Compiler
    │
    ├── Symbols
    ├── Dependencies
    ├── Calls
    ├── Data Flow
    ├── Runtime Evidence
    ├── Domain Models
    └── Project Structure
    │
    ▼
Knowledge Store
    │
    ▼
Knowledge API / RAG
    │
    ▼
Bounded Agent Context
```

The result is an engineering runtime where **agents perform work, while the runtime controls how that work is planned, authorized, executed, verified, and integrated**.

---

## Core Capabilities

### Multi-Agent Runtime

StackMind supports a structured agent hierarchy with specialized responsibilities.

A typical workflow can contain:

| Role                   | Responsibility                                    |
| ---------------------- | ------------------------------------------------- |
| **CEO / User**         | Product goals, priorities and human decisions     |
| **Architecture Agent** | Architecture, planning, work orders and contracts |
| **Backend Agent**      | Backend implementation                            |
| **Frontend Agent**     | Frontend implementation                           |
| **QA Agent**           | Verification, review and quality gates            |
| **GitOps Agent**       | Release integration and Git operations            |

Agent responsibilities are backed by runtime contracts rather than relying exclusively on prompt instructions.

---

### Lifecycle Supervisor

The Supervisor is the deterministic orchestration layer.

A product run progresses through explicit lifecycle phases:

```text
INIT
 │
 ▼
PLANNING
 │
 ▼
AWAITING_APPROVAL
 │
 ▼
AUTHORING
 │
 ▼
DISPATCHING
 │
 ▼
EXECUTING
 │
 ▼
INTEGRATION_REVIEW
 │
 ▼
PRODUCT_READY
 │
 ▼
GITOPS
 │
 ▼
COMPLETE
```

Failure and blocking states are handled explicitly:

```text
                    ┌─────────────► FAILED
                    │
EXECUTING ──────────┤
                    │
                    └─────────────► BLOCKED
```

The Supervisor is responsible for deterministic state transitions. It does not replace the agents.

The agents perform the work; the Supervisor determines **when and under what conditions the next stage may execute**.

The repository tests explicitly cover planning, human approval, dependency-aware dispatch, retry loops, blocked work, persistence, integration review and GitOps completion.

---

## Governed Agent Execution

Every agent operation can be constrained by a contract defining:

* Agent identity
* Role
* Work order
* Allowed paths
* Denied paths
* File-count budgets
* Token budgets
* Runtime boundaries

Example:

```yaml
schema_version: 1

agent_id: codex
work_order: WO-001

identity:
  role: backend
  reports_to: claude

scope:
  allow:
    - src/backend/**
    - tests/backend/**
  deny:
    - .git/**
    - .sync/runtime/**
    - .sync/knowledge/**
    - .env

  write: read-write

budget:
  max_files_touched: 20
  max_tokens: 50000
```

The repository's agent contract templates demonstrate this model, including explicit allow/deny scopes and execution budgets.

The objective is **fail-closed authority**:

```text
Agent Request
     │
     ▼
Contract Check
     │
 ┌───┴────┐
 │        │
ALLOW    DENY
 │        │
 ▼        ▼
Execute  Block
```

---

# Knowledge Compiler

StackMind contains a project knowledge subsystem that converts source code into a persistent, queryable representation.

```text
Repository
    │
    ▼
Parse
    │
    ▼
Resolve
    │
    ▼
Intermediate Representation
    │
    ├───────────────┐
    ▼               ▼
Symbol Registry   Domain Compilers
    │               │
    └───────┬───────┘
            ▼
      Knowledge Store
            │
       ┌────┼────┐
       ▼    ▼    ▼
     Search Graph Metrics
       │    │
       └────┼────┘
            ▼
       Knowledge API
```

The repository contains dedicated knowledge components for:

* Parsing
* Symbol resolution
* Intermediate representation
* Incremental compilation
* Storage
* Registry management
* Projections
* Search
* Embeddings
* Runtime analysis
* Data-flow analysis
* Knowledge validation

The knowledge subsystem is located under `validators/knowledge/`.

---

## Code Intelligence

StackMind's knowledge layer can represent more than simple file contents.

It includes components for:

### Static relationships

```text
Function A
    │
    ├── CALLS ──────► Function B
    ├── DEPENDS_ON ─► Module C
    └── DEFINES ────► Symbol D
```

### Runtime evidence

Controlled runtime analysis can capture observed execution relationships.

```text
A ──CALLS──► B
     │
     └── evidence:
         provider: runtime-tracer
         evidence_type: runtime-observed
         confidence: 1.0
```

### Data flow

StackMind can represent bounded data-flow relationships:

```text
request.args
      │
      ▼
validate_input()
      │
      ▼
db.execute()

FLOWS_TO
FLOWS_TO
```

These capabilities are implemented under the knowledge analysis subsystem.

---

# Knowledge API & RAG

Agents should not have to repeatedly rediscover a project by scanning the repository.

StackMind provides a knowledge retrieval layer:

```text
                    Agent Query
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
       Lexical       Semantic        Graph
       Search        Search         Traversal
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                    Reranking
                        │
                        ▼
                Evidence Normalization
                        │
                        ▼
                 Contract Scope Gate
                        │
                        ▼
                Ranked Context Bundle
```

The retrieval layer can combine:

* Lexical search
* Semantic search
* Graph traversal
* Runtime evidence
* Data-flow relationships
* Contract boundaries
* Evidence normalization

The intended result is **bounded, relevant project context instead of unrestricted repository dumping**.

---

## Knowledge Query Primitives

The architecture defines primitives such as:

```text
lookup(node_id)
filter(kind, path, ...)
traverse(start, edge_kind, direction, depth)
semantic(query, top_k)
assemble_context(task, token_budget)
```

These allow an agent to ask questions about the project without rebuilding the project's understanding from scratch.

---

# Harness Runtime

The Harness controls how an individual agent executes work.

```text
Work Order
    │
    ▼
Contract Validation
    │
    ▼
Dependency Checks
    │
    ▼
Context Assembly
    │
    ▼
LLM / Agent
    │
    ▼
Tool Execution
    │
    ▼
Output Validation
    │
    ▼
Diff / Scope Validation
    │
    ▼
Verification
    │
    ▼
Persist Result
```

The repository contains dedicated harness components for:

* Contract gates
* Dependency gates
* Authoring gates
* Retrieval
* Execution
* Snapshots
* Verification
* Security boundaries

The harness therefore acts as the controlled execution boundary between an LLM and the project workspace.

---

# Work Orders

Work is represented as persistent work orders rather than informal prompts.

A work-order lifecycle can be represented as:

```text
ACTIVE
  │
  ├──────────► BLOCKED
  │
  ▼
COMPLETED
```

Work orders can contain:

* Assigned agents
* Dependencies
* Deliverables
* Execution state
* Review state
* Retry information
* Completion information

Dependency-aware dispatch prevents a dependent work order from running before its prerequisites are complete.

For example:

```text
WO-001
Scaffolding
    │
    ▼
WO-002
Backend
    │
    ▼
WO-003
Frontend
    │
    ▼
WO-004
QA / Integration
```

---

# Verification

Verification is a first-class subsystem.

```text
                  Agent Work
                      │
                      ▼
                 Deliverable
                      │
                      ▼
                 Verification
                      │
             ┌────────┴────────┐
             ▼                 ▼
          APPROVED        NEEDS_CHANGES
             │                 │
             ▼                 ▼
         Continue            Retry
                               │
                               ▼
                          Retry Limit
                               │
                               ▼
                             FAILED
```

The repository contains dedicated verification components for:

* Structural verification
* Replay
* Canary verification
* Verification pipelines
* Verification models

The Supervisor also validates actual deliverables on disk rather than relying only on an operation status value. The test suite explicitly covers this behavior.

---

# Human Approval

StackMind keeps human approval as an explicit lifecycle boundary.

```text
Planning
   │
   ▼
Plan Proposed
   │
   ▼
AWAITING_APPROVAL
   │
   ├──── APPROVE ────► AUTHORING
   │
   └──── REJECT ─────► PLANNING
```

A rejection can feed human feedback back into the planning cycle.

This prevents the runtime from treating an LLM-generated plan as automatically authorized execution.

---

# GitOps

Successful work eventually reaches a GitOps stage.

```text
Workers
   │
   ▼
QA
   │
   ▼
Integration Review
   │
   ▼
Product Ready
   │
   ▼
GitOps
   │
   ▼
Release Commit
```

Release commits can carry provenance information such as:

```text
Work-Order: WO-004
Released-By: local-llm
Approved-By: gemma
Architect: claude
Target-Work-Orders: WO-001, WO-002, WO-003
```

The test suite verifies creation of Git commits containing this provenance trail.

---

# Runtime Architecture

StackMind separates the reusable runtime engine from the project-specific runtime state.

```text
┌─────────────────────────────────────────────────────────────┐
│                     STACKMIND ENGINE                        │
│                                                             │
│  CLI          Schemas          Knowledge Compiler            │
│  Harness      Runtime Kernel   Verification                 │
│  Provider     TUI              Protocols                    │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                  PROJECT RUNTIME INSTANCE                   │
│                                                             │
│  .sync/                                                     │
│   ├── agents/                                               │
│   ├── inbox/                                                │
│   ├── outbox/                                               │
│   ├── reviews/                                              │
│   ├── runtime/                                              │
│   ├── state/                                                │
│   ├── work-orders/                                         │
│   └── knowledge/                                            │
└─────────────────────────────────────────────────────────────┘
```

The repository provides templates for these runtime structures, including agent inboxes/outboxes, reviews, runtime state, work orders and boot configuration.

---

# `.sync/`

A governed project instance uses `.sync/` as its runtime state and coordination area.

A typical structure is:

```text
.sync/
├── agents/
├── decisions/
├── escalations/
├── inbox/
│   ├── CEO/
│   ├── claude/
│   ├── codex/
│   ├── gemini/
│   ├── gemma/
│   └── local-llm/
├── outbox/
├── releases/
├── reviews/
├── runtime/
│   ├── boot/
│   ├── drafts/
│   └── receipts/
├── standup/
├── state/
└── work-orders/
    ├── ACTIVE/
    ├── BLOCKED/
    ├── COMPLETED/
    └── TEMPLATES/
```

The filesystem therefore acts as a durable coordination surface between the runtime components and agents.

---

# CLI

StackMind exposes a CLI for managing the runtime.

Examples include:

```bash
# Initialize a governed project
stackmind init ./my-project --name "My App"

# Validate runtime state
stackmind validate ./my-project

# Diagnose runtime configuration
stackmind doctor ./my-project

# Build project knowledge
stackmind graph build -p ./my-project

# Update knowledge incrementally
stackmind graph update -p ./my-project

# Query project knowledge
stackmind graph query "AuthService.login" -p ./my-project

# Find callers
stackmind graph callers "AuthService.login" -p ./my-project

# Analyze impact
stackmind graph impact "AuthService.login" --depth 3 -p ./my-project

# Assemble bounded context
stackmind graph context \
  "How does authentication work?" \
  --token-budget 2000 \
  -p ./my-project

# Run a governed harness cycle
stackmind harness run-once
```

The CLI source is organized under `cli/`, with dedicated modules for graph operations, contracts, daemon management, harness execution, validation, migration, learning, skills, TUI and other runtime operations.

---

# TUI

StackMind includes a terminal user interface connected to the runtime.

The TUI is organized into components for:

```text
cli/tui/
├── app.py
├── chat.py
├── diff.py
├── events.py
├── governance.py
├── keyboard.py
├── landing.py
├── layout.py
├── runtime_panel.py
└── state.py
```

The TUI is intended to expose runtime state rather than act as a separate orchestration engine.

Conceptually:

```text
             ┌──────────────────────┐
             │       StackMind TUI   │
             ├──────────────────────┤
             │ Session               │
             │ Runtime               │
             │ Activity              │
             │ Work Orders            │
             │ Contract Boundary      │
             │ Diff                   │
             │ Verification           │
             │ Human Approval         │
             └───────────┬───────────┘
                         │
                         ▼
                  Runtime Daemon
```

This keeps the TUI as a client of the runtime rather than allowing UI state to become the source of truth.

---

# Learning and Skills

StackMind also contains separate learning and skill subsystems:

```text
validators/learning/
├── cluster.py
├── distiller.py
├── miner.py
└── normalizer.py

validators/skill/
├── decay.py
├── governor.py
├── models.py
├── retriever.py
└── store.py
```

The distinction is intentional:

```text
Knowledge
    │
    │ "What exists in the project?"
    ▼
Knowledge Compiler

Experience
    │
    │ "What patterns have been learned?"
    ▼
Learning

Reusable capability
    │
    │ "What procedure/capability can be retrieved?"
    ▼
Skills
```

This creates a foundation for agents to become increasingly context-aware without mixing project facts with learned procedural knowledge.

---

# Security and Boundaries

StackMind contains explicit runtime security boundaries.

Relevant kernel components include:

```text
validators/kernel/
├── boundary.py
├── contract.py
├── evidence.py
├── identity.py
├── interpreter_denylist.py
├── operations.py
├── sandbox.py
├── security.py
├── session.py
├── tools.py
└── workspace.py
```

These boundaries are complemented by:

* Contract enforcement
* Scope validation
* Workspace isolation
* Sandbox controls
* Tool controls
* Dependency gates
* Destructive-operation safeguards
* Human approval
* Verification

The objective is to make agent authority a runtime property rather than merely a prompt convention.

---

# Repository Structure

```text
stackmind/
│
├── cli/
│   ├── main.py
│   ├── graph.py
│   ├── daemon.py
│   ├── harness.py
│   ├── contract.py
│   ├── validate.py
│   ├── migrate.py
│   ├── learn.py
│   ├── skill.py
│   └── tui/
│
├── validators/
│   │
│   ├── kernel/
│   │   ├── daemon/
│   │   ├── providers/
│   │   ├── multi/
│   │   ├── mcp/
│   │   ├── tui/
│   │   ├── contract.py
│   │   ├── sandbox.py
│   │   ├── security.py
│   │   └── workspace.py
│   │
│   ├── knowledge/
│   │   ├── analysis/
│   │   ├── compiler/
│   │   ├── embedding/
│   │   ├── projections/
│   │   ├── api.py
│   │   ├── registry.py
│   │   ├── storage.py
│   │   └── writer.py
│   │
│   ├── harness/
│   │   ├── runner.py
│   │   ├── backend.py
│   │   ├── contract_gate.py
│   │   ├── dependency_gate.py
│   │   ├── retrieval.py
│   │   └── snapshot.py
│   │
│   ├── learning/
│   ├── skill/
│   └── verification/
│
├── schemas/
│   ├── knowledge/
│   ├── boot.schema.json
│   ├── contract.schema.json
│   ├── experience.schema.json
│   ├── harness-output.schema.json
│   ├── runtime-version.schema.json
│   ├── tree.schema.json
│   └── work-order.schema.json
│
├── templates/
│   └── sync/
│
├── migrations/
│
├── tests/
│
├── docs/
│   ├── rfcs/
│   ├── runtime-truth/
│   ├── diagrams/
│   └── archive/
│
├── AGENTS.md
├── PLAN.md
├── STACKMIND.md
├── pyproject.toml
└── README.md
```

The repository contains dedicated tests for daemon behavior, the execution kernel, provider gateway, multi-agent runtime, knowledge API, compiler components, security, TUI behavior, unified RAG, verification and lifecycle supervision.

---

# Installation

StackMind is a Python package.

Current package metadata specifies:

```text
Package: stackmind
Version: 3.6.0
Python: >=3.10
License: MIT
Build backend: Hatchling
```

Install from source:

```bash
git clone https://github.com/Abhishek3670/stackmind.git
cd stackmind

pip install -e .
```

For development:

```bash
pip install -e ".[dev]"
```

---

# Development

Run the test suite:

```bash
pytest
```

The repository contains extensive tests covering:

* CLI integration
* Runtime lifecycle
* Daemon protocol
* Multi-agent execution
* Harness execution
* Provider gateways
* Knowledge compilation
* Knowledge API
* Storage
* Learning
* Skills
* Verification
* Security
* TUI
* RAG
* Runtime state
* Work-order execution

---

# Design Principles

StackMind is built around several core principles.

### 1. Deterministic orchestration

The runtime, rather than an LLM, controls lifecycle transitions.

### 2. Explicit authority

Agents receive explicit contracts defining where and how they may operate.

### 3. Durable state

Important runtime state is persisted instead of existing only inside an LLM conversation.

### 4. Project knowledge as infrastructure

Project understanding is compiled and persisted so agents do not need to repeatedly reconstruct it.

### 5. Evidence-backed intelligence

Knowledge can contain static, runtime-observed and data-flow evidence.

### 6. Human-controlled authorization

Important lifecycle decisions can pause for explicit human approval.

### 7. Verification before integration

Agent output is not automatically considered complete because an LLM reported success.

### 8. Separation of planning and implementation

Architecture/planning responsibilities are separated from worker implementation.

### 9. Governed execution

The execution environment enforces contracts, workspace boundaries and safety gates.

### 10. Provenance

Important operations can preserve information about the work order, agents, reviewers and release operations.

---

# End-to-End Example

Suppose the user enters:

```text
Create a login page with a backend login endpoint.
```

StackMind can conceptually process that goal as:

```text
USER
 │
 ▼
TUI / CLI
 │
 ▼
Supervisor
 │
 ▼
Architecture Agent
 │
 │ creates plan
 ▼
AWAITING_APPROVAL
 │
 │ human approves
 ▼
Authoring
 │
 │ creates work orders
 ├───────────────────┐
 ▼                   ▼
Backend             Frontend
Agent               Agent
 │                   │
 ▼                   ▼
Implementation      Implementation
 │                   │
 └─────────┬─────────┘
           ▼
          QA
           │
      ┌────┴────┐
      ▼         ▼
  APPROVED   NEEDS_CHANGES
      │         │
      │         └────► Worker retry
      ▼
Integration Review
      │
      ▼
Product Ready
      │
      ▼
GitOps
      │
      ▼
Release Commit
      │
      ▼
COMPLETE
```

At the same time, the Knowledge Compiler and Knowledge API can provide agents with relevant project structure, symbols, dependencies, call relationships and other indexed context.

---

# Architecture at a Glance

```text
                         ┌──────────────────┐
                         │       USER       │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │     TUI / CLI    │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │      DAEMON      │
                         └────────┬─────────┘
                                  │
                                  ▼
                    ┌───────────────────────────┐
                    │   LIFECYCLE SUPERVISOR    │
                    └─────────────┬─────────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
                    ▼             ▼             ▼
                PLANNING      WORK ORDERS    APPROVAL
                    │             │             │
                    └─────────────┼─────────────┘
                                  ▼
                         ┌──────────────────┐
                         │     HARNESS      │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
                CONTRACT       CONTEXT        TOOLS
                    │             │             │
                    │             ▼             │
                    │      KNOWLEDGE API       │
                    │             │             │
                    │      ┌──────┴──────┐      │
                    │      ▼             ▼      │
                    │   GRAPH/RAG    PROJECT     │
                    │                FACTS       │
                    │             │             │
                    └─────────────┼─────────────┘
                                  ▼
                              AGENT
                                  │
                                  ▼
                            DELIVERABLE
                                  │
                                  ▼
                           VERIFICATION
                                  │
                       ┌──────────┴──────────┐
                       ▼                     ▼
                    APPROVED             RETRY
                       │
                       ▼
                INTEGRATION REVIEW
                       │
                       ▼
                    GITOPS
                       │
                       ▼
                   COMPLETE
```

---

# Project Documentation

The repository contains a broader architecture and engineering documentation set.

Important areas include:

```text
docs/
├── architecture.md
├── STACKMIND_ARCHITECTURE.md
├── StackMind_Agent_Runtime.md
├── STACKMIND_AGENT_RUNTIME_ROADMAP.md
├── protocols.md
├── cli-reference.md
├── getting-started.md
├── migration-guide.md
├── rfcs/
├── runtime-truth/
└── diagrams/
```

The RFC series currently covers:

```text
RFC-001  Symbol Identity and Registry
RFC-002  Storage and Projection
RFC-003  Knowledge Compiler
RFC-004  Knowledge API
RFC-005  Background Intelligence
RFC-006  Harness Runtime
```

---

# Status

StackMind is an actively developed engineering runtime with substantial runtime, knowledge, harness, verification and TUI infrastructure.

The repository currently contains:

* Multi-agent runtime infrastructure
* Lifecycle supervision
* Persistent work orders
* Contract enforcement
* Knowledge compilation
* Code-graph intelligence
* Retrieval / RAG infrastructure
* Agent execution harness
* Verification pipeline
* Learning and skill subsystems
* TUI runtime integration
* Extensive automated testing

The source tree should be treated as the implementation authority; architectural documents in `docs/` describe individual phases, designs and historical decisions.

---

# License

MIT
