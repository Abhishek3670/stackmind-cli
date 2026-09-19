# Harness Engineering — Evaluation & Validation Report (v3)

**Status:** Final validation (supersedes v1 and v2)
**Evaluates:** `docs/SMPOC/harness_engineering_required.md` — Agent Runner / "Harness Runtime" POC plan (3rd iteration)
**Against:** This repository — `stackmind` v1.2.0 (Python CLI runtime) — claims verified directly against source code
**Related:** SKC docset (Verdict, Implementation Plan, RFC-001/002/003); candidate RFC-006
**Date:** 2026-07-16

---

## 1. Verdict

> **VALIDATED — build-ready with 4 sketch-level fixes.** The third iteration is dramatically more accurate than its predecessors: every deep protocol claim spot-checked against the codebase verifies, including obscure ones (`GEMMA-02`, `LOCK_STOLEN` receipts, template directory layout). It fully adopts the v2 report's demands: runtime-protocol integration (lock, authority, identity, shutdown), RFC-006 framing as the SKC pillar-3 seed, terminology fix ("WebSearchRunner" for the tool, "Harness Runtime" for the layer), Worker-level agent identity, validation-in-CI, and prompt-injection sanitization. Remaining defects are in pseudocode sketches and the cost model — none architectural.

**Iteration history:**
- **v1 doc:** targeted the wrong codebase entirely (MERN chatbot, `POST /chat`, MongoDB) — not applicable.
- **v2 doc:** right stack and layout, but no protocol awareness (no lock/authority/validation/shutdown) and one reversed data-flow (outbox as task source).
- **v3 doc (this one):** knows the protocol, verified to source-code depth. Converged.

---

## 2. Validation table — doc claims vs. actual code

