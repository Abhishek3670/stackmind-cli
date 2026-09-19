# RFC-002: Storage & Projection

**Status:** Draft — for decision
**Scope:** StackMind Knowledge Compiler (SKC) — on-disk layout, edge model, projections, revisioning
**Depends on:** RFC-001 (Identity & Symbol Registry) — accepted
**Blocks:** Implementation Plan Phase 3 (Storage), Phase 4 (Projection); informs RFC-003 (incremental)

---

## 0. Why this is the second decision

RFC-001 fixed *identity*. RFC-002 fixes *where identity-bearing data lives and what is derived from it*. Get this wrong and you either (a) git-track things that shouldn't be tracked and drown in merge noise, or (b) treat rebuildable state as canonical and rebuild the storage layer later. The plan already flags the highest-leverage open item as "RFC-002's edge/reverse-index layout must carry RFC-001's immutable-ID constraint" — this RFC settles exactly that.

Two hard commitments frame everything below:
- **Edges are pairs of permanent NodeIDs** (RFC-001). Therefore edge data is *never* keyed, sharded, or filed by a mutable attribute (path, name). Rename/move touches zero edges.
- **The registry is the only canonical knowledge artifact** (RFC-001 §2.3). Everything else in `.sync/knowledge/` is derived and rebuildable.

---

## 1. Storage classification — three tiers

Every file under `.sync/knowledge/` belongs to exactly one tier. This is the core model of the RFC; all later sections are consequences.

| Tier | Meaning | Git | Deterministic | Recovery if lost |
|---|---|---|---|---|
| **T0 — Canonical** | State that cannot be recomputed from source | tracked | n/a | **restore from VCS** (not recomputable) |
| **T1 — Committed derived** | Deterministic compiler output, committed for reviewable diffs & build-free queries | tracked | **yes** (byte-identical) | **recompile** from source + T0 |
| **T2 — Local cache** | Rebuildable indexes & ML artifacts | **gitignored** | tolerant | **rebuild** on demand |

Assignment:

| Artifact | Tier | Rationale |
|---|---|---|
| Source, `.sync/` YAML, Git | T0 | Runtime is canonical (Directive P1) |
| **Symbol Registry** (`registry/`) | **T0** | Holds identity continuity (birth history, aliases) — not recomputable (RFC-001 §2.3) |
| **Node store** (`nodes/`) | **T1** | Derived from source+registry, but committed so agents/CI query without a build and diffs are reviewable |
| **Graph revisions** (`revisions/`) | **T1** | Provenance/audit; append-only; git-tracked history |
| **Reverse index** | **T2** | Pure function of node store (C2) |
| **Search index** | **T2** | Rebuildable from node store |
| **Metrics** | **T2** | Rebuildable |
| **Embeddings** | **T2** | Disposable ML cache (Directive P5) |

The subtle line: **T1 is derived but committed.** Node files are treated like a checked-in compiled artifact (think lockfile / generated code) — versioned for convenience and review, *guaranteed* to match source by the determinism CI gate (§9). Only the **registry** is canonical-recovered-from-VCS; everything T1/T2 is recomputed.

---

## 2. Directory layout

```
.sync/knowledge/
├── registry/                      # T0 — canonical, sharded (C1)
│   ├── 2a/                        #   bucket = first 2 hex of NodeID hash
│   │   └── FUNC-2a9f8b17c4d1e0f3.json
│   └── ...
├── nodes/                         # T1 — committed derived, sharded by Type + bucket
│   ├── Function/
│   │   └── 2a/
│   │       └── FUNC-2a9f8b17c4d1e0f3.json
│   ├── Class/…  Module/…  WorkOrder/…  Review/…  Decision/…  Issue/…
├── revisions/                     # T1 — append-only provenance
│   └── REV-0000000182.json
└── cache/                         # T2 — gitignored (see §13)
    ├── reverse-index/
    ├── search/
    ├── metrics/
    └── embeddings/
```

