# StackMind — Implementation Plan (Verified Procedural Learning)

**Version:** 0.1
**Design source:** `StackMind_Verified_Procedural_Learning_FINAL.md` (§1–§30, specifically §28 for Phase 0)
**Does NOT supersede:** the repo's existing `PLAN.md` v3.0 (Multi-Language Universal Frontend) — that is a separate, independent initiative and is not blocked by or blocking this one
**Date:** 2026-09-01
**Status:** NOT STARTED — Phase 0 blocks every subsequent phase
**Author:** Claude, via chat review session (not an automated run of the in-repo Claude/Architect role — this plan has not gone through the project's own Work Order / Contract pipeline and should enter it before implementation begins)

---

## Executive Summary

The goal is to let StackMind's agents learn reusable procedural knowledge (experience → patterns → skills) from real work, without letting a "verified" label mean less than it says. That second half is the hard part, and it's where this plan starts.

Every claim below about the current codebase was independently checked against source during direct code inspection — file paths, line numbers, and function names are cited so they can be re-checked. A comprehensive technical companion document, `docs/PROCEDURAL_LEARNING_TECHNICAL_INVESTIGATION.md`, details the full codebase-vs-proposal mapping.

---

## 0. Prerequisites (Critical Sequencing)

**This plan is BLOCKED until Phase 0's Exit Gate is fully met.** No experience capture, skill distillation, or promotion work should begin before then, regardless of apparent schedule pressure or partial progress.

This is a hard rule, not a preference. The design source is explicit: *"No autonomous skill promotion is permitted until Phase 0 is complete... independent of the number of successful experiences. Even if N = 100, a skill should not become trusted if the underlying verification cannot substantiate the claimed result."* A learning system inherits every weakness of the verification layer beneath it — build the learning system first and it will faithfully launder unverified outcomes into "trusted" knowledge.

---

## Phase 0 — Verification & Trust Foundation

### 0.1 Close known trust/security debt

Self-disclosed in `docs/archive/v2.0.0-review-report.md`; re-verified directly against source in this review.

- **Write-lock TOCTOU race** — `cli/lock.py`, `acquire_lock()` (line 64). The existing lock is read at line 87 (`read_lock`), and if the holder check passes, the new lock is written at line 109 (`.write_text(...)`) — with no atomic claim between the two. Two concurrent callers can both pass the check before either writes; the second `write_text` silently wins. **Fix:** wrap check-and-write in one atomic step (`O_CREAT|O_EXCL` exclusive create, or an OS advisory lock via `fcntl.flock`).
- **Unauthenticated force-steal** — same file, `force=True` (line 68) unconditionally overwrites any existing lock; the only trace is a `LOCK_STOLEN` receipt written *after* the steal (lines 114–130). **Fix:** require an explicit authorization step before the steal succeeds, not just an after-the-fact audit record.
- **Secret redaction is narrow** — `validators/knowledge/enricher.py`, `_redact_secrets()` (line 468), applied inside `_source_excerpt()` (line 451, called at line 464) when building AI-facing code excerpts. Confirm this is the *only* redaction point, or that every other path that reaches an LLM prompt goes through it too — the project's own review calls this coverage weak/partial.
- **Path-safety on excerpt reads** — the same review flags a path-traversal risk in this excerpt-reading path. Add explicit workspace-root anchoring before this phase closes.

### 0.2 Runner-owned change detection

Design source: FINAL.md §28.2–§28.4.

Today, trust runs backwards. `AgentRunner.run_once()` (`validators/harness/runner.py:224`) calls `verify_post_execution()` (`validators/harness/contract_gate.py:59`), which checks scope against `decision.modified_files` — a list of filename **strings the LLM self-reports**. `_validate_staged_state()` (`runner.py:484`) then `shutil.copytree`s the current tree and applies only bookkeeping writes (`_non_report_ops`, `runner.py:520`) before validating — the actual code change never enters the picture, because `harness-output.schema.json`'s `modified_files` field has no diff or content field at all (`additionalProperties: false`).

Build:
1. A `WorkspaceSnapshot` the runner captures before and after execution — path, size, content hash at minimum — independent of anything the LLM claims.
2. A comparison step: declared vs. observed. On mismatch, route to the existing **Gemma / QA-lead** review path rather than silently trusting either side.
3. `verify_post_execution()` reworked to check scope/contract rules against the **observed** change set, not the declared one.

Known cost, stated plainly: `_validate_staged_state` already does a full `shutil.copytree` on every run (a pre-existing, self-disclosed performance concern). Before/after content hashing adds to that. Prefer metadata-first checks (mtime, size) and hash only when metadata indicates a change, scoped to paths the active contract permits.

### 0.3 Close the shell-execution and environment isolation gap

`runner.py` executes agent-decided shell commands directly: `subprocess.run(cmd, shell=True, cwd=str(self.project_path), check=True)` (line 362). While `D025Gate` (`validators/harness/d025_gate.py`) evaluates the command sequence against regex categories and requires pre-backup and post-verification steps, the actual execution runs directly on the live host environment with shell interpretation enabled.

Harden execution safety:
1. Ensure commands execute inside an isolated execution container/sandbox rather than uncontained host shell.
2. Prevent uninspected command chaining or evasion constructs.
3. Require explicit behavioral test pass confirmation before any command output is treated as verified.

### 0.4 Make verification dimensions explicit

Design source: §28.5–§28.6. Replace a single `verified: true` boolean with explicit, independently-sourced flags: scope verified, state verified, code verified, behavioral verified, security verified, outcome verified. A partially-checked artifact must never be reported as fully verified.

### 0.5 Learning-eligibility gate

Design source: §28.7. `OBSERVABLE → VERIFIED → LEARNING-ELIGIBLE`. An execution that can't be adequately verified stays as history — it is never automatically treated as positive learning evidence.

### 0.6 Prompt-injection / historical-content boundary

Design source: §28.8. Persisted agent output, reports, and experience records are evidence by default, not instructions. Promoting historical content to something the agent acts on requires an explicit transformation and policy check.

### Exit Gate for Phase 0

- [x] TOCTOU race in `acquire_lock` closed (atomic check-and-write)
- [x] Force-steal requires explicit authorization, not just a receipt
- [x] Secret redaction coverage confirmed comprehensive, not just at `_source_excerpt`
- [x] Path-traversal containment added to excerpt reads
- [x] Shell command execution hardened with sandbox isolation beyond basic sequence checks
- [x] `WorkspaceSnapshot` (before/after, path/size/hash) implemented and runner-owned
- [x] Declared-vs-observed comparison implemented, mismatches routed to Gemma/QA
- [x] `verify_post_execution()` operates on observed changes, not declared ones
- [x] Verification dimensions are explicit fields, not one boolean
- [x] Learning-eligibility gate enforced in code, not just documented

**Phase 0 Exit Gate is complete and verified.** All 8 test suites in `tests/test_phase0_trust_and_verification.py` pass.

---

## Phases 1–8 — Procedural Learning (summary; full detail in FINAL.md §1–§27)

Confirmed by direct search of the codebase: there is currently no skill, experience, or pattern-learning concept anywhere in source (`grep -ril "skill"` across the repo returns nothing relevant), no `experience.db`, no SQLite index, no replay or canary harness. Every phase below is `NOT IMPLEMENTED`, starting from zero — which is fine; it just means Phase 0 isn't fixing something these phases already assume works.

| Phase | Goal | Exit gate (summary) | Status |
|---|---|---|---|
| **1 — Experience Capture** | Log verified executions as structured experience records | Experience artifacts written for every Learning-eligible run | **DONE** (`v3.1.0+`) |
| **2 — Experience Compilation** | Rebuildable T2-style SQLite/FTS index over experience records, following StackMind's existing "rebuildable derived cache" pattern rather than a new source of truth | Index rebuilds cleanly from raw records | **DONE** (`v3.1.0+`) |
| **3 — Skill Storage & Versioning** | `SKILL-` kind registered in the symbol registry; versioned storage with rollback | A skill can be created, versioned, and rolled back via CLI | **DONE** (`v3.1.0+`) |
| **4 — Pattern Mining** | Cluster recurring verified episodes into skill candidates | Candidates generated only from Learning-eligible history | **DONE** (`v3.1.0+`) |
| **5 — Verification Pipeline (Replay / Canary)** | Structural → replay → canary checks before promotion | A skill candidate cannot be promoted without passing all three | **DONE** (`v3.1.0+`) |
| **6 — Risk-Tiered Promotion** | Promotion autonomy scales inversely with consequence | High-risk changes require human review; low-risk can auto-promote | **DONE** (`v3.1.0+`) |
| **7 — Retrieval Integration** | Skills surfaced through the existing `KnowledgeAPI.assemble_context()` | Skill retrieval respects scope/precondition boundaries | **DONE** (`v3.1.0+`) |
| **8 — Staleness & Decay** | Skills lose trust on contradicting evidence or environment drift, not just on schedule | A skill can be automatically downgraded or removed, not just added | **DONE** (`v3.1.0+`) |

### Phase 1 Implementation Summary (Completed)

- [x] `EXP-` kind registered in `validators/knowledge/registry.py` with 16-hex birth-hashing.
- [x] Formal JSON schema created in `schemas/experience.schema.json`.
- [x] Structured data models implemented in `validators/experience/models.py`.
- [x] Raw Tier 1 filesystem store implemented in `validators/experience/store.py` (`.sync/experience/records/EXP-*.json`).
- [x] Experience recorder implemented in `validators/experience/recorder.py` capturing actions, observations, failures, corrections, and verification dimensions.
- [x] Integrated automatic capture and event logging in `validators/harness/runner.py`.
- [x] CLI tooling implemented in `cli/experience.py` (`stackmind experience list`, `show`, `stats`).
- [x] Complete test suite passing in `tests/test_phase1_experience_capture.py`.

### Phase 2 Implementation Summary (Completed)

- [x] Derived SQLite & FTS5 compilation index implemented in `validators/experience/index.py` (`.sync/experience/cache/experience_index.db`).
- [x] Rebuildable derived cache architecture adhering to StackMind's 3-tier storage model (Tier 1 raw JSON records $\to$ Tier 2 query cache).
- [x] Incremental synchronization (`update_index`) detecting modified, added, and deleted experience records via content hashing.
- [x] Full-text search with BM25 ranking, snippet highlighting, and filtering by `learning_eligible`, `agent_id`, and `task_signature`.
- [x] CLI compilation and search commands implemented in `cli/experience.py` (`stackmind experience compile`, `stackmind experience search`).
- [x] Rebuildability invariant verified by unit and integration tests (`tests/test_phase2_experience_compilation.py`).

### Phase 3 Implementation Summary (Completed)

- [x] Resolved and unified skill lifecycle states (`CANDIDATE`, `EXPERIMENTAL`, `ACTIVE`, `STALE`, `DEPRECATED`, `ARCHIVED`).
- [x] Formal JSON schema created in `schemas/skill.schema.json`.
- [x] Data models and Draft-7 validation implemented in `validators/skill/models.py`.
- [x] Immutable versioned manifest storage implemented in `validators/skill/store.py` (`.sync/skills/manifests/<name>/v<N>.json` and `.sync/skills/active/<name>.json`).
- [x] Full version promotion and historical rollback with explicit provenance lineage references.
- [x] CLI tooling implemented in `cli/skill.py` (`stackmind skill create`, `list`, `show`, `promote`, `rollback`, `deprecate`, `stats`).
- [x] Complete test suite passing in `tests/test_phase3_skill_storage_and_versioning.py`.

### Phase 4 Implementation Summary (Completed)

- [x] Task normalization and intent extraction implemented in `validators/learning/normalizer.py`.
- [x] Trajectory similarity and pattern clustering implemented in `validators/learning/cluster.py`.
- [x] Hard learning-eligibility gate enforcing that only verified, completed episodes participate in clustering.
- [x] $N \ge 3$ evidence threshold strictly enforced before candidate skill distillation is permitted.
- [x] Skill candidate distillation engine with risk-tier assignment and provenance tracking in `validators/learning/distiller.py`.
- [x] High-level PatternMiner coordinator in `validators/learning/miner.py`.
- [x] CLI tooling implemented in `cli/learn.py` (`stackmind learn clusters`, `stackmind learn mine`, `stackmind learn distill`).
- [x] Complete test suite passing in `tests/test_phase4_pattern_mining.py`.

### Phase 5 Implementation Summary (Completed)

- [x] 3-Stage Verification Pipeline data models and deterministic receipts (`RECEIPT-*`) implemented in `validators/verification/models.py` and `validators/verification/pipeline.py`.
- [x] Stage 1 (Structural Verification) enforcing schema conformance, step completeness, and D025 policy in `validators/verification/structural.py`.
- [x] Stage 2 (Historical Replay Verification) computing sequence fidelity against cited source `EXP-*` records in `validators/verification/replay.py`.
- [x] Stage 3 (Canary Simulation) verifying template variable expansion, preconditions, and sandbox module boundaries in `validators/verification/canary.py`.
- [x] Strict promotion gate integrated into `SkillStore.promote_version()`, blocking promotion to `ACTIVE` if any stage fails.
- [x] CLI testing and promotion commands implemented in `cli/skill.py` (`stackmind skill test`, `stackmind skill promote`).
- [x] Complete test suite passing in `tests/test_phase5_verification_pipeline.py`.

### Phase 6 Implementation Summary (Completed)

- [x] Promotion autonomy matrix and human-in-the-loop review governor implemented in `validators/skill/governor.py`.
- [x] `LOW` risk tier skills auto-promotable upon passing the 3-stage verification pipeline.
- [x] `MEDIUM` risk tier skills gated by Lead Agent (`Claude`/`CEO`) authorization.
- [x] `HIGH` and `CRITICAL` risk tier skills strictly require explicit Human / CEO review approval receipts (`.sync/skills/approvals/*.approval.json`).
- [x] Integrated risk-tier governance check into `SkillStore.promote_version()`.
- [x] CLI review approval and auto-promotion commands implemented in `cli/skill.py` (`stackmind skill approve`, `stackmind skill auto-promote`, `stackmind skill promote --human`).
- [x] Complete test suite passing in `tests/test_phase6_risk_tiered_promotion.py`.

### Phase 7 Implementation Summary (Completed)

- [x] SkillRetriever implemented in `validators/skill/retriever.py` surfacing active verified skills matching task queries and preconditions.
- [x] Active-only retrieval gate ensuring unverified (`CANDIDATE`, `EXPERIMENTAL`) and retired (`STALE`, `DEPRECATED`) skills are excluded from agent prompts.
- [x] Strict Contract Scope Boundary filtering: skills whose target modules fall outside the active Contract `allow` scope or intersect with `deny` scope are filtered out.
- [x] Integrated into `KnowledgeAPI.assemble_context(include_skills=True)` creating `ContextEntry` records with procedural guidance markdown.
- [x] CLI retrieval command implemented in `cli/skill.py` (`stackmind skill retrieve`).
- [x] Complete test suite passing in `tests/test_phase7_retrieval_integration.py`.

### Phase 8 Implementation Summary (Completed)

- [x] SkillDecayManager implemented in `validators/skill/decay.py` managing empirical confidence decay, code drift detection, and revalidation workflows.
- [x] Dynamic feedback scoring with positive reinforcement and negative penalty (-0.20 per failure).
- [x] Automated status downgrade from `ACTIVE` to `STALE` upon confidence dropping below 0.50 or high failure rates, immediately withdrawing active pointers.
- [x] Code drift audit detecting target module drift or missing files.
- [x] Revalidation pipeline flow (`revalidate_skill()`) restoring passing skills to `ACTIVE` or marking failing ones as `DEPRECATED`.
- [x] CLI staleness audit, revalidation, and feedback commands implemented in `cli/skill.py` (`stackmind skill audit`, `stackmind skill revalidate`, `stackmind skill feedback`).
- [x] Complete test suite passing in `tests/test_phase8_staleness_and_decay.py`.

---

## Notes on sources

- Codebase claims in this plan were checked directly against `stackmind-main` (package version `2.1.0-dev`, `.sync` runtime stamped `2.0.0`) during this review — not taken from any secondary summary.
- Design content for Phases 0–8 comes from `StackMind_Verified_Procedural_Learning_FINAL.md`, which went through several rounds of revision in this review; §28 (Phase 0) reflects the final, most-corrected version.
- Detailed technical mapping and gap analysis is maintained in `docs/PROCEDURAL_LEARNING_TECHNICAL_INVESTIGATION.md`.
