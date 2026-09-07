# StackMind Knowledge Compiler (SKC) — Implementation Plan

**Status:** Draft v1.0 (planning artifact — no implementation authorized)
**Source directive:** *SKC Engineering Directive & Implementation Plan*
**Prerequisite:** RFC-001 (Identity & Symbol Registry) — see `docs/rfcs/RFC-001-symbol-identity-and-registry.md`
**Nature:** This document is a **plan**. It defines *what will be built, in what order, and how each step is proven done*. It authorizes no code; Phase 0 must complete first.

---

## 1. Purpose

Translate the SKC directive's principles and Phase 0–7 roadmap into an executable plan bound to StackMind's **actual** codebase. Every phase below names the real files it creates or touches, its upstream dependency, and a binary acceptance gate.

The directive's thesis is the plan's spine: **StackMind becomes a compiler-backed engineering runtime.** The Knowledge Compiler transforms authoritative state (Source + `.sync/` + Git) into *derived, rebuildable projections*. The Project Knowledge Graph is **one projection**, never the system, never a second source of truth.

---

## 2. Baseline (what exists today)

| Area | Current state | Relevance to SKC |
|---|---|---|
| CLI | `cli/main.py` — one `click` group, 7 commands (`init`, `validate`, `doctor`, `migrate`, `shutdown`, `promote`, `lock`) | SKC adds a new `@cli.group('graph')`; existing commands untouched |
| Schemas | `schemas/*.schema.json` (7 files), Draft7 | SKC adds `schemas/knowledge/*` on the same validator path |
| Validation | `cli/validate.py` — 4 layers (Schema→Structure→Protocol→Boot) | SKC adds **Layer 5: Knowledge** |
| Locking | `cli/lock.py` — single `.sync/` write-lock | SKC reuses it; `.sync/knowledge/` joins that namespace |
| Storage | Git-tracked YAML under `.sync/` | Authoritative (canonical). SKC output is derived, additive |
| `validators/` | Empty package (`__init__` only) | Candidate home for extracted compiler/validation modules |

**No graph, AST, registry, or knowledge code exists.** All SKC work is additive.

---

## 3. Invariants (carried from the directive — every phase must hold these)

These are acceptance pre-conditions for *all* phases, not a one-time check:

1. Runtime (Source + `.sync/` + Git) is canonical; knowledge is derived.
2. Deterministic stages produce byte-identical output for identical (repo, `.sync/`, compiler version, registry).
3. Symbol **identity** is permanent; name/file/namespace may change, ID may not.
4. Symbol Registry is canonical: git-tracked, write-locked, Layer-5 validated, migrated. **Not cache.**
5. Embeddings are cache: disposable, rebuildable, gitignored.
6. Agents never write knowledge files; runtime emits events, compiler writes projections.
7. Deleting `.sync/knowledge/` never loses state — the compiler rebuilds everything.
8. Lock is held **only around writes**; never during parse, resolve, or enrichment.
9. LLMs never participate in deterministic compilation (Stages 1–4).

**Four corrections folded in from the `SMPOC_final.md` review (binding on this plan):**
- **C1 — Registry is sharded**, not a monolithic `symbols.json` (avoids merge chokepoint on the highest-churn canonical file).
- **C2 — Reverse index is a first-class projection** (outgoing-edges-in-node makes "who calls X?" O(all nodes) otherwise).
- **C3 — NodeID uses 64-bit truncation (16 hex)**, not 32-bit — collision-safe at 10k+ nodes.
- **C4 — Rename detection is scheduled explicitly** and its acceptance test lives in the phase that implements it — never asserted in a phase that omits the task.

---

## 4. RFC gating (Phase 0 — must complete before any implementation)

| RFC | Title | Status | Blocks |
|---|---|---|---|
| RFC-001 | Identity & Symbol Registry | Draft written | Phase 1 |
| RFC-002 | Storage & Projection (node layout, reverse index, sharding, revisioning) | **To write** | Phase 3, 4 |
| RFC-003 | Knowledge Compiler (parser, semantic analyzer, IR, event pipeline, incremental) | **To write** | Phase 2, 5 |
| RFC-004 | Knowledge API (query, traversal, context assembly, projection selection) | **To write** | Phase 7 |
| RFC-005 | Background Intelligence (embeddings, summaries, confidence, privacy, retry) | **To write** | Phase 6 |

**Phase 0 exit gate:** RFC-001, RFC-002, RFC-003 accepted. (RFC-004/005 may lag but must precede Phases 7/6 respectively.) **No implementation PR merges before this gate.**

RFC-002 must inherit RFC-001's constraint: *edges are pairs of permanent IDs → rename/move touches zero edge data → edges cannot be keyed by any mutable field.*

---

## 5. Phased execution plan

Each phase: **Objective · Depends on · Workstreams · Files · Acceptance gate.** Phases are sequential except where noted; the critical path is §6.

