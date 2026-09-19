# RFC-001: Symbol Identity & the Symbol Registry

**Status:** Draft — for decision
**Scope:** StackMind Knowledge Compiler (SKC) / Knowledge Engine (SKE), POC v3 (`SMPOC.md`)
**Depends on:** nothing
**Blocks:** RFC-002 (Storage & Projection), RFC-003 (Compiler Pipeline), all Week-1 code

---

## 0. Why this is the first decision

Node identity is the load-bearing wall. Everything downstream references a NodeID
and assumes it means the same thing over time:

- incremental compilation (which nodes changed vs. moved)
- graph diffing & git merges (same symbol → same ID on two branches)
- rename / move detection
- edge stability (a `CALLS` edge is just a pair of IDs)
- graph revisions & provenance
- GraphRAG references from summaries back into the graph
- cached embeddings keyed by node

If identity is wrong, the storage layer gets rewritten later. If it is right, the
rest of the compiler is mechanical. So we settle it before writing the first line.

---

## 1. Identity has two independent axes

The v3 doc, and the review of it, both collapse a 2-dimensional problem into one
line. There are **two** properties we want, and they are orthogonal — a scheme can
satisfy either, both, or neither:

### Axis 1 — Determinism across independent builds
Two engineers build the graph on two branches (or a teammate rebuilds from
scratch). The same source symbol **must** receive the same NodeID on both, with **no
shared counter or coordination**. Without this:
- every `git merge` of `.sync/knowledge/` conflicts on IDs,
- the same function exists under two IDs → duplicate identity no merge can fix,
- "diff-friendly sharded JSON" (the entire storage premise) is a fiction.

### Axis 2 — Stability across renames/moves within a history
When a function is renamed or its file is moved, its NodeID **should not change**, so
edges, embeddings, and history survive the refactor. Without this, every rename is a
delete+create: edges dangle, embeddings are recomputed, provenance resets.

|                          | Axis 1: deterministic across builds | Axis 2: stable across rename/move |
|--------------------------|:-----------------------------------:|:---------------------------------:|
| **A. Content-hash IDs**  | ✅ (pure function of path+qualname)  | ❌ (inputs change → ID changes)   |
| **B. Minted surrogate registry** | ❌ (two branches mint different IDs) | ✅ (ID pinned to symbol)     |
| **C. Compiler symbol table (this RFC)** | ✅                     | ✅                                 |

The v3 doc specifies **A's formula** (`TYPE + SHA256(module_path + ":" + qualified_name)`)
while claiming **B's benefits** ("stable across renames", "incremental-safe"). Those
are contradictory. The user's proposed compiler-style symbol table is the correct
instinct — but as usually sketched (opaque *minted* IDs) it lands on **B** and
silently loses Axis 1, reintroducing the merge problem sharding was meant to kill.

**This RFC proposes C: a synthesis that buys both axes.**

---

## 2. Decision: birth-deterministic ID + evolving symbol record

### 2.1 The NodeID
```
NodeID = TYPE "-" first16( SHA256( birth_key ) )
birth_key = <repo-relative path> ":" <qualified name>   # AT THE MOMENT OF FIRST SIGHTING
```
- **Deterministic mint (Axis 1):** the first build to ever see a symbol computes its
  ID by pure hash — no counter, no registry consulted for *minting*. Any independent
  build that first-sees the same `(path, qualname)` mints the identical ID. Two
  branches that both introduce `AuthService.login` in `src/auth.py` agree by
  construction.
- **Permanent & opaque thereafter:** once minted, the ID never recomputes. Its hash
  inputs are frozen at birth; later renames/moves do **not** re-hash.
- 16 hex chars (64 bits) → collision-negligible at repo scale; full digest retained
  in the symbol record for audit.

The subtlety that resolves the review's "contradiction": the hash is the **minting
function**, not the **identity function**. It runs once. After birth, identity lives
in the registry, not in the current path/name.

### 2.2 The Symbol Record (the compiler symbol table)
The registry stores one record per symbol — the user's model, made precise:

```json
{
  "node_id": "FUNC-92ab34ef0c1d2e3f",
  "kind": "Function",
  "birth_key": "src/auth.py:AuthService.login",
  "current": { "path": "src/services/auth.py", "qualified_name": "AuthService.log_in" },
  "aliases":       ["src/auth.py:AuthService.login"],
  "previous_names": ["AuthService.login"],
  "owner": "CLASS-4d5e6f...",
  "status": "active",
  "history": [
    {"rev": 181, "event": "born",   "key": "src/auth.py:AuthService.login"},
    {"rev": 204, "event": "renamed","from": "login", "to": "log_in"},
    {"rev": 219, "event": "moved",  "from": "src/auth.py", "to": "src/services/auth.py"}
  ]
}
```
- `node_id` — opaque, permanent (§2.1).
- `current` — where the symbol lives *now*; this is what changes on rename/move.
- `aliases` / `previous_names` — let a later parse of the *old* key resolve back to
  the same node during the same operation.
