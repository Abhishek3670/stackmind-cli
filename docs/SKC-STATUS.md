# SKC Planning — Status & Handoff Brief

**Purpose:** Single resume point for the StackMind Knowledge Compiler (SKC) planning effort. Feed this to a later session to restore full context without re-reading everything. **Planning/docs only — no code has been or should be written yet.**

**Last updated:** 2026-07-13
**Phase:** Pre-implementation (Phase 0 — Architecture Freeze)

---

## 1. One-paragraph context

StackMind today is a YAML-based multi-agent runtime coordinator (`.sync/` state, `cli/` commands, 4-layer validation, write-lock). The SKC effort evolves it into a **compiler-backed engineering runtime**: a deterministic compiler that transforms authoritative state (Source + `.sync/` + Git) into **derived, rebuildable knowledge projections** (Project Knowledge Graph, search, reverse index, metrics, embeddings). The graph is **one projection, never a second source of truth**. Verdict = **GO (9.3, conditional on Phase 0)**.

---

## 2. Document inventory

| Doc | Author | Role | Status |
|---|---|---|---|
| `docs/SMPOC/STACKMIND_FINAL_VERDICT.md` | user | Executive GO decision, 3 pillars, build order | reference |
| `docs/SMPOC/SMPOC.md` | user | Working POC (evolved v1→v3 in-place) | superseded by RFCs |
| `docs/SMPOC/SMPOC_final.md` | user | Consolidated POC + Week-1 plan | superseded by RFCs (had 4 fixed defects, see §5) |
| `docs/SMPOC/SKC-IMPLEMENTATION-PLAN.md` | this effort | Phased, gated execution plan bound to real repo | **current** |
| `docs/rfcs/RFC-001-symbol-identity-and-registry.md` | this effort | Identity model | **written, unaccepted** |
| `docs/rfcs/RFC-002-storage-and-projection.md` | this effort | On-disk layout, edges, projections | **written, unaccepted** |
| `docs/rfcs/RFC-003-knowledge-compiler.md` | this effort | Pipeline, IR, incremental | **written, unaccepted** |
| `docs/SMPOC/harness_engineering.md` | user | Harness/Agent-Runner research v2 | superseded |
| `docs/SMPOC/harness_engineering_required.md` | user | Harness/Agent-Runner spec v3 | **validated — seed for RFC-006** |
| `docs/SMPOC/Master_Knowledge_Architecture_Report.md` | user | Condensed synthesis of POC artifacts | superseded — predates final RFCs; contains stale claims (global `CALLS.json`, "Harness Runtime = web search", revision-as-snapshot); do not use as reference |
| `docs/HARNESS-ENGINEERING-EVALUATION.md` | this effort | v3 validation report (code-verified claims, defects D1–D4) | current |
| `docs/SKC-STATUS.md` | this effort | This handoff brief | current |

All docs are **untracked / uncommitted** in git.

---

## 3. RFC status (6 total — ALL WRITTEN)

| RFC | Title | Status | Gates |
|---|---|---|---|
| 001 | Identity & Symbol Registry | ✅ written, ⏳ unaccepted | Phase 1 |
| 002 | Storage & Projection | ✅ written, ⏳ unaccepted | Phase 3, 4 |
| 003 | Knowledge Compiler | ✅ written, ⏳ unaccepted | Phase 2, 5 |
| 004 | Knowledge API | ✅ written, ⏳ unaccepted | Phase 7 |
| 005 | Background Intelligence | ✅ written, ⏳ unaccepted | Phase 6 |
| 006 | Harness Runtime (Agent Runner) | ✅ written (D1–D4 fixes folded in), ⏳ unaccepted | Harness build (post-Phase-7, per Verdict gating) |

**Phase 0 gate = RFC-001/002/003 accepted.** They are *written* but acceptance is only asserted in prose — **no real sign-off recorded**, so the gate is technically still open.

---

## 4. Key decisions locked by the written RFCs (cheat-sheet)

Carry these forward — later docs/implementation must not contradict them:

- **Identity (RFC-001):** `NodeID = TYPE-first16(SHA256(birth_key))`, birth_key = `path:qualname` **at first sighting, frozen**. Hash = minting function (runs once), *not* identity function. Registry pins ID across rename/move. Two identity axes: (1) deterministic across builds, (2) stable across renames — birth-hash + registry buys both.
- **Registry is canonical (T0):** git-tracked, write-locked, validated, migrated. **Not cache.** Sharded (never one `symbols.json`).
- **Storage tiers (RFC-002):** T0 canonical (source, `.sync/`, registry) · T1 committed-derived (nodes, revisions — byte-deterministic) · T2 cache (reverse index, search, metrics, embeddings — gitignored, rebuildable).
- **Edges (RFC-002):** stored in the **source node's** file as **permanent-NodeID** references. Rename/move rewrites **zero** edges. No global `CALLS.json`. Unresolved = placeholder, never dropped.
- **Reverse index (RFC-002):** T2 projection; also a **build-time input** for incremental affected-set computation.
- **Determinism (RFC-002 §9, RFC-003 §8):** no wall-clock/RNG/PID/abs-path in T0/T1; time lives only in `revisions/`. Enforced by **compile-twice-diff CI gate**.
- **Compiler (RFC-003):** 5 stages; deterministic boundary after Stage 3; **no LLM in Stages 1–4**. IR = transient in-memory canonical form; its persisted form = the T1 node store.
- **Resolution is repo-scoped & environment-independent:** stdlib/third-party calls = `EXTERNAL`, never linked to a NodeID → graph identical across machines/virtualenvs.
- **Rename detection:** scheduled in **Stage 2**, tested in **Phase 5** (not Phase 1). Missed rename = lossy, never corrupt.
- **Directive invariants:** runtime canonical · knowledge derived · embeddings cache · agents never write knowledge · every projection rebuildable · lock only around writes · event-driven.

---

## 5. Defects already caught & folded in (don't re-litigate)

The 4 `SMPOC_final.md` defects are **fixed** in the RFCs/plan (labeled C1–C4):
- **C1** registry sharded (not monolithic `symbols.json`).
- **C2** reverse index is a first-class projection (else "who calls X?" is O(all nodes)).
- **C3** 64-bit / 16-hex NodeID (not 32-bit `[0:8]` — collision-prone).
- **C4** rename-stability tested in the phase that builds detection (Phase 5), fixing `SMPOC_final`'s Week-1 self-contradiction.

---

## 6. Open threads (parked — pick up on resume)

1. **Record RFC acceptance** for 001–006 to actually close the Phase 0 gate (currently prose-only).
2. ~~Draft RFC-004~~ ✅ done — `docs/rfcs/RFC-004-knowledge-api.md` (read-only 4-primitive surface, provenance envelope, staleness flags, budgeted context assembly).
3. ~~Draft RFC-005~~ ✅ done — `docs/rfcs/RFC-005-background-intelligence.md` (async enricher, `enriched_hash` staleness, **privacy resolved: 4-mode policy, default `full` for this repo**, cost caps).
4. **Verdict edits suggested:** reframe 9.3 as **conditional on Phase 0**; **gate Scheduler/TUI** behind proven Knowledge-API adoption rather than listing as certainties.
5. ~~Harness Runtime pillar undefined~~ ✅ done — `docs/rfcs/RFC-006-harness-runtime.md` formalizes it (Agent Runner as Worker agent; D1–D4 fixes folded in; terminology ruled: "Harness Runtime" = execution layer, "WebSearchRunner" = retrieval tool).
6. **Housekeeping:** decide whether the docset ever gets committed (currently gitignored by design); optionally add `docs/rfcs/README.md` index.

---

## 7. Critical path (from the Plan)

```
RFC-001/002/003 accepted → P1 Identity → P2 Frontend → P3 Storage → P4 Projection → P5 Incremental → P7 Agents
                                                                              └ P6 Background Intel (parallel, after P4)
RFC-004 → gates P7   RFC-005 → gates P6   RFC-006 → gates Harness build (post-P7 per Verdict)
```

All six RFCs are now **written**. The only thing standing before Phase 1 implementation is **recording acceptance** (thread #1) — and lifting the planning freeze.

---

## 8. How to resume

Feed this file first. Then, depending on intent:
- **Continue planning** → draft RFC-004 or RFC-005 (§6).
- **Close Phase 0** → record acceptance on RFC-001/002/003.
- **Start building** (only after Phase 0) → open `docs/SKC-IMPLEMENTATION-PLAN.md`, begin Phase 1; obey §4 invariants.

**Do not** write implementation code until the Phase 0 gate is explicitly closed.