---

### Phase 1 — Identity Foundation
- **Objective:** Permanent, deterministic symbol identity backed by a canonical, sharded registry.
- **Depends on:** RFC-001 accepted.
- **Workstreams:**
  1. Sharded Symbol Registry store (C1) — per-symbol files or NodeID-prefix buckets under `.sync/knowledge/registry/`.
  2. Birth-key minting: `NodeID = TYPE "-" first16(SHA256(birth_key))` (C3), computed once, frozen.
  3. Registry lifecycle: load / create-if-missing / upsert / mark-obsolete.
  4. Registry schema + Layer-5 invariants (unique IDs, ID = birth-hash of earliest history key, one live node per record).
  5. Registry migration hook (registry participates in `stackmind migrate`).
- **Files:** `schemas/knowledge/symbol.schema.json`; registry module (proposed `validators/knowledge/registry.py`); Layer-5 stub wired into `cli/validate.py`.
- **Acceptance gate:**
  - Same (repo) compiled twice → identical NodeIDs.
  - Registry survives deletion of all *derived* projections and is itself reconstructable only from history (never silently re-minted from current keys).
  - `stackmind validate` Layer-5 passes on a seeded registry; fails on an injected duplicate ID.
  - **Rename-stability is NOT asserted here** (C4) — deferred to Phase 5 where detection is built.

---

### Phase 2 — Compiler Frontend (deterministic)
- **Objective:** Deterministic Source → IR frontend. No LLMs.
- **Depends on:** RFC-003; Phase 1 (needs stable IDs).
- **Workstreams:**
  1. LibCST parser: modules, classes, functions/methods; qualified-name stack.
  2. Symbol Table build; every definition → `(path, qualname)` → registry lookup → NodeID.
  3. Jedi-backed resolver for intra-repo imports/calls; **placeholder edges** for unresolved (`target: null` + flag).
  4. IR definition (in-memory canonical form feeding all projections).
- **Files:** parser + resolver + IR modules (proposed `validators/knowledge/compiler/`).
- **Acceptance gate (determinism):** `compile(repo) == compile(repo)` — IR is byte-identical across repeated runs and across a fresh checkout at the same commit. Unresolved calls are recorded, never dropped.

---

### Phase 3 — Storage Layer
- **Objective:** Persist IR as sharded, schema-validated, deterministic JSON.
- **Depends on:** RFC-002; Phase 2.
- **Workstreams:**
  1. `.sync/knowledge/` layout: `registry/`, `nodes/<Type>/`, `revisions/`.
  2. Node files carry deterministic fields + **outgoing** edges inline + empty `ai` block.
  3. Graph revision stamping (git SHA, compiler version, schema version, counts).
  4. Node/edge/revision schemas + Layer-5 checks (no dup IDs; edge targets exist or flagged unresolved; revision references valid SHA).
- **Files:** `schemas/knowledge/{node,edge,graph-revision}.schema.json`; storage/writer module; Layer-5 expansion in `cli/validate.py`.
- **Acceptance gate:** IR → disk is deterministic (identical bytes for identical IR); a one-symbol change rewrites only that node's file (+registry shard); `stackmind validate` catches an injected dangling edge.

---

### Phase 4 — Projection Engine
- **Objective:** Generate all derived projections from IR. Prove rebuildability.
- **Depends on:** RFC-002; Phase 3.
- **Workstreams:**
  1. Project Knowledge Graph projection.
  2. **Reverse Index projection (C2)** — `called_by` / inbound edges, as a derived (gitignored) cache.
  3. Search Index projection.
  4. Metrics projection.
