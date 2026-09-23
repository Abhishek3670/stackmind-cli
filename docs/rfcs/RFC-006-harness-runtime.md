# RFC-006: Harness Runtime (Agent Runner)

**Status:** Draft — for decision
**Scope:** SKC pillar 3 — the Agent Runner execution layer: task loop, context assembly, external retrieval, verification, observability
**Seeded by:** `docs/SMPOC/harness_engineering_required.md` (validated v3 spec) + `docs/HARNESS-ENGINEERING-EVALUATION.md` (code-verified claims; defects D1–D4 **incorporated as fixes here**)
**Depends on:** RFC-004 (Knowledge API — internal context), RFC-005 (shared retry/privacy discipline); protocol facts verified against `cli/lock.py`, `cli/shutdown.py`, `cli/promote.py`, `docs/protocols.md`
**Blocks:** any Agent Runner build; informs Verdict build-order item 5 ("Harness Layer")

---

## 0. Why this RFC

The Verdict names three pillars: Runtime Governance, Knowledge Compiler, **Harness Runtime** — the third was a label until the harness research produced a validated spec. This RFC formalizes it. One terminology ruling, binding docset-wide:

> **"Harness Runtime"** = this execution layer. The web-search component is a **retrieval tool** (*WebSearchRunner*) inside it — never "the harness."

Scope discipline: this RFC specifies the runner as a **protocol-compliant Worker agent**. It is *not* a scheduler, not a TUI, not orchestration (Verdict risk: "chasing orchestration instead of differentiation"). Those remain gated behind proven Knowledge-API adoption.

---

## 1. Position in the architecture

```
.sync/ runtime (canonical)              Knowledge projections (derived)
        │                                        │
        │  work orders / inbox                   │  RFC-004 Knowledge API
        ▼                                        ▼
                    ┌──────────────────────────────────┐
                    │        Agent Runner (Worker)      │
                    │  poll → context → [search] → LLM  │
                    │        → verify → write-back      │
                    └──────────────────────────────────┘
                                     │
                         outbox / drafts / WO updates
                         (locked, validated writes)
```

- Consumes tasks from **its own inbox** (`.sync/inbox/<runner>/`) and `work-orders/ACTIVE/` where it is in `assigned_agents`.
- Internal context via **RFC-004 `assemble_context`**; external evidence via the retrieval tool (§5).
- Emits session reports to **outbox**, WO updates per authority rules (§3), receipts on shutdown.
- **Never** writes `.sync/knowledge/` (Directive P6 — the runner is an agent; agents never write knowledge).

---

## 2. Lifecycle & task loop (D1/D2 fixes applied)

```
start:  register identity → read session number (read-only, §3) → announce via outbox
loop:
  1. READ  work-orders/ACTIVE + own inbox           ← NO LOCK (reads are lock-free)   [D2 fix]
  2. select task; decide retrieval (policy §6)
  3. assemble context: RFC-004 internal + sanitized external evidence (§7)
  4. call LLM (outside any lock)
  5. verify: schema-check own output; run `stackmind validate` on a staged copy
  6. WRITE under lock: outbox report + WO update/draft; release immediately          [D1 fix]
  7. no tasks → sleep(poll_interval); loop
shutdown: finish in-flight task → drain or --defer inbox (GEMMA-02) → receipt → release any lock
```

**D1 fix — lock acquisition is checked, always:**
```python
@contextmanager
def runtime_lock(agent, session_id, retries=5, base_delay=2.0):
    for attempt in range(retries):
        r = subprocess.run(["stackmind", "lock", "acquire", agent,
                            "--session-id", str(session_id)], capture_output=True)
        if r.returncode == 0:
            break
        time.sleep(base_delay * 2 ** attempt)      # lock held elsewhere — back off
    else:
        raise LockUnavailable(agent)               # → blocker report, task deferred; NEVER write
    try:
        yield
    finally:
        subprocess.run(["stackmind", "lock", "release", agent], check=False)
```
A failed acquire **defers the task and emits a blocker** — it never falls through to an unlocked write. `--force` steal is not used by the runner (that path writes `LOCK_STOLEN` receipts and is reserved for human/CEO intervention).

**D2 fix — the lock wraps *writes only*** (step 6). Polling, parsing, search, and LLM calls are lock-free; git-backed YAML reads need no serialization. Lock hold time is asserted "writes-only" in tests (same discipline as RFC-003 §9).

**Loop safety:** >3 consecutive re-reads of the same unprocessed item ⇒ abort with blocker (the GEMMA-02 pattern, `cli/shutdown.py`). Poll interval backs off when idle.

---

## 3. Identity & authority (D4 fix applied)

- The runner is a **Worker-level agent**: named entry in `AGENTS.md` (e.g. `hweb-runner`), own inbox/outbox dirs, own agent contract in `.sync/agents/`.
- **Authority invariants (verified against code):** only Claude writes `TREE.yaml` and `runtime/boot/` (CLAUDE-01, `cli/promote.py`). Therefore the runner:
  - **reads** its session number from TREE — never increments it **[D4 fix]**; its session identity is anchored by its outbox report + shutdown receipt (receipts are the runner-writable record);
  - writes state changes as **drafts** (`runtime/drafts/`) promoted via `stackmind promote`, or updates **only WO files where it is assigned**, under lock;
  - never touches other agents' inboxes/files (protocol: no cross-inbox scanning).