- `history` — append-only provenance, tied to graph revisions (RFC-002).

Rename → append alias, update `current.qualified_name`, **NodeID unchanged**.
Move → append alias, update `current.path`, **NodeID unchanged**.

### 2.3 The registry is CANONICAL state, not cache
This is the hard corollary and it contradicts the v3 doc, which files the registry
under `.sync/cache/`:

> A registry that provides identity-stability **cannot** be recomputable. If it is
> lost, the next build re-mints birth-IDs from *current* keys, so every
> since-renamed/moved symbol gets a new ID and Axis 2 evaporates.

Therefore the registry is:
- **git-tracked** (not gitignored) at `.sync/knowledge/symbols/` (RFC-002 fixes exact path),
- **write-locked** on update (same `.sync/runtime/LOCK`),
- **Layer-5 validated** (schema + invariants below),
- **revisioned** alongside nodes/edges.

Only **embeddings** remain cache (gitignored, hash-reused) — they are the one truly
recomputable artifact. Registry ≠ cache.

### 2.4 Rename detection stays advisory — degrade, never corrupt
Deciding "renamed" vs. "deleted + newly created" is a heuristic (match on kind +
body-hash + owner + signature within one changeset). The safety property:

- **Guess "rename" correctly** → provenance preserved, ID stable. Best case.
- **Miss the rename** → old node marked `deleted`, new node minted fresh; old edges
  dangle (already a first-class state via placeholder edges). **Lossy, not wrong.**
- **Never** can two *distinct* live symbols collapse onto one NodeID: their
  birth-keys differ, so their birth-hashes differ. Distinctness is guaranteed by
  construction; only *continuity* is heuristic.

We accept lossy continuity for the POC. That is the right risk posture: identity
never *corrupts*, it only occasionally *forgets*, and forgetting self-heals on the
next clean build.

---

## 3. Consequences

**For non-code nodes** (WorkOrder, Review, Decision, Issue): they already carry
natural, authored, stable IDs (`WO-431`). They bypass minting entirely — the registry
records them for uniformity but never hashes them. Axis 1 and 2 are trivially
satisfied because StackMind already guarantees their IDs.

**For edges** (RFC-002): edges are pairs of permanent NodeIDs, so rename/move touches
**zero** edge files — the point of §2. This is also why edges must not be sharded by
a mutable key; RFC-002 will address the `CALLS.json`-churn problem separately.

**For collisions:** birth_key collisions (two symbols, same path+qualname, same
instant — e.g. a conditional redefinition) are possible but pathological. The
registry detects a birth-key already bound to a live node and appends a disambiguator
(`#2`) to the losing symbol's key before hashing. Logged, rare, contained.

**For merges:** two branches that add the same symbol → identical NodeID → the node
file merges cleanly (or is byte-identical). Two branches that rename it differently →
same NodeID, conflicting `current`/`history` → a **real** semantic conflict surfaced
in one small file, which is exactly where you want it.

---

## 4. Registry invariants (Layer-5 validation hooks)

1. Every `node_id` is unique across the registry.
2. Every live code node in `nodes/` has exactly one registry record, and vice versa.
3. `node_id` matches `TYPE-first16(SHA256(birth_key₀))` where `birth_key₀` is the
   earliest `history` entry — i.e. the ID is verifiably a birth-hash, not re-minted.
4. `current` is never empty for `status: active`.
5. Every alias/previous key resolves to at most one live node.
6. `history` is append-only and monotonic in `rev`.

These are cheap, mechanical, and catch the exact drift (silent re-minting, dup IDs,
orphaned records) that would otherwise corrupt the graph invisibly.

---

## 5. Alternatives considered

- **A — pure content-hash, no registry.** Simplest; stateless. Rejected: fails Axis 2,
  so every rename/move is delete+create — churns edges, embeddings, provenance. Fine
  only if we declare rename-stability a non-goal (we don't).
- **B — minted surrogate IDs (UUID/sequential) in a registry.** Fails Axis 1: two
  branches mint different IDs for the same symbol → unmergeable duplicate identity.
  This is the trap the "just use a registry" framing falls into.
- **C — birth-hash + evolving record (chosen).** Buys both axes; cost is that the
  registry becomes canonical, write-locked, validated state (§2.3) and rename
  detection is heuristic (§2.4). We accept both.

---

## 6. Open questions (defer, don't block)

- Method identity when a method moves *between* classes in the same file — rename of
  `owner` vs. move? (Heuristic; punt to RFC-003.)
- Cross-file rename+move in a single commit — does the alias match survive when *both*
  path and qualname change at once? (Body-hash match should carry it; needs a test.)
- Overloads / conditional redefinitions beyond the `#2` disambiguator.

---

## 7. Recommendation

Adopt **Option C**. Freeze compiler implementation until RFC-001 is accepted, then
produce **RFC-002 (Storage & Projection)** and **RFC-003 (Compiler Pipeline)** on top
of this identity model. Only then does the Week-1 plan rest on firm ground — with
identity settled, Week 1 becomes mechanical rather than exploratory.
