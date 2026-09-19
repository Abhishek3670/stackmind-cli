# RFC-005: Background Intelligence

**Status:** Draft — for decision
**Scope:** SKC Stage 5 — LLM summaries, embeddings, confidence, privacy policy, retry/cost discipline
**Depends on:** RFC-002 (Storage tiers), RFC-003 (Stage-5 handoff boundary)
**Blocks:** Implementation Plan Phase 6; improves (but does not block) RFC-004 Q4 semantic search

---

## 0. Why this RFC

Stages 1–4 are deterministic and LLM-free (RFC-003). Everything fuzzy lives here, behind a strict boundary: the enricher **adds** `ai` blocks and embeddings; it can be disabled, lag, or die mid-run and the system stays fully functional. This RFC also owns the one policy question every earlier draft dodged: **does source code leave the machine?** (§5 resolves it.)

Framing invariants (inherited, binding):
- Writes **only** the `ai` block of T1 nodes and T2 embedding cache. Never `deterministic`, never registry, never edges (RFC-003 §10).
- Everything it writes is **tagged** (confidence + provenance) and **disposable-or-regenerable**.
- **Never blocks** compilation or queries. Empty `ai` is a valid steady state.

---

## 1. Pipeline

```
compile batch commits (RFC-003 §9)
      │  changed NodeIDs
      ▼
Enrichment Queue  ──►  Worker pool (rate-limited)
                          │
                          ├── Summarizer (LLM)   → ai.summary, ai.purpose, ai.risk + confidence
                          └── Embedder           → T2 embeddings/<bucket>/<NodeID>.vec
```

- **Queue admission:** a node is enqueued when its `deterministic.content_hash` differs from `ai.enriched_hash` (see §2) or its `ai` block is empty. Idempotent: re-enqueueing an already-current node is a no-op.
- **Ordering/coalescing:** rapid successive changes to one node coalesce to a single job (latest hash wins).
- **Priority:** WorkOrder/Decision/Review nodes and high-fan-in code nodes (via reverse index) enrich first — they're the likeliest `assemble_context` members.
- **Drain target (SLA, advisory):** new nodes enriched within 5–10 min of commit under normal load; lag is visible in `graph stats`, never an error.

---

## 2. The `ai` block contract & staleness

```jsonc
"ai": {
  "summary": "Routes questions to the appropriate agent based on topic.",
  "risk": null,
  "confidence": 0.93,
  "enriched_hash": "sha256:af8392e1…",   // deterministic.content_hash at enrichment time
  "model": "…",                          // model id, pinned
  "prompt_version": "enrich-v1",
  "enriched_at": "2026-07-16T10:23:45Z"
}
```

- **Staleness is self-describing:** `enriched_hash != deterministic.content_hash` ⇒ summary refers to an older body. RFC-004 exposes this (`ai_stale: true` per result) so agents discount outdated summaries instead of trusting them.
- **`enriched_at`/model/prompt_version live only inside `ai`** — RFC-002 §9's no-wall-clock rule applies to *deterministic* content; the `ai` block is excluded from the determinism diff (RFC-002 §3.4), so provenance timestamps here are legal and useful.
- **Confidence semantics:** [0,1]; the enricher self-reports, calibrated coarsely (POC: fixed per task type — e.g. 0.9 summaries from full body, 0.6 from signature-only). Refinement deferred; the *plumbing* (tag, propagate, filter) is the POC deliverable.