- **Sharding = Type + 2-hex bucket** (256 buckets). Bounds directory size (a 10k-function repo → ~40 files/bucket) and localizes both filesystem and git-merge pressure. Registry is bucketed by NodeID (C1 — never a monolithic `symbols.json`).
- Filename **is** the NodeID. Lookup by ID is an O(1) path construction, no scan.

---

## 3. Node store: schema & the no-timestamp rule

A node file carries a **deterministic** block, an **ai** block (RFC-001 layering), and its **outgoing edges** inline.

```jsonc
// nodes/Function/2a/FUNC-2a9f8b17c4d1e0f3.json
{
  "id": "FUNC-2a9f8b17c4d1e0f3",
  "type": "Function",
  "deterministic": {
    "qualname": "AuthService.login",
    "path": "src/auth.py",
    "signature": "(self, credentials)",
    "location": { "line_start": 45, "line_end": 60 },
    "content_hash": "sha256:af8392e1…",
    "owner": "CLASS-4d5e6f0a1b2c3d4e",
    "edges": {
      "CALLS":   ["FUNC-91b7c3d4e5f60718", null],
      "IMPORTS": ["MOD-37a1b2c3d4e5f6a7"]
    },
    "unresolved": [
      { "relation": "CALLS", "name": "auth.login", "confidence": 0.4 }
    ]
  },
  "ai": {}                          // filled asynchronously; never by deterministic stages
}
```

