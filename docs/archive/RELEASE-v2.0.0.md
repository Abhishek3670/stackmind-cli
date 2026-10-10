# StackMind v2.0.0 — Release Notes

**Release Date:** 2026-07-17
**Previous Version:** v1.2.0
**Status:** ALL PHASES COMPLETE

---

## Release Summary
StackMind v2.0.0 transforms the runtime from a governance-only platform into a compiler-backed engineering runtime. It adds two new pillars: a Knowledge Compiler (Pillar 2) that deterministically compiles source code into a persistent, queryable knowledge graph, and a Harness Runtime (Pillar 3) that provides governed agent execution with Knowledge API integration.

## What's New

### Pillar 2: Knowledge Compiler (Phases 1-7)
Deterministic source-to-IR compilation. Agents start from shared compiled understanding.

| Phase | WO | Title | Tests |
|-------|----|-------|-------|
| 1 | WO-001 | Symbol Registry & Identity Foundation | 257 |
| 2 | WO-002 | Compiler Frontend (Deterministic) | 266 |
| 3 | WO-003 | Storage Layer (Sharded JSON) | 270 |
| 4 | WO-004 | Projection Engine | 275 |
| 5 | WO-005 | Incremental Compiler + Rename Detection | 283 |
| 6 | WO-006 | Background Intelligence (Async LLM) | 291 |
| 7 | WO-007 | Knowledge API + Agent Integration | 298 |

### Pillar 3: Harness Runtime (Phase 8)
| Phase | WO | Title | Tests |
|-------|----|-------|-------|
| 8 | WO-008 | Harness Runtime | 304 |

### Pillar 1: Runtime Governance (pre-existing, v1.2.0)
CLI, 4-layer validation, write-lock, work orders, agent protocols — unchanged.

## Architecture Overview
```text
Source Code
    │
    ▼
┌─────────────────────────────────┐
│  Knowledge Compiler (Pillar 2)  │
│  parse → resolve → IR → store   │
│  → project → enrich → query     │
└─────────────────────────────────┘
    │                         │
    ▼                         ▼
┌──────────────┐    ┌──────────────────────┐
│  Projections │    │  Knowledge API       │
│  (reverse    │    │  (lookup, filter,    │
│   index,     │    │   traverse, search,  │
│   search,    │    │   assemble_context)  │
│   metrics)   │    └──────────────────────┘
└──────────────┘              │
                              ▼
                    ┌──────────────────────┐
                    │  Harness (Pillar 3)  │
                    │  Agent Runner        │
                    │  poll → context →    │
                    │  LLM → verify →     │
                    │  write-back          │
                    └──────────────────────┘
                              │
                              ▼
                    ┌──────────────────────┐
                    │  Governance (Pillar 1)│
                    │  lock → validate →   │
                    │  commit              │
                    └──────────────────────┘
```

## New CLI Commands

### `stackmind graph` (10 subcommands)
| Command | Description |
|---------|-------------|
| graph build | Compile entire project into knowledge store |
| graph update | Incremental update (changed files only) |
| graph watch | File watcher daemon |
| graph query | Symbol lookup/filter/search |
| graph callers | Direct callers of a symbol |
| graph impact | Transitive impact analysis |
| graph explain | Callers + callees of a symbol |
| graph context | Agent context assembly |
| graph stats | Node/edge/revision counts |
| graph versions | List graph revisions |

### `stackmind harness` (1 subcommand)
| Command | Description |
|---------|-------------|
| harness run-once | Governed agent execution (single run) |

## Key Features

### Deterministic Compilation
- Same source → byte-identical IR output
- No RNG, no wall-clock, no absolute paths
- CI gate: compile-twice-diff

### Birth-Hash Symbol Identity
- NodeID = TYPE-first16(SHA256(path:qualname))
- Assigned once, frozen forever
- Survives renames/moves via alias detection

### Three-Tier Storage
- T0 (Canonical): Symbol Registry — git-tracked, never derived
- T1 (Committed Derived): Nodes, revisions — deterministic, git-tracked
- T2 (Cache): Projections, embeddings — gitignored, rebuildable

