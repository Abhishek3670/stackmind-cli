# RFC-003: Knowledge Compiler

**Status:** Draft — for decision
**Scope:** SKC compiler — pipeline, Intermediate Representation, parser, semantic analysis, event ingestion, incremental compilation
**Depends on:** RFC-001 (Identity), RFC-002 (Storage & Projection) — accepted
**Blocks:** Implementation Plan Phase 2 (Frontend) & Phase 5 (Incremental); precedes RFC-004 (Knowledge API)

---

## 0. Why this is the third decision

RFC-001 fixed *identity*, RFC-002 fixed *where data lives*. RFC-003 fixes *how authoritative state becomes the Intermediate Representation that RFC-002 persists and Phase 4 projects*. Two things this RFC must own that the earlier drafts left dangling:

1. **The IR** — RFC-002 says "projectors are `IR → artifact`" but never defines IR. It's defined here.
2. **Compiler determinism** — the Verdict named it a top technical risk. This RFC makes determinism an *enforced property of resolution*, not an aspiration (§8).

Framing commitments (from the Directive):
- **Stages 1–4 are deterministic. No LLM participates** (Principle 2, 9). LLMs live only in Stage 5 (RFC-005).
- Compute happens **outside** the write-lock; only the write batch is locked (RFC-002).

---

## 1. The pipeline

```
Stage 1  Ingestion         (source, .sync, git, runtime events)  → change set
Stage 2  Symbol Resolution (registry lookup, rename detection)   → stable IDs
Stage 3  Semantic Analysis (imports, calls, deps, placeholders)  → IR
─────────────────────── deterministic boundary ───────────────────────
Stage 4  Projection        (IR → node store + T2 projections)    [RFC-002]
Stage 5  Background Intel   (summaries, embeddings)               [RFC-005, async]
```

Stages 1–3 produce the IR. Stage 4 serializes it (RFC-002 §1: IR persisted = the T1 node store) and derives T2 projections. Stage 5 runs asynchronously and only ever *adds* `ai` fields — it can be absent, disabled, or killed with no effect on Stages 1–4.

---

## 2. Intermediate Representation (the contract)

The IR is the **in-memory canonical form** of the project at one revision. It is the single interface between "understanding the code" (frontend) and "writing artifacts" (storage/projection).

```
IR {
  revision_inputs: { git_commit, sync_ref, compiler_version, schema_version, registry_version }
  symbols: [ Symbol ]        # each with a stable NodeID from the registry
  edges:   [ Edge ]          # (source_id, relation, target_id | null, resolution)
  diagnostics: [ Diagnostic ]# parse/resolve failures — non-fatal, per-symbol
}

Symbol { id, type, deterministic{ qualname, path, signature, location, content_hash, owner } }
Edge   { source_id, relation, target_id?, resolution: RESOLVED|EXTERNAL|UNRESOLVED, confidence }
```

Properties (all enforced):
- **Transient.** The IR is not itself a tracked file; its persisted form *is* the RFC-002 node store. The determinism gate applies to `IR → disk`.
- **Pure function** of `(source, .sync, git, registry, compiler_version)`. Nothing else may influence it (§8).
- **Contains no wall-clock / environment data** (RFC-002 §3, §9). `revision_inputs` carries provenance; the timestamp is stamped only into the `revisions/` record at Stage 4.
- **`ai` is absent from IR.** The deterministic pipeline never produces `ai`; Stage 5 fills it directly into T1 node files post-hoc, keyed by NodeID.

---

## 3. Stage 1 — Ingestion & event model

Two ingestion paths converge on one IR/registry/storage transaction:

- **Code path.** Source file changes → parse (Stage 2/3). Sources of "changed":
  - **Full build:** enumerate all repo Python files.
  - **Incremental:** file-system events (`watchdog`) and/or git post-commit diff (§7).
- **Runtime path.** `.sync/` lifecycle events (WorkOrder / Review / Decision / Issue create/update/close). These upsert **runtime nodes** directly — *no AST* — and their edges (`ASSIGNED_TO`, `REVIEWS`, `IMPLEMENTS`, `HAS_ISSUE`) per RFC-002 §4. Runtime events are consumed **only after StackMind Layer-3 authority validation has already passed** (Directive P6: agents never write knowledge; runtime emits validated events, compiler projects them).

**Event record:** `{ id, kind, target, source_of_truth_ref }`. **Idempotency:** dedup by content hash (code) or event id (runtime); re-applying an event yields identical IR. **Ordering:** events processed in a deterministic order (sorted by path/id), so batch interleaving cannot change output.

**Watcher exclusion (C4-adjacent, RFC-002):** the watcher ignores `.sync/knowledge/` and `.sync/cache/` — the compiler's own writes must never re-trigger ingestion.

---

## 4. Stage 2 — Symbol Resolution & rename detection (C4)