| # | Doc claim | Verified against | Result |
|---|---|---|---|
| 1 | `GEMMA-02`: inbox-drain requirement; >3 consecutive re-reads → blocker + abort | `cli/shutdown.py:46,243`; `docs/protocols.md:213` ("Inbox Drain & Deferral Gate (GEMMA-02)") | ✓ |
| 2 | Forced lock steal (`--force`) writes a `LOCK_STOLEN` audit event | `cli/lock.py:119-121` — writes `receipts/LOCK_STOLEN_<agent>_<timestamp>.yaml` with `event_type: LOCK_STOLEN` | ✓ |
| 3 | `.sync/` layout includes `agents/`, `escalations/`, `releases/`, `PROTOCOL_DIGEST.md`, `SYSTEM_CONTEXT` | `templates/sync/` — all present (`agents/`, `escalations/`, `releases/`, `PROTOCOL_DIGEST.md`, `SYSTEM_CONTEXT.template.md`) | ✓ |
| 4 | Work-order schema defines `title`, `description`, `deliverable`, `milestones`, `constraints` | `schemas/work-order.schema.json` — all present as properties | ✓ (nit: only `id, type, title, status, priority, assigned_agents, dependencies` are in `required`; `deliverable` is conditionally required for actionable types) |
| 5 | Authority: only Claude writes canonical state (`TREE.yaml`, `runtime/boot/`); workers write `drafts/` + promotion step | `cli/promote.py` (CLAUDE-01 gate: validate draft → promote → validate canonical), README authority model | ✓ |
| 6 | `stackmind shutdown --defer` moves unprocessed inbox items to `_deferred/` | README, `cli/shutdown.py` | ✓ |
| 7 | Lock CLI syntax: `stackmind lock acquire <agent> --session-id N` / `release` / `status`; lock at `.sync/runtime/LOCK` | `cli/main.py` lock group, `cli/lock.py` | ✓ |
| 8 | Inbox = tasks *to* agent; outbox = session reports *from* agent (v2's reversal fixed) | README, `templates/sync/inbox|outbox` per-agent dirs | ✓ |
| 9 | 4-layer validation pipeline; runner writes must pass `stackmind validate` | `cli/validate.py` (Schema → Structure → Protocol → Boot) | ✓ |
| 10 | Python ≥3.10; additive integration (new modules/subcommand, zero changes to 7 existing commands) | `pyproject.toml`; matches SKC plan's additive pattern | ✓ |

Minor omission (harmless): `templates/sync/standup/` exists but is absent from the doc's layout diagram.

---

## 3. Remaining defects — fix before build (all sketch-level)

### D1 — Lock context manager never checks acquire success (unsafe pattern)
The pseudocode runs `subprocess.run(['stackmind','lock','acquire',...])` and proceeds to write **without checking the return code**. If the lock is held (e.g. by Claude), acquire fails, and the runner writes anyway — silently violating the exact protocol the lock enforces. The doc's own Failure-Modes section says "retry with backoff or abort" — the code sketch must actually do it: check `returncode`, retry with backoff, abort with a blocker report on persistent failure.

### D2 — Reads taken under the write-lock (contradicts its own rule)
RFC-006 lifecycle step 1 and the polling loop acquire the lock to **read** `work-orders/ACTIVE/` and the inbox, while the doc elsewhere states (correctly, matching RFC-003 §9) that the lock is held "only around write batches." Reads of git-backed YAML need no write-lock; polling under it starves other agents. Drop the lock from the read path.

### D3 — Cost model's numbers disagree because the search-gating fraction is unstated
Search pricing appears as "$5 per 1,000 queries" (provider table) but the per-query breakdown charges "$1.00 search fee" per 1k queries (~implies 20% of queries search), while "10k queries/mo ≈ $40 including $5 search" implies ~10% search. The hidden variable is the **retrieval-policy hit rate**, never stated. Fix: declare "assume X% of tasks trigger search," recompute one consistent table. Also "$5.00/1K request (prompt+completion tokens)" garbles per-request vs per-token units.

### D4 — Session-counter authority conflict
Lifecycle says the runner "acquires identity (session counter from TREE)" — but per the doc's own (code-verified) authority section, **only Claude writes `TREE.yaml`**. The runner may *read* its session number, not increment it. Fix: read-only session reference, increment via draft→promote, or a receipt-based counter.

**Nits:** "Local (Brave?) — free" mislabels Brave (its Search API is paid; free scraping ≠ Brave API). GPT-4o pricing figures remain placeholders — pin to real pricing at build time.

---

## 4. What the doc gets right beyond protocol accuracy

- **Search-provider abstraction** (Perplexity/SerpAPI/local behind one interface, normalized result schema, per-provider rate-limit handling) — clean seam; keeps the retrieval tool swappable.
- **Retrieval policy** (`should_use_search()` gating on freshness/dynamic-domain/source-request signals; skip for internal or conceptual queries) — the cost-control lever, rightly first-class.
- **Context builder** — internal state (work-order YAML) + sanitized external evidence with citations; matches the RFC-004 context-assembly direction (internal graph + external web converging in one assembly step).
- **Observability** — structured JSON event logs (search + LLM metrics), plus a `meta` block (provider chain, cache_hit, confidence, uncertainty) on outbox reports.
- **Testing plan** — lock-collision simulation, validate-on-every-write in CI (build breaks on violation), shutdown-semantics tests, mocked search adapters, sanitization unit tests.
- **Benchmark plan** — baseline (internal-only) vs. augmented, per the Phase-7 acceptance methodology adopted from the v2 report.
- **Effort estimate** — 30–40h including the ~6–8h protocol-integration overhead the v2 report predicted.

---

## 5. Fit with the SKC docset

- **RFC-006 seed:** the doc explicitly frames the Agent Runner as the SKC pillar-3 "Harness Runtime" specification skeleton (responsibilities, lifecycle, interfaces, failure modes, metrics). This is the first substantive content for a pillar that was label-only in the Verdict.
- **Terminology collision resolved:** "Harness Runtime" = execution layer; the search tool renamed ("WebSearchRunner"). Matches the v2 report's recommendation.
- **SKC invariants respected:** LLM outside the deterministic path; runner never writes `.sync/knowledge/`; runtime stays canonical; lock only around writes (after D2 fix); all writes validated.
- **Sequencing unchanged:** nothing here blocks or is blocked by SKC Phases 1–5. Under the current planning freeze, this doc is a spec, not a work authorization.

---

## 6. Disposition

1. **Accept as the working spec for RFC-006.** Formalize it into `docs/rfcs/RFC-006-harness-runtime.md` when RFC authoring resumes.
2. **Apply D1–D4 before any build** — all are localized edits to sketches/tables, ~1–2h total.
3. **Keep the benchmark section** as the template for Phase-7 acceptance measurements (baseline vs. augmented, accuracy/latency/tokens/cost).
4. **Pin real provider pricing** (LLM + search) at build time; treat all current figures as placeholders.

---

## 7. Bottom line

Three iterations: v1 described a codebase that doesn't exist here; v2 knew the directory layout but not the governance; **v3 knows the governance to source-line depth** — `GEMMA-02`, `LOCK_STOLEN`, CLAUDE-01 promotion, deferral gates all check out. Four sketch-level fixes (unchecked lock acquire, reads under lock, unstated search-gating fraction, TREE session-counter authority) stand between this document and a buildable POC spec. It is now fit to seed RFC-006.