**Determinism rules (binding — enforced by §9 CI):**
1. **No wall-clock fields in node files.** `last_updated`/timestamps break "compile twice → identical." All temporal provenance lives in `revisions/` only. *(This directly reverses the `last_updated` field the earlier POC drafts put on nodes.)*
2. **Canonical JSON serialization** (§9): sorted keys, sorted edge-target arrays, fixed indentation, LF newlines, trailing newline.
3. `content_hash` is over **source text of the symbol**, not the JSON — so the node is a pure function of source + registry.
4. `ai` block is excluded from `content_hash` and from determinism comparison (it's T2-sourced, may be absent).

**Node ↔ registry bijection:** every live code node has exactly one T0 registry record and vice versa (Layer-5 invariant, §11).

---

## 4. Edge model (the RFC-001 inheritance)

**Rule: an edge is stored in the file of its _source_ node, as a target-NodeID reference under a relation key.** Consequences, all load-bearing:

- **Rename/move rewrites zero edges.** Targets are permanent IDs; a renamed symbol keeps its ID, so every inbound edge across the whole graph is untouched. This is the entire payoff of RFC-001 and the reason edges are *not* sharded by path/name.
- **One-symbol edit → one node-file write.** No global `CALLS.json` to rewrite (kills the churn problem the earlier drafts had).
- **Unresolved edges are first-class.** A call that static analysis can't bind becomes either a `null` slot in the relation array *and* a `deterministic.unresolved[]` record (name + confidence), never a dropped edge. RFC-003 owns resolution retry.
- **Runtime-artifact edges follow the same rule.** `ASSIGNED_TO` lives on the WorkOrder node; `REVIEWS` on the Review node; `IMPLEMENTS` on the WorkOrder node. Source-node-owns-edge is uniform across code and runtime nodes.

**Inbound queries are not served by the node store.** "Who calls X?" / `called_by` requires the reverse index (§6). This is deliberate: outgoing-only keeps writes local; inbound is a derived projection.

---

## 5. Registry on-disk shape (identity semantics: see RFC-001)

RFC-002 specifies only *storage*; RFC-001 owns *semantics*. On disk:

- One file per symbol, bucketed: `registry/<bucket>/<NodeID>.json` (C1).
- Contents per RFC-001 §2.2: `node_id`, `kind`, `birth_key`, `current{path,qualname}`, `aliases[]`, `previous_names[]`, `owner`, `status`, `history[]`.
- **T0 — git-tracked, write-locked, migrated, Layer-5 validated.**
- Sharding makes concurrent symbol additions on two branches touch *different* files → merges are non-overlapping except when two branches mutate the *same* symbol (a real semantic conflict, correctly surfaced in one small file).

---

## 6. Reverse Index projection (C2)

- **Tier:** T2 (gitignored, rebuildable).
- **Content:** `NodeID → [ {source, relation}, … ]` — the inverse of every node's outgoing edges.
- **Build:** scan node store once, invert. Rebuildable in full at any time; incrementally, a changed node's old inbound contributions are removed and new ones added (RFC-003).
- **Why not T1:** it's a pure function of the (committed) node store, so committing it is redundant and doubles merge surface. Losing it costs one rebuild pass, nothing more.
- **Storage shape:** sharded by *target* NodeID bucket to mirror the node layout and keep per-file size bounded.

Without this projection, `called_by` is O(all nodes). With it, it's an O(1) file read. Every "impact of changing X" query depends on it.

---

## 7. Projection contract (sets up Phase 4)

All projections (reverse index, search, metrics, and the PKG view itself when materialized) obey one contract:

> A **Projector** is a pure, deterministic function `IR → artifact` (or `node-store → artifact`). It declares its **inputs** (which node types / fields it reads) so the incremental compiler (RFC-003) can invalidate precisely. It writes only T2. It never reads T2 written by another projector (no projection-to-projection chaining that could hide drift).

- **Search index:** tokens/summaries → NodeIDs. T2.
- **Metrics:** counts, fan-in/out, complexity aggregates. T2.
- **PKG view:** an optional materialized traversal-friendly form; node store + reverse index already *are* the graph, so this is a convenience projection, not a requirement.

Acceptance (Directive DoD #6, Plan Phase 4): delete all T2 → recompile → **identical** T2.

---

## 8. Revisioning & provenance

- **Tier:** T1. One file per revision: `revisions/REV-<zero-padded-monotonic>.json`. No `HEAD` pointer file (a merge chokepoint) — HEAD = lexicographically greatest filename.
- **Record:**
  ```json
  {
    "id": 182,
    "parent": 181,
    "git_commit": "abc123…",
    "sync_ref": "def456…",
    "compiler_version": "skc-0.1.0",
    "schema_version": "knowledge-1",
    "registry_version": 47,
    "counts": { "nodes": 1782, "edges": 5321, "unresolved": 44 },
    "built_at": "2026-07-12T10:23:45Z"
  }
  ```
- **`built_at` is allowed here** (revisions are audit, not compiled output) — it is the *only* place a wall-clock value lives, keeping node files deterministic (§3).
- Every query result is attributable to a revision → a surprising answer is traceable to the exact `(git_commit, sync_ref, compiler_version)` that produced it.

---

## 9. Determinism requirements (the CI gate)

Storage is only trustworthy if `compile(repo) → disk` is reproducible. RFC-002 mandates a **canonical serializer** used for all T0/T1 writes:

- keys sorted; arrays that represent sets (edge targets, aliases) sorted by value; stable float/int formatting; LF; trailing newline; UTF-8; no insertion-order dependence.
- **No wall-clock, no PID, no absolute paths, no RNG** in T0/T1 payloads (paths are repo-relative).
- **CI job "compile-twice-diff":** build → snapshot T1 → wipe → rebuild → `git diff --exit-code` over T1. Any nonzero diff fails the build. This gate is the enforcement mechanism for Directive Principle 2 and the whole T1 tier.

T2 is exempt (embeddings/metrics may vary); only T0/T1 must be byte-stable.

---

## 10. Merge semantics

The layout is engineered so git merges are boring:

- Distinct symbols → distinct files/buckets → **non-overlapping** merges.
- Same symbol edited on both branches → conflict localized to **one** node file (and/or one registry shard), which is exactly where a human should adjudicate.
- Deterministic serialization means "no real change" produces **byte-identical** files → auto-merges cleanly instead of spurious conflicts.
- Edges never conflict on rename/move (immutable-ID targets), removing the largest historical source of graph merge pain.

---

## 11. Validation — Layer-5 responsibilities owned by storage

Wired into `cli/validate.py` (5th layer). Storage-owned checks:

1. **Schema:** every T0/T1 file validates against `schemas/knowledge/{node,edge,symbol,graph-revision}.schema.json` (Draft7, same path as existing schemas).
2. **Unique IDs:** no NodeID appears in two files.
3. **Node ↔ registry bijection:** one live registry record per live code node, and back.
4. **Edge targets:** every edge target exists **or** has a matching `unresolved[]` record.
5. **Determinism-of-ID:** each node's ID equals `TYPE-first16(SHA256(birth_key₀))` from its registry record's earliest history entry (catches silent re-minting).
6. **Revision chain:** `parent` links form an unbroken monotonic chain; referenced `git_commit`/`sync_ref` resolvable.
7. **Canonical form:** files are already in canonical serialization (a re-serialize is a no-op) — this is the cheap on-`validate` proxy for the full compile-twice CI gate.

T2 is **not** validated by `stackmind validate` (it's cache); its correctness is guaranteed by rebuild, not inspection.

---

## 12. Rebuildability contract (Directive Principle 7)

Concrete meaning of "deleting `.sync/knowledge/` never destroys state":

| Deleted | Recovery | Result |
|---|---|---|
| T2 (cache) | rebuild from node store | identical (modulo embedding nondeterminism) |
| T1 (nodes, revisions) | recompile from source + T0 registry | **byte-identical** nodes; new revision appended |
| T0 (registry) | **`git restore`** — *not* recomputation | identity continuity preserved |
| **Everything incl. registry, with no VCS** | recompile from source | graph rebuilds, but **rename/move history is lost** (birth-keys re-mint from current state) — lossy, not corrupt (RFC-001 §2.4) |

The last row is the honest boundary: full identity continuity depends on the registry surviving in VCS. Losing it degrades gracefully (fresh identities) and never corrupts (distinct symbols never fuse).

---

## 13. `.gitignore` additions

```
.sync/knowledge/cache/
```

Only `cache/` (all T2) is ignored. `registry/`, `nodes/`, `revisions/` (T0/T1) are tracked.

---

## 14. Alternatives considered

- **Monolithic `graph.json`** — rejected (merge conflicts, whole-file rewrites). Superseded by sharding.
- **Global per-relation edge files (`CALLS.json`)** — rejected: a one-symbol change rewrites a repo-wide file, defeating sharding. Edges-in-source-node (§4) fixes it.
- **Single `symbols.json` registry** — rejected (C1): the highest-churn canonical file must not be one blob. Bucketed shards instead.
- **Node files git-ignored (pure T2)** — considered. Rejected for the POC: committing nodes gives reviewable diffs and build-free queries, and the determinism gate removes the drift risk. Revisit if node churn becomes a git problem.
- **Committing embeddings/reverse-index (T1)** — rejected: doubles merge surface for zero benefit; they're pure functions of committed nodes.
- **Wall-clock `last_updated` on nodes** — rejected: breaks determinism (§9). Time lives only in revisions.

---

## 15. Open questions (defer to RFC-003 / later)

- Exact incremental invalidation of the reverse index on a single node edit (full vs. delta) — RFC-003.
- Whether the PKG view is ever materialized (T2) or always served live from node store + reverse index — measure in Phase 4.
- Bucket width (2 hex / 256) vs. very large repos (3 hex / 4096) — parameterize; default 2.
- Cross-`.sync` bundling of `knowledge/` with the `.sync-ref` anchor the main repo already tracks — align with existing validate Layer-4 anchoring.

---

## 16. Recommendation

Adopt the **three-tier model** (§1), the **source-node-owns-edge / immutable-ID** rule (§4), the **reverse-index-as-T2-projection** (§6), and the **no-wall-clock-in-nodes determinism gate** (§9). These four decisions make Phase 3 storage and Phase 4 projection mechanical and merge-safe, and they discharge the plan's flagged risk (RFC-002 must carry RFC-001's immutable-ID constraint).

Proceed to **RFC-003 (Knowledge Compiler)**: parser, semantic analyzer, IR, event pipeline, and the incremental invalidation that this layout was shaped to support.