- **Shutdown:** full `stackmind shutdown` semantics — handoff report required, inbox drained or `--defer` to `_deferred/`, receipt written, lock released.

---

## 4. Verification (the pillar's middle word)

Before any write-back, the runner verifies its own output — this is what distinguishes a *harness* from a bare LLM loop:

1. **Schema:** the report/WO update validates against the relevant schema (`work-order.schema.json` etc.) locally.
2. **Runtime validation:** `stackmind validate` must pass on the written state; on failure the runner rolls back its write (or never promotes the draft), emits an error report to outbox, and halts that task — invalid state never persists silently.
3. **Provenance:** every report cites the RFC-004 `revision` its context came from, plus the retrieval `meta` block (§8) — reviewers can reconstruct exactly what the runner knew.

---

## 5. Retrieval tool abstraction (WebSearchRunner)

```python
class SearchProvider(Protocol):
    def search(self, query: str, k: int = 3) -> list[Result]: ...
# Result: {title, url, snippet, published, provider}  (normalized schema)
```

- Providers: Perplexity, SerpAPI, local/self-hosted — pluggable, one active chain with fallback order.
- Per-provider: rate-limit header awareness, 429 exponential backoff (3 tries), timeout, error normalization — the RFC-005 §3 retry discipline verbatim.
- **Query cache** keyed by normalized query text, configurable TTL (default 24h), hit/miss recorded in `meta`.
- **Cost caps:** per-task max searches, per-day budget; exhaustion pauses retrieval (task proceeds internal-only, flagged), never fails the task. *(D3 fix context: all cost figures are computed from a **declared search-gating fraction** measured in the benchmark — no unstated assumptions.)*

---

## 6. Retrieval policy (when to search)

`should_use_search(task) -> bool`, keyword-first with optional cheap-LLM classification later:

| Signal | Action |
|---|---|
| Freshness terms ("latest", "current", year ≥ now-1), news/market/competitor/pricing domains, explicit source/URL requests | **search** |
| Internal/policy/`.sync`-answerable, conceptual/fundamental questions | **no search** |

The measured hit rate of this gate is the **declared gating fraction** feeding the cost model (D3 fix) and is reported in `graph`-style stats for the runner.

---

## 7. Context builder

`prompt = system + RFC-004 assemble_context(task, budget) + sanitized external evidence + task instruction (cite sources)`

External-evidence sanitization (prompt-injection defense, RFC-005 §5 discipline shared):
- strip/neutralize instruction-like text and code blocks in snippets; truncate long passages;
- each snippet embedded with explicit citation (title, URL, date) — untrusted content is *quoted evidence*, never instructions;
- conflicting sources ⇒ both kept, conflict flagged in the report's `uncertainty` field;
- internal budget precedence: internal (deterministic) context is never evicted in favor of external snippets.

---

## 8. Observability

Structured JSON lines per event + a `meta` block on every outbox report:

```yaml
meta:
  knowledge_revision: 182
  provider_chain: ["perplexity"]
  searches: 2
  cache_hits: 1
  llm: {model: "...", prompt_tokens: 3800, completion_tokens: 450, latency_ms: 2100}
  cost_estimate_usd: 0.021
  confidence: "high"
  uncertainty: "sources disagree on release date"
```

Metrics: task latency breakdown (poll/search/LLM/write), lock wait+hold times, retrieval gate hit-rate, validation failures, cost actuals. Everything the benchmark (§9) needs falls out of these logs.

---

## 9. Acceptance & benchmark (Phase-7 methodology)

- **Protocol gates (binary):**
  - two concurrent runners: exactly one write wins per lock cycle; zero unlocked writes (D1 test);
  - lock hold time == write time only (D2 test);
  - runner-touched `.sync` passes 4-layer `stackmind validate` after every task;
  - shutdown with non-empty inbox fails without `--defer`; `--defer` lands items in `_deferred/`;
  - TREE.yaml byte-identical after a full runner session (D4 test).
- **Benchmark (baseline vs. augmented):** same task corpus run internal-only vs. retrieval-enabled; measure accuracy/hallucination (judged), latency, tokens, cost — with the gating fraction reported. Placeholder pricing forbidden in results; actuals only.

---

## 10. Alternatives considered

- **LLM self-invoking search tools (tool-use loop)** — deferred: developer-orchestrated retrieval gives deterministic gating, cost control, and simpler audit for the POC; revisit post-benchmark.
- **Runner as a Claude-level agent** — rejected: canonical-write authority stays with Claude (CLAUDE-01); Worker level suffices and keeps the blast radius small.
- **Holding the lock across a task** — rejected (D2): starves all other agents for seconds-to-minutes per LLM call.
- **HTTP daemon interface** — deferred with RFC-004's; the runner is a CLI-launched process for the POC.

---

## 11. Recommendation

Adopt the runner as specified: **checked locking around writes only, Worker authority with read-only TREE, verification-before-write-back, gated retrieval with declared cost fractions, and full protocol shutdown**. With D1–D4 folded in, the seed spec's four defects are closed and this RFC completes the docset's sixth pillar. Build authorization remains a separate decision (planning freeze; Verdict gates the Harness Layer behind Knowledge-API adoption).