### Incremental Compilation
- Content-hash dirty detection
- Affected-set computation via reverse index
- File watcher with self-trigger prevention

### Knowledge API
- Four query primitives: lookup, filter, traversal, search
- Context assembly with token budgets
- Provenance envelope on every response

### Background Intelligence
- Async LLM summaries and embeddings
- Never touches deterministic state
- Cost-capped, failure-tolerant

### Harness Runtime
- Governed execution: poll → context → LLM → verify → write-back
- Schema validation before every write
- Observability (tokens, latency, cost)

## Validation Upgrade
- Added Layer 5: Knowledge validation
- Checks: unique NodeIDs, edge target existence, revision chain integrity, canonical form

## Metrics
| Metric | Value |
|--------|-------|
| Work Orders | 8 completed |
| Total Tests | 304 passing |
| Coverage | 83% |
| Decisions | D-001 (RFC acceptance), D-002 (coupling resolution), D-003 (Phase 8 gate) |
| Build Status | GREEN |
| Lint | CLEAN |
| Validation | PASS |

## Team Performance
| Agent | Role | Sessions | WOs Delivered |
|-------|------|----------|---------------|
| Claude | Senior Architect | 2 | — (coordination) |
| Codex | Backend Lead | 8 | WO-001 through WO-008 |
| Gemma | QA Lead | 7+ | All reviews |
| Local-LLM | GitOps Lead | 7 | All commits |
| Gemini | Frontend Lead | 0 | — (not needed) |

## Code Review Findings

### What's Good
- Real architectural progression
- Serious test investment
- Clean module boundaries
- Well documented

### Known Issues & Technical Debt

#### High Priority
1. Write lock TOCTOU race — check-then-act without atomic primitive
2. Harness orchestration scope — does bookkeeping/coordination, not source edits
3. Verify validates bookkeeping tree, not code changes

#### Medium Priority
4. Weak secret redaction in enricher
5. Path traversal possible in enricher._read_excerpt
6. Prompt injection surface via analyzed source
7. --force lock steal is unauthenticated

#### Low Priority
8. Full-tree copy on every harness run
9. Non-atomic FileMove
10. Crude token estimation (len/4)
11. Silent failure swallowing in git operations

### Release Hygiene Items
- pyproject.toml version needs bump from 1.2.0 to 2.0.0
- Committed \_\_pycache\_\_/*.pyc files need removal
- Coverage gate (--cov) was removed from pyproject.toml
- docs/ folder not updated for v2.0.0 features

### Recommended Fixes Before Production
1. Replace acquire_lock with atomic primitive (O_EXCL or flock) + TTL
2. Clarify harness scope in documentation
3. Harden enrichment egress path (containment, redaction, privacy default)

## Quick Start
```bash
# Build knowledge graph for any Python project
stackmind graph build -p /path/to/project

# Query symbols
stackmind graph query "function_name" -p /path/to/project

# Who calls this symbol?
stackmind graph callers "symbol_name" -p /path/to/project

# Impact analysis
stackmind graph impact "symbol_name" --depth 3 -p /path/to/project

# Agent context assembly
stackmind graph context "question" --token-budget 2000 -p /path/to/project

# Run harness (single execution)
stackmind harness run-once
```

## Breaking Changes
- New validation Layer 5 (Knowledge) — existing runtimes must migrate
- `graph_version` field added to boot snapshots
- Migration path: v1.2.0 → v2.0.0 (non-reversible major version)

## Dependencies Added
- `libcst` — Full-fidelity Python AST parser
- `jedi` — Cross-file symbol resolution

## Upgrade Path
```bash
pip install --upgrade stackmind
stackmind migrate --check ./my-project
stackmind migrate ./my-project
stackmind validate ./my-project
```

## References
- PLANv2.md — Full implementation plan
- STACKMIND.md — Complete project documentation
- docs/rfcs/ — Technical specifications (RFC-001 through RFC-006)
- docs/SMPOC/ — Original research and POC designs
- report.md — Detailed code review findings

---

**StackMind v2.0.0: From governance runtime to compiler-backed engineering runtime. Shipped.**
