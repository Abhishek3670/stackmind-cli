# RFC-004: Knowledge API

**Status:** Draft — for decision
**Scope:** SKC query surface — query language, traversal, context assembly, projection selection
**Depends on:** RFC-001 (Identity), RFC-002 (Storage & Projection), RFC-003 (Compiler) — the Phase 0 trio
**Blocks:** Implementation Plan Phase 7 (Agent Integration); consumed by RFC-006 (Harness Runtime)

---

## 0. Why this RFC

RFC-001/002/003 define how knowledge is *produced and stored*. RFC-004 defines how it is *consumed* — the single surface through which every agent (and the RFC-006 Agent Runner) retrieves project context instead of re-parsing repository files (Directive DoD #4). Two commitments frame it:

1. **The API is strictly read-only.** It never writes T0/T1/T2. Agents never write knowledge (Directive P6); neither does the thing agents call.
2. **Every response is provenance-stamped.** A result that can't say which graph revision produced it is not a result.

---

## 1. Consumers and access modes

| Consumer | Mode |
|---|---|
| Agents (Planner, Coder, Reviewer, QA, Debugger) | Python API (in-process) or CLI |
| RFC-006 Agent Runner | Python API — internal context half of its context builder |
| Humans / CI | CLI (`stackmind graph query`, `explain`, `stats`) |
| Optional local HTTP endpoint | Deferred — CLI/Python only for POC (Plan §9 keeps scope bounded) |

---

## 2. Query surface (four primitives)

Everything is composed from four primitives. No custom query language for the POC (explicit non-goal in the Plan) — typed filters + traversal verbs.

### Q1 — Lookup (exact)
```python
kg.get(node_id)                         # O(1): path constructed from ID (RFC-002 §2)
kg.find(type="Function", name="login")  # registry/current-name resolution, incl. aliases
```
Name lookups resolve through the **Symbol Registry** — a query by an *old* name of a renamed symbol resolves via `previous_names`/`aliases` to the same NodeID (RFC-001 §2.2 payoff, surfaced at the API).

### Q2 — Filter (attribute scan)
```python
kg.filter(type="WorkOrder", status="ACTIVE")
kg.filter(type="Function", path_prefix="cli/")
```
Served from node store + T2 indexes; never requires parsing source.

### Q3 — Traversal (relational)
```python
kg.out(node_id, relation="CALLS")            # outgoing: read from the node file itself
kg.inbound(node_id, relation="CALLS")        # inbound: served by the reverse index (RFC-002 §6)
kg.path(from_id, to_id, max_hops=6)          # explain: shortest relation path
kg.impact(node_id, depth=2)                  # transitive inbound closure ("what breaks if X changes")
```
`inbound`/`impact` **require** the reverse index; if the T2 cache is missing, the API rebuilds it (projection contract, RFC-002 §7) rather than degrading to an O(all nodes) scan.

### Q4 — Semantic (fuzzy)
```python
kg.search("where do we handle JWT tokens", k=5)
```
Embedding similarity over the T2 vector cache, results re-ranked with deterministic signals (name match, path match). **Degrades gracefully:** if embeddings are absent/partial (enricher disabled or lagging — RFC-005), falls back to token/full-text search over names, signatures, and available `ai.summary` fields, and marks the response `semantic: false`. Semantic absence never makes a query fail.

---

## 3. Response contract

Every response carries the same envelope:

```jsonc
{
  "revision": 182,                    // graph revision that served this (RFC-002 §8)
  "git_commit": "abc123…",
  "stale": false,                     // see §4
  "semantic": true,                   // false when embedding fallback was used
  "results": [
    {
      "id": "FUNC-2a9f8b17c4d1e0f3",
      "type": "Function",
      "deterministic": { … },         // always present
      "ai": { "summary": "…", "confidence": 0.93 },   // may be {} — never blocks
      "provenance": { "source": "graph", "matched_by": "name|filter|traversal|embedding" }
    }
  ]
}
```

Rules:
- **Deterministic and `ai` fields are visibly distinct** in every result (RFC-001 layering carried to the consumer). Callers can request `deterministic_only=True` to strip AI content entirely.
- **Confidence is passed through, never hidden.** An agent deciding on an `ai.summary` sees its confidence.
- Same graph state + same query ⇒ same response (modulo `stale` flag). Semantic ranking ties are broken deterministically (by NodeID) so even Q4 is reproducible against a fixed embedding cache.

---

## 4. Freshness & staleness

The graph is derived; the repo may have moved since the last compile. The API must never silently serve stale knowledge as current:

- On each query session, compare the latest revision's `git_commit` against the repo HEAD (and `sync_ref` against `.sync` state).
- Mismatch ⇒ set `stale: true` on responses and include `staleness: {commits_behind, dirty_files}` when cheap to compute.
- **The API does not auto-recompile.** Triggering `graph update` is the watcher/daemon's job (RFC-003 §7) or the caller's choice. Serving stale-but-flagged beats blocking a query on a compile.

---

## 5. Context assembly (the Phase-7 payload)

The highest-level operation: turn a task into a bounded context bundle an agent can drop into its prompt.

```python
kg.assemble_context(
    task="WO-431",              # or free text
    budget_tokens=4000,
    include=("code", "workorders", "decisions", "reviews"),
)
```

Assembly algorithm (deterministic given graph + budget):
1. **Anchor** — resolve the task to seed nodes (the WO node, its `deliverable` path's Module nodes, or Q4 hits for free text).
2. **Expand** — bounded traversal from anchors (callers/callees 1–2 hops, linked Decisions/Reviews/Issues via runtime edges).
3. **Rank** — deterministic signals first (direct edge distance, WO linkage), semantic similarity second.
4. **Distill** — for each selected node emit compact context: qualname, signature, path:line, `ai.summary` *(confidence-tagged)* — **not raw file dumps** (the "clean, LLM-ready content" principle adopted from the harness research).
5. **Budget** — trim lowest-ranked to fit `budget_tokens`; report `dropped: N` so truncation is never silent (no-silent-caps rule).

Output includes the standard envelope (revision, staleness) so an agent can cite *which knowledge state* its work was based on — this is what makes agent output auditable at review time.

**Boundary with RFC-006:** `assemble_context` produces the **internal** half of a prompt. External evidence (web search) is the Agent Runner's context-builder concern; it *calls* this API and merges. The API itself never fetches anything external.

---

## 6. CLI mapping

Registered under the existing `@cli.group('graph')` (`cli/graph.py`; none of the 7 existing commands touched):

| CLI | API |
|---|---|
| `stackmind graph query --type Function --name login` | Q1/Q2 |
| `stackmind graph query "jwt handling" [--deterministic-only]` | Q4 (with fallback) |
| `stackmind graph callers FUNC-… [--depth 2]` | Q3 inbound / impact |
| `stackmind graph explain --from A --to B` | Q3 path |
| `stackmind graph context WO-431 --budget 4000` | §5 assembly |
| `stackmind graph stats` / `versions` | envelope metadata (already in Plan) |

Output: JSON (`--json`) for agents/CI, rich text for humans. Exit code 0 with empty `results` for no-match (a no-match is an answer, not an error).

---

## 7. Authority & safety

- **Read-only by construction:** the API layer holds no writer handles; T2 rebuild (the one "write" it may trigger) goes through the projection contract, writes only cache, and needs no lock.
- **No write-lock interaction** for queries — reads of git-backed JSON are lock-free (same rule as RFC-006 D2 fix).
- **No authority checks on reads** for the POC: any agent may read any knowledge. Knowledge is a projection of state agents could already read from `.sync/`/source; the authority model governs *writes*. (Revisit only if `.sync` ever holds agent-secret material.)

---

## 8. Layer-5 validation hooks owned by this RFC

1. Every revision referenced in an API smoke query resolves (chain unbroken — shares RFC-002 §11.6).
2. `assemble_context` on a fixture task returns byte-identical bundles across two runs on the same graph (determinism CI extends to the read path).
3. Alias resolution: querying a renamed fixture symbol by its old name returns the stable NodeID.

---

## 9. Alternatives considered

- **Cypher-like / GraphQL query language** — rejected for POC (Plan §9): four primitives + assembly cover the agent use cases; a QL is surface area without a consumer.
- **Auto-recompile on stale** — rejected: hides latency inside innocent-looking queries and races the watcher; flag-and-serve instead (§4).
- **HTTP service as primary interface** — rejected for POC: agents are in-process/CLI consumers; a daemon adds ops burden with no current caller. Interface shaped so an HTTP layer can wrap it later without change.
- **Hiding low-confidence `ai` fields** — rejected: filtering is the *caller's* policy; the API passes confidence through and offers `deterministic_only`.

---

## 10. Recommendation

Adopt the **four-primitive read-only surface**, the **provenance envelope**, **flag-don't-block staleness**, and **budgeted deterministic context assembly**. This is the contract Phase 7 is accepted against (agents resolve cross-file questions via the API with no repo file reads, results carrying revision + confidence). Proceed with RFC-005 in parallel — Q4 quality depends on it, but nothing here blocks on it.