RFC-001 owns identity *semantics*; RFC-003 owns *when resolution runs and how rename detection is scheduled* — the item `SMPOC_final` asserted but never scheduled.

Per changed file:
1. Parse → set of `(kind, path, qualname, content_hash, owner, signature)` for each definition.
2. **Registry lookup** by `birth_key = path:qualname`. Hit → reuse NodeID. Miss → candidate for mint **or** rename.
3. **Rename/move detection** (runs here, in Stage 2):
   - Compute, within the changeset, the set of **vanished** birth-keys (in previous build, gone now) and **appeared** birth-keys (new now).
   - Match vanished↔appeared on `(kind + content_hash + owner + signature)`. `content_hash` is the strong signal — a pure rename leaves the body identical.
   - **Confident match** → rebind: keep the existing NodeID, append the new key as an alias and update `current` in the registry (RFC-001 §2.2). Edges untouched (immutable IDs).
   - **No confident match** → the vanished symbol is marked `deleted`; the appeared symbol **mints** a fresh birth-hash ID.
4. **Graceful degradation (RFC-001 §2.4):** a missed rename is lossy (dangling inbound edges become `UNRESOLVED`, provenance resets) — never corrupt (distinct birth-keys cannot fuse).

**Acceptance for this stage lives in Plan Phase 5** (rename → ID stable; move → ID stable), *not* Phase 1 — detection is built here, so its test belongs here (C4).

---

## 5. Stage 3 — Semantic Analysis & call resolution

Extracts edges. The hard, determinism-critical stage.

**Parser:** **LibCST** primary (full-fidelity, precise locations), `ast` acceptable fallback. Visitor with a qualname stack extracts modules/classes/functions/methods, signatures, locations, and a per-symbol `content_hash`. Parsing is pure — reads source text, no environment.

**Edge extraction & resolution tiers:**
| Tier | Case | Result |
|---|---|---|
| **A** | Intra-file / explicitly-imported name resolvable statically | `RESOLVED` → target NodeID via registry |
| **B** | Cross-file in-repo, needs inference | **Jedi** `goto/infer` → in-repo definition → NodeID |
| **C** | stdlib / third-party / dynamic / unresolvable | `EXTERNAL` (named, not linked) or `UNRESOLVED` (placeholder edge + `unresolved[]`, RFC-002 §4) |

**Import-alias handling:** track `import json as js` → `js.dump()` resolves to `json.dump`; `from x import y` maps `y` to its module.

**The determinism rule that makes Jedi safe (§8):** resolution scope is **the repository only**. Calls into stdlib or third-party packages are recorded as `EXTERNAL` and **never resolved to a NodeID**. This is non-negotiable — it is what makes the graph independent of what happens to be installed in site-packages.

---

## 6. Two-pass within a build (forward references)

A single build (full or incremental batch) runs resolution in two passes to eliminate ordering dependence and the forward-reference validation gap raised earlier:

1. **Pass 1 — Register.** Parse all in-scope files; register/resolve every symbol → NodeID in the registry. No edges yet.
2. **Pass 2 — Resolve edges.** With every in-scope symbol now ID-bearing, resolve edges. A target defined in a not-yet-seen file in Pass 1 is available by Pass 2, so `CALLS`/`IMPORTS` to later-parsed files resolve cleanly instead of dangling.

This makes Layer-5's "edge targets exist or are flagged" (RFC-002 §11) satisfiable without resolution-order luck.

---

## 7. Incremental compilation (Phase 5 core)

Goal: "one file changed → only affected symbols rebuild." Correctness hinges on computing the **affected set** precisely.