Embeddings: keyed by NodeID, cached by `content_hash` — a rebuild or rename **reuses** vectors whose content is unchanged (rename keeps NodeID per RFC-001, body unchanged ⇒ cache hit; the earlier drafts' "rebuild recomputes everything" cost is designed out).

---

## 3. Failure, retry, rate limits

- **Per-job try/except:** one failed node never poisons the queue. Retry with exponential backoff (3 attempts), then park with reason; parked jobs are retried on the next scan.
- **429/quota:** honor provider headers; global rate limiter across the worker pool; on sustained throttling, drain slower — never escalate to an error visible to the compiler.
- **Kill-safety:** worker death mid-run leaves T0/T1 deterministic state untouched (it only ever patches `ai` blocks atomically via temp+rename) — the RFC-003 §9 atomicity pattern applies to `ai` patches too.
- **Cost caps:** hard per-day token/call budget in config; when exhausted, the queue pauses and `graph stats` reports it. Silent overspend is a defect; silent pause is not.

---

## 4. Model & embedding choices (POC defaults, all config-swappable)

| Task | Default | Notes |
|---|---|---|
| Summaries | Small/fast hosted model, or local via Ollama in `local` mode (§5) | pinned model id recorded per node |
| Embeddings | `text-embedding-3-small`-class hosted, or local (e.g. nomic-embed) in `local` mode | dimension recorded in cache header; changing models invalidates the whole T2 vector cache (dimension mismatch), which is acceptable — it's cache |
| Prompting | fixed template per node type, versioned (`prompt_version`) | template change ⇒ bump version ⇒ existing enrichments marked stale-by-version, re-enriched lazily |

---

## 5. Privacy policy — the deferred question, resolved

Earlier drafts contradicted themselves ("don't send raw code to the LLM" vs. "summarize the code"). You cannot produce a useful body-level summary without the body. The resolution is an explicit, config-gated mode — **not** a pretense:

| Mode | What leaves the machine | Summary quality |
|---|---|---|
| `full` | symbol source body + signature + docstring | best |
| `signatures` | signature + docstring only | weak but non-trivial |
| `local` | **nothing** — local models only (Ollama/LMStudio) | depends on local model |
| `off` | nothing; enricher disabled | none (deterministic-only graph) |

- **Default: `full` for this repo** (open-source, MIT); projects with sensitive source set `signatures` or `local` in config.
- Regardless of mode: **never** send `.sync/` runtime content that isn't the node being enriched; never send credentials/env; redact string literals matching secret patterns before transmission (cheap regex pass).
- The active mode is recorded in each `ai` block's provenance (`model` + mode), so an audit can verify what was exposed.

This subsumes the harness-research security items (keys env-only, HTTPS, no secrets in logs) — they apply verbatim to the enricher.

---

## 6. Cost model (placeholder discipline)

The POC ships with a *structure*, not fixed numbers (pricing drifts; the earlier docs' figures are all placeholders):

```
monthly_cost ≈ nodes_changed/day × 30 × (summary_tokens × model_rate + embed_tokens × embed_rate)
```

- `graph stats` reports actuals: jobs run, tokens spent, cache-hit rate, projected monthly.
- Sensitivity levers, in order of effect: enrichment gating (skip trivial nodes — e.g. functions < 3 lines), cache-hit rate (content-hash reuse), model tier, prompt length.
- Acceptance uses **measured** numbers from the Phase-6 run, not estimates — same discipline the harness benchmark adopts.

---

## 7. Validation (Layer-5 additions owned by this RFC)

1. `ai` block schema: allowed keys only, `confidence` ∈ [0,1], `enriched_hash` well-formed.
2. `ai` present ⇒ `model` + `prompt_version` present (no untraceable AI content).
3. **Cross-layer guard:** enricher output diff touches only `ai` blocks — CI asserts a Stage-5 run produces zero diff outside `ai` (the mechanical proof of the P2/P9 boundary).
4. T2 embedding cache is *not* validated by `stackmind validate` (cache; rebuild is its correctness story — RFC-002 §11).

---

## 8. Acceptance (Plan Phase 6 gate, restated concretely)

- Compile + all RFC-004 queries succeed with enricher **off** (deterministic-only mode).
- Turning it on only adds `ai` fields / T2 vectors; determinism CI (T1 minus `ai`) stays green.
- Kill -9 mid-run: no corruption; queue resumes; parked jobs retried.
- Content-hash cache: rebuilding the graph re-enriches **zero** unchanged nodes.
- Cost cap: exhausting the budget pauses cleanly and reports.

---

## 9. Alternatives considered

- **Inline enrichment during compile** — rejected (Directive P2/P9): couples latency and nondeterminism into the hot path.
- **Committing embeddings (T1)** — rejected (RFC-002 §14): binary-ish churn, pure cache.
- **LLM-inferred edges** written into `deterministic.edges` — rejected: inference may only ever live in `ai` (as suggestions), never in the relational layer agents trust.
- **Pretending "no raw code to LLM" while summarizing bodies** — rejected as incoherent; replaced by the explicit mode table (§5).

---

## 10. Recommendation

Adopt the **queue-based async enricher** with the **`enriched_hash` staleness contract**, the **four-mode privacy policy (default `full` here)**, **content-hash cost caching**, and the **`ai`-only write guard enforced in CI**. This closes the last policy hole in the docset and completes the five planned RFCs.