- **Files:** projection modules; `.gitignore` entries for derived caches; `stackmind graph build`/`stats`/`versions` wired under `@cli.group('graph')` in `cli/main.py` (new `cli/graph.py`).
- **Acceptance gate (directive Definition-of-Done #6):** delete all projections → recompile → **identical** projections. "Who calls X?" answered from the reverse index without scanning all nodes.

---

### Phase 5 — Incremental Compiler
- **Objective:** Rebuild only affected symbols; implement rename/move detection (C4).
- **Depends on:** Phase 4; RFC-003 (incremental section).
- **Workstreams:**
  1. File watcher (`stackmind graph watch`) via `watchdog`; **excludes `.sync/knowledge/` and cache dirs** (no self-trigger loop).
  2. Content-hash dirty detection; per-file re-parse.
  3. Symbol dependency graph → affected-set computation.
  4. **Rename/move detection**: match vanished birth-keys to appeared keys (kind + body-hash + owner + signature) → rebind existing NodeID as alias; low-confidence → delete+create.
  5. Batch scheduler; lock **acquired per write-batch, released before next parse**.
- **Files:** watcher + incremental + detection modules; `stackmind graph update` command.
- **Acceptance gate:**
  - One file changed → only affected symbols recompile (assert via revision diff).
  - **Rename test (moved from Phase 1 per C4):** rename a function → NodeID unchanged, alias recorded, inbound edges intact.
  - **Move test:** relocate a file → NodeIDs unchanged, paths updated.
  - Watch daemon writing to `.sync/knowledge/` does not re-trigger itself.

---

### Phase 6 — Background Intelligence (async, non-blocking)
- **Objective:** Enrich nodes with summaries/embeddings without touching the deterministic path.
- **Depends on:** RFC-005; Phase 4 (needs stable nodes).
- **Workstreams:**
  1. Enrichment queue over new/changed node IDs.
  2. LLM summaries → `ai.summary` + `confidence`; **all AI output tagged, never overwrites deterministic fields.**
  3. Embeddings → gitignored cache; reuse by content hash.
  4. Retry/rate-limit/failure-tolerant worker; missing `ai` never blocks queries.
  5. **Resolve the v3 privacy contradiction** (per RFC-005): explicit, config-gated policy on whether source bodies leave the machine.
- **Files:** enrichment worker; `.sync/cache/embeddings/` (gitignored); config surface.
- **Acceptance gate:** compilation and queries succeed with the enricher fully disabled; enabling it only *adds* `ai` fields; killing it mid-run leaves deterministic state intact.

---

### Phase 7 — Agent Integration
- **Objective:** Agents consume the Knowledge API before reading repo files.
- **Depends on:** RFC-004; Phase 4 (query surface).
- **Workstreams:**
  1. `stackmind graph query` (by type/name, traversal, semantic) + Knowledge API.
  2. Context-assembly endpoint (projection selection).
  3. Agent-protocol update (`AGENTS.md`, `docs/protocols.md`): "query Knowledge API before parsing files."
  4. Rollout: query-only first (advisory), then default path.
- **Files:** `cli/graph.py` query commands; API module; docs.
- **Acceptance gate (Definition-of-Done #4):** Planner/Reviewer/Coder resolve a cross-file question via the API with no repo file reads; results carry provenance (revision + confidence).

---

## 6. Dependency graph & critical path

```
RFC-001 ─┬─► Phase 1 ─► Phase 2 ─► Phase 3 ─► Phase 4 ─► Phase 5 ─► Phase 7
RFC-002 ─┘                                   │            
RFC-003 ─────────────────────────────────────┘            
RFC-004 ──────────────────────────────────────────────────────────► Phase 7
RFC-005 ───────────────────────────────────────► Phase 6 (parallel to 5, after 4)
```

- **Critical path:** RFC-001/002/003 → P1 → P2 → P3 → P4 → P5 → P7.
- **Parallelizable:** Phase 6 runs alongside Phase 5 once Phase 4 lands. RFC-004/005 can be authored during Phases 1–4.

---

## 7. Cross-cutting requirements (apply to every phase)

- **Validation:** each phase ships its schema + Layer-5 checks with the feature — no feature merges without its validator.
- **Locking:** every writer wraps writes in the `.sync/` lock context; hold time measured and asserted "writes-only" in tests.
- **Determinism CI:** a "compile twice, diff output" job gates Phases 2–4; any nondeterminism fails the build.
- **Testing ladder:** unit (parser/registry/detection) → integration (build → mutate → update on a sample repo) → schema (good/bad fixtures) → rebuildability (delete projections → recompile → diff).
- **Provenance:** every projection artifact traceable to a graph revision (git SHA + compiler version).

---

## 8. Acceptance ladder → Definition of Done

| Directive DoD | Proven by |
|---|---|
| 1. Deterministic (commit + `.sync`) → same IR | Phase 2 gate + determinism CI |
| 2. All projections generated from IR | Phase 4 gate |
| 3. PKG fully rebuildable | Phase 4 gate (delete → recompile → identical) |
| 4. Agents use Knowledge API, not file parsing | Phase 7 gate |
| 5. Runtime remains single source of truth | Invariant §3.1 + §3.6, enforced every phase |
| 6. Projections deletable & regenerable losslessly | Phase 4 + Phase 1 (registry survives) |

---

## 9. Explicitly out of scope (this plan)

- Neo4j / Memgraph / managed vector DB (JSON + gitignored vector cache only).
- Multi-language support (Python-first).
- A query *language* beyond the RFC-004 surface.
- Any write-back from knowledge into authoritative layers (forbidden by Principle 1).
- Time/staffing estimates — this plan sequences work and defines gates; scheduling is a separate exercise once RFCs are accepted.

---

## 10. Immediate next action

Author **RFC-002** and **RFC-003** (RFC-001 exists). Nothing in Phases 1–7 is authorized until the Phase 0 gate (§4) closes. The single highest-leverage decision remaining is RFC-002's edge/reverse-index layout — it must carry RFC-001's "edges are immutable-ID pairs" constraint forward, or Phase 3 storage will be rebuilt later.