1. **Dirty detection.** Compare current file `content_hash` to the stored node `content_hash` (RFC-002 §3). Unchanged → skip entirely.
2. **Affected set = A ∪ B:**
   - **A — direct:** all symbols defined in dirty files (re-parse, re-resolve their outgoing edges).
   - **B — inbound-on-structural-change:** if a symbol in a dirty file is **deleted or renamed**, every node with an edge *targeting* it must be revisited (its edge becomes `UNRESOLVED` or rebinds). These are found via the **reverse index** (RFC-002 §6) — this is why the reverse index is a **build-time input, not merely a query artifact.**
   - A pure body edit with stable signature and ID touches only A. A signature change may widen B (callers' resolution may shift).
3. **Batch scheduler.** Rapid events are debounced/coalesced so a file saved five times in a second compiles once.
4. **Reverse-index update.** After the batch, incrementally patch the reverse index (remove the changed nodes' old inbound contributions, add new) rather than full rebuild.

---

## 8. Determinism contract (enforced, not hoped)

Given identical `(repo state, .sync state, registry, compiler_version)`, the IR — and therefore the T1 node store — is **byte-identical**. Enforced by:

- **Purity:** Stages 1–3 read only source, `.sync/`, git, and the registry. No network, no wall-clock, no RNG, no PID, no absolute paths (RFC-002 §9).
- **Environment independence (the sharp rule):** resolution never consults installed packages; external symbols are `EXTERNAL`, never linked (§5). Two developers with different virtualenvs produce the same graph.
- **Pinned toolchain:** LibCST and Jedi versions are pinned and recorded in `compiler_version`; a toolchain bump is a new `compiler_version` and an expected (audited) graph change, not silent drift.
- **Order independence:** parallel parsing permitted, but IR symbol/edge collections are sorted before serialization (RFC-002 canonical form).
- **CI gate:** the RFC-002 "compile-twice-diff" job is the enforcement mechanism; a determinism regression fails the build.

---

## 9. Write-lock, atomicity & partial failure

- **Compute unlocked.** Parsing, resolution, rename detection, projection computation all happen without the lock.
- **Locked write batch.** Acquire `.sync/` lock → write changed registry shards + node files + one new `revisions/` record → release. Reverse-index/other T2 writes may happen inside or after the lock (they're cache).
- **Atomic per file:** write to temp + rename, so a crash never leaves a half-written node/registry file.
- **Per-file transactional, best-effort:** a parse error in file F produces a `Diagnostic`, **keeps F's previous nodes**, and does not abort the batch. One bad file never corrupts the graph or blocks the other files. Diagnostics surface in `graph stats` / `validate`.

---

## 10. Handoff to Stage 5 (async boundary)

After a batch commits, changed NodeIDs are enqueued for enrichment (RFC-005). The boundary is strict:
- Deterministic pipeline writes **T0 (registry) + T1 (nodes, revisions)**.
- Enricher writes **T2 (embeddings)** and the **`ai` block** of T1 nodes, keyed by NodeID, tagged with confidence, **never touching `deterministic`**.
- Queries never block on enrichment; a node with an empty `ai` block is fully valid (RFC-002 §3).

This is also where the earlier "don't send raw code to the LLM" question lives — it is an RFC-005 policy decision, out of scope here. Stages 1–4 never transmit source anywhere.

---

## 11. Module & CLI surface (plan-level, not code)

Proposed modules under `validators/knowledge/compiler/`: `ingest`, `parse`, `resolve`, `ir`, `incremental`. CLI mapping (registered under `@cli.group('graph')`, RFC-002/plan):
- `graph build` → full pipeline, Stages 1–4, all files.
- `graph update` → incremental one-shot (§7).
- `graph watch` → daemon driving `update` on events (§3, §7).
- `graph stats` → node/edge/unresolved/diagnostic counts + last revision.

None of the 7 existing commands are modified.

---

## 12. Alternatives considered

- **Resolve against the installed environment (link stdlib/third-party to nodes).** Rejected: makes the graph depend on site-packages → non-deterministic across machines (§8). External calls are named, not linked.
- **Single-pass resolution.** Rejected: forward references to later-parsed files dangle; two-pass (§6) removes the ordering dependence.
- **LLM-assisted call resolution in the deterministic path.** Rejected outright (Directive P2/P9). Any AI inference is Stage 5, tagged, advisory.
- **Rename detection in Stage 1 (pre-parse).** Rejected: detection needs `content_hash`+signature, which require parsing — it belongs in Stage 2 after parse.
- **Abort batch on any parse error.** Rejected: one malformed file would freeze the whole graph; per-file transactional best-effort (§9) is safer.

---

## 13. Open questions (defer)

- **Method-moves-between-classes** — rename of `owner` vs. move? (RFC-001 §6 open item; heuristic tuning in Phase 5.)
- **Simultaneous rename+move** (path *and* qualname change at once) — `content_hash` match should carry it; needs a Phase 5 test fixture.
- **Jedi cost ceiling** on large repos — may need a resolution cache (T2) keyed by `(content_hash, compiler_version)`; measure in Phase 5 before adding.
- **Cross-language** — Python-only per plan §9; the parser interface should be pluggable but no second language is built.
- **Git-hook vs. watchdog** as the canonical incremental trigger — support both; decide default in Phase 5.

---

## 14. Recommendation

Adopt the **five-stage pipeline** with the deterministic boundary after Stage 3, the **IR contract** (§2), **rename detection scheduled in Stage 2** (§4, discharging C4), **repo-scoped environment-independent resolution** (§5, §8, discharging the Verdict's determinism risk), **two-pass build** (§6), and **reverse-index-as-build-input** for incremental correctness (§7).

With RFC-001/002/003 accepted, the Phase 0 gate closes and Plan Phases 1–5 rest on settled ground. Proceed to **RFC-004 (Knowledge API)** — query, traversal, context assembly, projection selection — which consumes the IR-derived projections this RFC produces.
