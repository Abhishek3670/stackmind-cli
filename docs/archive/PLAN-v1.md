# StackMind — Agent Runtime Flaw Analysis & Improvement Plan

**Based on:** Session handoff reports from Claude (session 30), Codex, Gemma, Gemini, Local-LLM  
**Runtime version analyzed:** v1.0.4 (commit dd35298, tag v1.0.4)  
**Date:** 2026-06-18  
**Status:** UPDATED — engine shipped v1.1.0/v1.2.0, see Part 0 for verification against new release

---

## Part 0 — v1.1.0/v1.2.0 Verification (added 2026-06-19)

The `stackmind` package (the engine repo, separate from this analysis) was checked against this plan. **Source verification note:** GitHub's directory-tree pages and individual `.py`/`.json` files are not reachable through automated fetch in this environment (tree pages are robots-disallowed; raw file paths aren't search-indexed unless already linked). This verification is therefore based on `README.md`, `docs/architecture.md`, `docs/protocols.md`, and `docs/cli-reference.md` — the externally documented behavior, not a direct code read. Treat "Addressed" below as "documented as addressed," pending a source-level confirmation.

### Addressed (13 of 22 items)

| ID | Fix | Evidence |
|---|---|---|
| PLAT-01 | Boot integrity now anchors to `INDEX.yaml`, not TREE-vs-TREE | Layer 4: "Canonical drift — TREE.yaml work-order totals must match INDEX.yaml" |
| PLAT-03 | Write-lock implemented as `stackmind lock acquire/release/status` | `.sync/runtime/LOCK`; `shutdown` is the canonical release mechanism |
| PLAT-04 | Normalization writes a `NORMALIZATION` decision entry | `stackmind promote` records it in `decisions/` on success |
| PLAT-05 | `.sync-ref` anchoring added to validate | Layer 4: "live .sync HEAD must match the .sync-ref SHA tracked by the main repo" |
| CLAUDE-01 | Draft promotion gated validate→promote→validate | `stackmind promote` enforces this exact sequence |
| CODEX-01 | Shutdown re-reads TREE at write time | "never a value cached earlier in the session" |
| CODEX-02 | Untracked `.sync` paths flagged | Layer 3: "Untracked .sync path detection" (WARN) |
| CODEX-03 | TREE-vs-INDEX cross-validation | Covered by the same canonical drift check as PLAT-01 |
| GEMMA-01 | Snapshot version lag detection (3-version threshold) | Layer 4: WARN when snapshot lags TREE by >3 versions |
| GEMMA-02 | Inbox drain gate blocks shutdown on unprocessed items | `stackmind shutdown`: "must contain zero unprocessed items" |
| GEMINI-03 | Bundled review files now an ERROR | Layer 3: "review file referencing more than one work order violates D024" |
| GEMINI-04 | `release_target` required on completion notices | Layer 3: ERROR if missing |
| LOCAL-LLM-02 | Canonical writes routed through validated `promote` | Validation gates surround both sides of the write |

### Partial (3 items)

| ID | What shipped | What's still missing |
|---|---|---|
| PLAT-02 | `graph_version` field added; lock reduces write collisions | No formal broadcast/invalidation mechanism (no `BROADCAST` file, no `last_normalized_at`). Agents still rely on the lock alone to infer staleness. |
| GEMMA-04 | Inbox drain gate (GEMMA-02) indirectly helps | No exception path for messages that legitimately arrive mid-session after shutdown has started — risks normalizing `--force` |
| LOCAL-LLM-03 | CODEX-01 fix reduces stale-version commit risk | No post-`git commit` readback step described in the shutdown sequence |

### Still open (6 items)

CLAUDE-02 (no dispatch-confirmation tracking), CLAUDE-03 (session numbering notation unstandardized), GEMMA-03 (quality metrics still lack required commit hash / timestamp), GEMINI-01 (broken local test environment still has no formal blocker protocol), GEMINI-02 (no tooling check against worker self-assignment in handoffs), LOCAL-LLM-01 (no schema field forcing delegated-vs-initiated action disclosure).

### New issues found in v1.1.0/v1.2.0 docs

**Update 2026-06-19 (second pass):** Re-checked the repo — no new commits (still 24 commits, README still v1.1.0). Read `docs/migration-guide.md`, the one doc not yet reviewed. It resolves two of the five items below and adds one new confirming data point.

1. ~~`tree_version` vs `graph_version` naming inconsistency~~ — **RESOLVED, not a bug.** The migration registry confirms these are two distinct fields: `graph_version` was added fresh in the 1.0.0→1.1.0 migration ("Add graph_version and CEO inbox `_read` folder") and tracks the SHA-256 hash of `GRAPH_REPORT.md` per D023.3, while `tree_version` is the pre-existing TREE.yaml version counter. They coexist by design. Still worth a one-line clarification in `protocols.md`'s boot-sequence pseudocode so a new agent doesn't have to infer this from the migration log, but it's a documentation-clarity nit, not a structural issue.

2. ~~Version drift between docs (1.1.0 vs 1.2.0)~~ — **RESOLVED, not a bug.** `migration-guide.md`'s migration registry confirms a real `1.0.0 → 1.1.0 → 1.2.0 → 2.0.0` path (the last hop explicitly marked non-reversible as a major version). 1.2.0 is a genuine planned/in-flight target, not a stale example — the CLI reference showing `1.2.0` in its `--version` output is consistent with that. README lagging at "1.1.0" is just a normal release-in-progress snapshot, not a contradiction.

3. **The write-lock is advisory only.** (Unchanged from prior pass.) `cli-reference.md` states explicitly: "It is an advisory lock at the tooling layer." Any agent writing directly to `.sync/` files bypasses it, and `--force` lets any agent steal a lock with no approval gate. **Action:** require CLI-only writes in `AGENTS.md`; log a `LOCK_STOLEN` compliance event when `--force` is used.

4. **Inbox drain gate has no deferred-item pathway.** (Unchanged from prior pass.) The all-or-nothing gate (zero unprocessed items or `--force`) will likely push agents toward routine `--force` use when messages arrive late. **Action:** add a "deferred receipt" move-to-`_read/` option with a one-line defer note.

5. **No stated test coverage for the engine's own validators.** (Unchanged from prior pass.) **Action:** publish a coverage threshold (≥90% on `validators/`) and surface it in `stackmind doctor` output.

6. **New finding — schema drift was bad enough to need a dedicated repair migration.** `migration-guide.md`'s "Example 5: Normalizing Drifted Data (v1.1.0 → v1.2.0)" introduces a `normalize_enum_field` migration action specifically because "long-running runtimes can accumulate field values that were never schema-valid" — citing agents writing free-form `phase_status` values like `"RELEASED_DEPLOYED_HEALTHY (v1.0.5 ...)"` instead of a proper enum. This is a direct, independent confirmation of the core thesis of this plan: agents drift from protocol when validation doesn't actively enforce it at write time, and the drift accumulates silently until something breaks. It's good that this is now auto-repaired (non-conforming values are mapped to the closest enum with the original string preserved losslessly in `phase_status_detail`) — but the existence of this migration is evidence the underlying behavior (agents writing out-of-schema values) hasn't been fully closed off at the source, only cleaned up after the fact. **Action:** consider whether `validate` Layer 1 (Schema) should reject non-conforming enum writes at `stackmind shutdown` time, rather than relying on a periodic normalization migration to clean up after the fact.

---

---

## Executive Summary

Five agent handoff reports were analyzed against the StackMind protocol spec (D021–D031) and architecture docs. The findings reveal one root cause driving most issues: **there is no single source of truth that every agent verifiably anchors to at write time.** Multiple "almost-canonical" artifacts (`TREE.yaml`, `claude.boot.yaml`, `INDEX.yaml`, git tags) are supposed to stay synchronized, but nothing actively enforces their agreement. The result is agents that pass their own boot checks while being silently wrong about the shared state of the system.

All 14 identified flaws and their fixes are documented below, grouped by scope.

---

## Part 1 — Platform-Level Flaws

These affect all agents and must be addressed in the StackMind engine before per-agent fixes have lasting effect.

---

### PLAT-01 · Boot integrity check is circular — validates stale against stale

**Severity:** Critical  
**Protocol reference:** D021, validation Layer 4 (Boot Integrity)

**Flaw:**  
Layer 4 compares `claude.boot.yaml`'s recorded version against `TREE.yaml`'s `tree_version`. When both files are stale at the same version (both v1.0.3 while git/INDEX was already at v1.0.4), the check passes — because the two stale files agree with *each other*, not with the truth. Claude's session 29→30 caught this manually; the validator did not.

**Fix:**  
Boot integrity validation must anchor to an external ground-truth source. Read `INDEX.yaml.version` (which stays current through git operations) and compare it against `TREE.yaml.version`. If they differ, halt and require normalization before proceeding.

Concrete rule to add to Layer 4:

```
IF TREE.yaml.version != INDEX.yaml.version:
    RAISE canonical_drift_error
    HALT boot sequence
    WRITE normalization required notice to Claude inbox
```

**Acceptance criteria:** A simulated drift (manually set `claude.boot.yaml` to v1.0.3 while `INDEX.yaml` is at v1.0.4) must cause `stackmind validate` to fail with a clear error message, not pass silently.

---

### PLAT-02 · `tree_version` abused as a broadcast/cache-invalidation signal

**Severity:** High  
**Protocol reference:** D021 (boot optimization), TREE.yaml ownership

**Flaw:**  
Claude deliberately bumped `tree_version 46→47` during session 30 "so all other agents detect mismatch and re-read TREE on next boot." The platform has no formal broadcast mechanism, so a versioning field is being used as a notification bus. This creates false-positive drift events for other agents, leaves no entry in the decision log, and conflates versioning with signaling.

**Fix:**  
Add a `last_normalized_at` ISO timestamp field to `TREE.yaml`. Agents compare this to their snapshot's `last_read_at` during boot step 2 (PEEK). A newer timestamp triggers a full TREE re-read without touching the version counter.

Alternatively — and more robustly — add a `.sync/runtime/BROADCAST` file that Claude writes invalidation notices to (one line per entry: `<ISO timestamp> <reason>`). Agents read only lines newer than their `last_boot_at`.

`tree_version` must only increment when the TREE schema or structural content genuinely changes, not as a notification mechanism.

---

### PLAT-03 · No write-ordering or concurrency guard on `.sync` mutations

**Severity:** High  
**Protocol reference:** D023 shutdown sequence, key invariant 5 (only Claude writes to `runtime/boot/`)

**Flaw:**  
In the session-30 window, Claude wrote `claude.boot.yaml`, Codex wrote its draft snapshot, and Local-LLM committed the `.sync` repo — all in an undefined order with no locking. Codex's draft captured `tree_version 46` at the moment Claude was bumping it to 47, making the draft immediately stale before it was even written.

**Fix:**  
Add a `.sync/runtime/LOCK` file (a write-lock marker). The active agent creates it at boot (containing agent name + session ID + ISO timestamp) and removes it at shutdown. All mutations to canonical paths (`runtime/boot/`, `TREE.yaml`) must check for the lock and refuse if another agent holds it. The CLI's `stackmind shutdown` command should be the only mechanism that clears the lock, enforcing serialized canonical writes across sessions.

```yaml
# .sync/runtime/LOCK (example)
held_by: claude
session_id: 30
acquired_at: 2026-06-10T09:14:00+05:30
```

---

### PLAT-04 · Normalization actions bypass the decision log

**Severity:** Medium  
**Protocol reference:** D023 (audit trail), decisions/ ownership

**Flaw:**  
Claude promoted a draft and bumped `tree_version` under "architect authority" with no new entry in `decisions/`. Any state mutation that affects canonical files (`claude.boot.yaml`, `TREE.yaml`) should be traceable. Currently, normalization-level operations leave no footprint in the audit trail.

**Fix:**  
Add a lightweight decision type: `NORMALIZATION` (distinct from architecture decisions). Normalization entries are auto-generated by the shutdown sequence, capturing: what files changed, from/to which versions, and at what timestamp. Claude does not need to author these manually; the `stackmind shutdown` command should write them automatically when it detects it has modified a canonical file.

```yaml
# decisions/D-047-normalization.yaml (auto-generated example)
id: D-047
type: NORMALIZATION
authored_by: claude
session: 30
timestamp: 2026-06-10T10:00:00+05:30
changes:
  - file: runtime/boot/claude.boot.yaml
    from_version: v1.0.3
    to_version: v1.0.4
  - file: runtime/TREE.yaml
    tree_version_from: 46
    tree_version_to: 47
reason: Promoted session-30 draft; corrected canonical drift.
```

---

### PLAT-05 · `.sync` git-ignore creates audit blindness in the main repo

**Severity:** Medium  
**Protocol reference:** Two-repository architecture (architecture.md)

**Flaw:**  
Multiple agents note that `.sync/` is ignored by the main repo's git, so draft and session files do not appear in `git status`. This is by design, but it means no agent — and no CI system — can verify the `.sync` commit state from the main repo. Drift is invisible until someone manually `cd .sync && git log`.

**Fix:**  
Track a `.sync-ref` file in the main project repo. This file contains the most recent `.sync` commit SHA that was considered valid at the time of the last main-repo release. Add a `stackmind validate` check that compares the live `.sync HEAD` against `.sync-ref` and reports if the `.sync` repo has uncommitted or unexpected commits.

```
# .sync-ref (tracked by main repo)
ed9d856
```

This gives the main repo a verifiable anchor into the `.sync` repo without merging them.

---

## Part 2 — Per-Agent Flaws

---

### CLAUDE-01 · Draft promotion without pre-write schema validation

**Severity:** High  
**Agent:** Claude  
**Protocol reference:** D023 shutdown step 1, validation Layer 1 (Schema)

**Flaw:**  
Claude promoted the session-30 draft to `runtime/boot/claude.boot.yaml` as a normalization action, with no record of running `stackmind validate` before or after the write. Promoting a draft that fails schema validation would silently corrupt the canonical boot file.

**Fix:**  
The shutdown sequence must enforce: validate draft → promote → validate canonical. If either validation fails, abort the promotion and write a blocker to Claude's next-session inbox. The CLI's `stackmind shutdown` should automate this gate; it should not be possible to promote a draft without a preceding validation pass.

---

### CLAUDE-02 · Inbox directives written but not confirmed before handoff

**Severity:** Medium  
**Agent:** Claude  
**Protocol reference:** D022 (work order atomicity), inbox SLA rules

**Flaw:**  
Claude's "Messages to Dispatch" says the Local-LLM directive was "written to inbox." Dispatching a message and confirming receipt are different things. The handoff closes with the directive unacknowledged. If Local-LLM misses it, the normalization commit could be silently skipped.

**Fix:**  
"Messages to Dispatch" should be renamed "Messages written this session (pending read by recipient)." The distinction clarifies that the message exists on disk — Claude has already acted — but the receiving agent has not acknowledged it. TREE.yaml's per-agent `unread_inbox_count` field provides the verification loop; Claude should record it in the handoff as confirmation.

---

### CLAUDE-03 · Session numbering notation is ambiguous

**Severity:** Low  
**Agent:** Claude  
**Protocol reference:** Handoff report format (D023+)

**Flaw:**  
The handoff header reads "COMPLETED THIS SESSION (29→30)" while the body references "session 30." It is unclear whether the report covers work done in session 29 handed to session 30, or work done in session 30 just completed.

**Fix:**  
Standardize to: `session_completed: 30` with an optional `next_session_id: 31`. Ordinal phrasing (29→30) implies a transition; cardinal phrasing (session 30 complete) is unambiguous. Add this to the handoff report schema.

---

### CODEX-01 · Draft snapshot written with immediately-stale `tree_version`

**Severity:** High  
**Agent:** Codex  
**Protocol reference:** D021 (boot optimization), D023 shutdown step 1

**Flaw:**  
Codex read `TREE.yaml` at boot (tree_version 46) and then wrote its draft snapshot reflecting that version. Claude bumped TREE to version 47 in the same session window. When the draft is promoted, it will contain an incorrect tree_version — creating exactly the canonical drift the boot sequence is meant to detect and prevent.

**Fix:**  
Workers must re-read `TREE.yaml` immediately before writing their draft snapshot — not use the value cached at boot time. Boot step 2 (PEEK TREE) is an optimization for reading; it must not be used as the source for draft writes. Add an explicit pre-draft-write re-read to the shutdown sequence.

```
# Shutdown step 1 (revised):
RE-READ runtime/TREE.yaml   ← always fresh, regardless of boot cache
WRITE draft snapshot using fresh TREE values
```

---

### CODEX-02 · Untracked path `reviews/.` treated as a note, not a blocker

**Severity:** High  
**Agent:** Codex  
**Protocol reference:** D023 shutdown step 7 (commit .sync repo), D023.2 compliance check

**Flaw:**  
Codex mentions "Existing unrelated untracked path: reviews/." under Blockers as a casual observation. In the `.sync` repo, untracked files represent uncommitted work or orphaned artifacts. The `reviews/` directory may contain review files that were never dispatched to Gemma's inbox. The D023 shutdown sequence requires all relevant paths to be committed; an untracked directory is non-compliance evidence.

**Fix:**  
Any untracked file in the `.sync` repo discovered at boot must be classified as a Blocker and reported to Claude in the same session. The CLI's `stackmind validate` Layer 3 (Protocol Compliance) should flag unexpected untracked paths as a compliance warning and block new work assignment until resolved.

---

### CODEX-03 · No cross-validation against `INDEX.yaml` or git tags

**Severity:** Medium  
**Agent:** Codex  
**Protocol reference:** D021 (boot sequence), key invariants

**Flaw:**  
Codex confirmed its protocol hash and validated its own snapshot, but did not check `work-orders/INDEX.yaml` or `git describe --tags` to confirm team-level state. A worker validating only its own data can be out of sync with the team without knowing it.

**Fix:**  
Add a mandatory boot step for all workers after PEEK TREE: compare `INDEX.yaml.total_completed` against `TREE.yaml.total_completed`. A mismatch signals INDEX has been updated but TREE hasn't propagated. One field comparison, near-zero token cost.

---

### GEMMA-01 · Snapshot at `tree_version: 1` — session continuity completely broken

**Severity:** Critical  
**Agent:** Gemma  
**Protocol reference:** D021 (boot optimization), D023.2 compliance

**Flaw:**  
Gemma reports "Synced to tree_version: 47 (was 1 in stale snapshot)." tree_version 1 is the system's initial state at runtime initialization. Gemma's snapshot had not been meaningfully updated for the entire project lifetime — meaning Gemma has been performing full TREE re-reads every session, burning 150K+ tokens each time instead of the 1K–3K target from D021. This is a complete failure of boot optimization for the QA Lead role.

**Fix:**  
The D023.2 compliance check must flag agents whose snapshot `tree_version` lags behind TREE.yaml's current `tree_version` by more than a configurable threshold (suggested: 3 versions). Claude should receive a NON_COMPLIANT notice for any such agent during TREE maintenance. Additionally, `stackmind validate` Layer 4 should compute and report the version delta for each agent's snapshot explicitly.

```
stackmind validate output (proposed):
[WARN] gemma.boot.yaml snapshot version lag: 46 versions behind current TREE
       → Agent may be running without session continuity
       → Recommend: Claude to promote fresh Gemma snapshot
```

---

### GEMMA-02 · 10 pending review requests silently dismissed — D024 violation

**Severity:** High  
**Agent:** Gemma  
**Protocol reference:** D024 (Mandatory Review Handoff)

**Flaw:**  
Gemma reports "Reviewed inbox: 10 pending reviews exist but all pre-date v1.0.4 release" and takes no action on them. D024 requires each review request to have a documented outcome. Silently skipping 10 reviews leaves no audit trail and could hide a review that was still required for compliance or for a WO to be formally closed.

**Fix:**  
Gemma must formally close each pre-release review request. A batch-close message to Claude's inbox is sufficient:

```
# inbox/claude/2026-06-18_gemma_batch-close.md
Batch closing 10 pre-v1.0.4 review requests as superseded by release.
WOs: [list each WO-ID]
Reason: All changes incorporated into v1.0.4 (dd35298). No further action required.
```

Each original review message must then be moved to `inbox/gemma/_read/`. The CLI's `stackmind shutdown` validator should require that `inbox/<agent>/` contains zero unprocessed items before allowing the session to close.

---

### GEMMA-03 · Quality metrics have no commit hash or test-run timestamp

**Severity:** Medium  
**Agent:** Gemma  
**Protocol reference:** D024 (quality gate), Gemma agent contract

**Flaw:**  
Gemma's handoff states backend coverage 81.21%, frontend 86.57%, 247 tests GREEN — with no commit SHA and no test-run timestamp. These metrics are unverifiable. They could be from a prior branch, a pre-v1.0.4 state, or a run that predates Gemini's WO-055/WO-044 changes.

**Fix:**  
Quality metrics in all Gemma handoff reports must include:

```yaml
quality_metrics:
  commit: dd35298          # exact SHA, not tag
  branch: main
  tested_at: 2026-06-18T...
  backend_coverage: 81.21%
  frontend_coverage: 86.57%
  total_tests: 247
  status: GREEN
```

If Gemma cannot produce a commit reference for the metrics, they must be flagged as `unverified` in the report.

---

### GEMMA-04 · No awareness of Gemini's incoming WO-055 review request

**Severity:** Medium  
**Agent:** Gemma  
**Protocol reference:** Inbox SLA rules, D024

**Flaw:**  
Gemini explicitly dispatches a review request to Gemma for WO-055 and WO-044 in this same session. Gemma's handoff makes no mention of it. Either the message had not been written yet when Gemma ran, or Gemma did not scan the full inbox. If Gemma's next session doesn't find this message before boot completes, the review will be silently delayed.

**Fix:**  
The D023 shutdown sequence should require agents to read and acknowledge any inbox items received during the current session window — not just items present at boot. Any new items must be listed under "Messages received this session" in the handoff report, even if their full processing is deferred to the next session.

---

### GEMINI-01 · Local test environment failure mis-classified as non-blocking

**Severity:** High  
**Agent:** Gemini  
**Protocol reference:** D022 (work order deliverable requirements), D023.2 compliance

**Flaw:**  
Gemini reports "Local npm test environment issues persist but do not block the build." If Gemini cannot run tests locally, all verification is delegated to CI. This means Gemini is shipping implementation changes it cannot locally validate. Environment-specific regressions would only surface in CI — after the code is committed. The bar of "build passes" is lower than "tests pass locally."

**Fix:**  
A broken local test environment must be classified as BLOCKED with a formal open WO (type: BUGFIX, assigned to Gemini, priority P2). Gemini must not submit further implementation WOs for review until either: (a) the environment is fixed, or (b) Claude explicitly documents a temporary CI-only verification exception as a Decision entry. "Doesn't block the build" is not a sufficient standard for a QA-gated system.

---

### GEMINI-02 · WO-056 self-assigned — workers cannot plan their own next WO

**Severity:** High  
**Agent:** Gemini  
**Protocol reference:** Authority model (workers: implementation only), D022 (WO assignment: Claude)

**Flaw:**  
Gemini writes "Once approved, proceed with WO-056 (Register YOLO models) as indicated in the assignment." Work order assignment is exclusively Claude's responsibility per the authority model. A worker planning its own next task — even based on a prior mention — risks executing stale or superseded scope without explicit authorization.

**Fix:**  
Gemini's "My Next Tasks" must only list items found in its currently assigned WOs or explicit inbox directives. If WO-056 is assigned, the handoff must cite the source: "WO-056 (assigned by Claude, inbox message 2026-06-XX)." If not formally assigned, Gemini must escalate to Claude rather than assume.

---

### GEMINI-03 · WO-055 and WO-044 bundled into a single review request

**Severity:** Medium  
**Agent:** Gemini  
**Protocol reference:** D024 step 2 — one review file per work order

**Flaw:**  
D024 requires one review request file per work order. Bundling WO-055 and WO-044 into a single file makes partial approval/rejection impossible and breaks per-WO auditability. If Gemma approves one but not the other, there is no clean mechanism to record that outcome.

**Fix:**  
Generate separate review files:

```
inbox/gemma/2026-06-18_gemini_WO-055-review.md
inbox/gemma/2026-06-18_gemini_WO-044-review.md
```

The CLI's `stackmind shutdown` validator should detect any review file referencing multiple WO IDs and flag it as a protocol violation before the session is allowed to close.

---

### GEMINI-04 · Post-release changes shipped with no declared release target

**Severity:** Medium  
**Agent:** Gemini  
**Protocol reference:** D031 (version management), release ownership (CEO + Claude)

**Flaw:**  
WO-055 and WO-044 were completed in the current session while the system is already at v1.0.4 LIVE. These changes have not gone through Gemma review yet, so they cannot be part of v1.0.4. There is no mention in Gemini's handoff of whether these require a v1.0.5 release or will be folded retroactively — meaning versioning is undefined for active changes in production.

**Fix:**  
Any implementation WO completed after a release tag must include a `release_target` field in the completion notice sent to Claude's inbox:

```yaml
# inbox/claude/2026-06-18_gemini_WO-055-complete.md
wo_id: WO-055
status: COMPLETE (pending Gemma review)
release_target: v1.0.5   ← required field
commit: <branch/sha>
```

Workers must declare a release target; Claude makes the final versioning decision and documents it. Shipping without a declared release target is a protocol violation.

---

### LOCAL-LLM-01 · Overclaims scope — describes Claude's work as its own

**Severity:** High  
**Agent:** Local-LLM  
**Protocol reference:** Authority model, Forbidden Actions ("Modify canonical snapshots")

**Flaw:**  
Local-LLM's completed items include "Fixed canonical drift" and "Promoted session-30 draft → canonical claude.boot.yaml." These were Claude's actions. Local-LLM was the git executor — it committed files Claude had already mutated. An audit trail showing Local-LLM promoted a boot snapshot it does not own looks like a Forbidden Actions violation, even if it was authorized.

**Fix:**  
Local-LLM's handoff must clearly distinguish delegated actions from initiated actions:

```
✅ COMPLETED THIS SESSION:
- Executed commit of Claude's canonical state normalization
  (per Claude inbox directive 2026-06-10_claude_normalize.md)
  → Committed: runtime/boot/claude.boot.yaml, runtime/TREE.yaml
  → Commit SHA: ed9d856
  → Commit repo: .sync/
```

The source directive must always be cited. Summarizing Claude's work as Local-LLM's own is misleading and breaks the audit trail.

---

### LOCAL-LLM-02 · Committed without running `stackmind validate`

**Severity:** High  
**Agent:** Local-LLM  
**Protocol reference:** D025 (Destructive Operations Safeguard), D023 shutdown step 7

**Flaw:**  
Local-LLM's handoff shows no evidence of running `stackmind validate` before committing ed9d856. A commit of malformed YAML to `runtime/boot/claude.boot.yaml` or `TREE.yaml` would corrupt the canonical state source of truth for the entire agent team.

**Fix:**  
The commit directive Claude writes to Local-LLM's inbox must include an explicit pre-commit validation requirement. Local-LLM's completion notice must include the validation result:

```yaml
# inbox/claude/2026-06-18_local-llm_commit-complete.md
validation_run: stackmind validate .sync/
validation_result: PASS (4/4 layers)
committed_sha: ed9d856
```

The `stackmind shutdown` validator for Local-LLM should refuse to close without a recorded validation pass for any session that touched canonical files.

---

### LOCAL-LLM-03 · Committed tree_version is unconfirmed — 46 or 47?

**Severity:** Medium  
**Agent:** Local-LLM  
**Protocol reference:** D023 (audit trail), TREE.yaml ownership

**Flaw:**  
Given the race condition between Claude bumping `tree_version` to 47 and Codex's draft capturing version 46, it is unconfirmed which value was in `TREE.yaml` at the moment Local-LLM committed. There is no post-commit readback in the handoff to verify committed file contents.

**Fix:**  
After every `.sync` commit touching canonical files, Local-LLM must run a post-commit readback and include the result in its completion notice:

```
Post-commit verification:
  TREE.yaml tree_version: 47  ✓
  claude.boot.yaml version: v1.0.4  ✓
  commit: ed9d856
```

This creates a verifiable record that committed content matches what was intended.

---

## Part 3 — Priority Matrix

Status as of v1.1.0/v1.2.0 (2026-06-19): ✅ Addressed · 🟡 Partial · ⬜ Open

| Priority | ID | Flaw | Agents affected | Status |
|---|---|---|---|---|
| Critical | PLAT-01 | Boot integrity validates stale vs stale | All | ✅ |
| Critical | GEMMA-01 | Snapshot at tree_version: 1 — continuity broken | Gemma | ✅ |
| High | PLAT-02 | `tree_version` used as broadcast signal | All | 🟡 |
| High | PLAT-03 | No concurrency guard on `.sync` mutations | Claude, Codex, Local-LLM | ✅ |
| High | CLAUDE-01 | Draft promotion without schema validation | Claude | ✅ |
| High | CODEX-01 | Draft snapshot written with stale tree_version | Codex | ✅ |
| High | CODEX-02 | Untracked `reviews/.` treated as a note | Codex | ✅ |
| High | GEMMA-02 | 10 review requests silently dismissed | Gemma | ✅ |
| High | GEMINI-01 | Local test failure mis-classified as non-blocking | Gemini | ⬜ |
| High | GEMINI-02 | WO-056 self-assigned by worker | Gemini | ⬜ |
| High | LOCAL-LLM-01 | Overclaims Claude's work as its own | Local-LLM | ⬜ |
| High | LOCAL-LLM-02 | Committed without `stackmind validate` | Local-LLM | ✅ |
| Medium | PLAT-04 | Normalization actions bypass decision log | Claude | ✅ |
| Medium | PLAT-05 | `.sync` git-ignore creates audit blindness | All | ✅ |
| Medium | CLAUDE-02 | Inbox directives unconfirmed before handoff | Claude | ⬜ |
| Medium | CODEX-03 | No cross-validation against INDEX.yaml | Codex | ✅ |
| Medium | GEMMA-03 | Quality metrics without commit hash | Gemma | ⬜ |
| Medium | GEMMA-04 | No awareness of Gemini's incoming review request | Gemma | 🟡 |
| Medium | GEMINI-03 | Two WOs bundled in one review request | Gemini, Gemma | ✅ |
| Medium | GEMINI-04 | Post-release changes without declared release target | Gemini | ✅ |
| Medium | LOCAL-LLM-03 | Committed tree_version unconfirmed | Local-LLM | 🟡 |
| Low | CLAUDE-03 | Session numbering notation ambiguous | Claude | ⬜ |

**New since v1.0.4** (see Part 0 for detail): tree_version/graph_version naming inconsistency · version drift between docs (1.1.0 vs 1.2.0) · advisory-only lock has no bypass detection · inbox drain gate has no deferred-item pathway · no published test coverage for the engine's own validators.

---

## Part 4 — Recommended Implementation Order

*Original ordering (Phases 1–5) is preserved below for reference; Phases 1–3 are now substantially shipped per the v1.1.0/v1.2.0 verification in Part 0.*

**Phase 1 — Fix the validator (PLAT-01, PLAT-02)** ✅ PLAT-01 shipped. PLAT-02 partially shipped (`graph_version` exists; broadcast mechanism still missing).

**Phase 2 — Add write-ordering (PLAT-03)** ✅ Shipped as `stackmind lock`. Note from Part 0: the lock is advisory-only — `AGENTS.md` should mandate CLI-only writes to make the guarantee real in practice, not just in the happy path.

**Phase 3 — Fix draft write timing (CODEX-01 + all workers)** ✅ Shipped — shutdown re-reads TREE at write time.

**Phase 4 — Per-agent protocol compliance** Partially shipped (GEMMA-02, GEMINI-03, GEMINI-04, LOCAL-LLM-02 done). Still open: GEMINI-01, GEMINI-02, LOCAL-LLM-01, GEMMA-03, CLAUDE-02, CLAUDE-03 — these are largely behavioral/schema gaps rather than validator gaps, and may need handoff-schema or agent-contract changes rather than new `validate` checks.

**Phase 5 — Audit trail completeness (PLAT-04, PLAT-05)** ✅ Both shipped.

### Recommended next phase (Phase 6 — added 2026-06-19)

1. Resolve the `tree_version`/`graph_version` naming ambiguity in protocols.md and the TREE schema — this is a clarity bug that will cause confusion on every future boot until fixed.
2. Add the deferred-receipt pathway to the shutdown inbox-drain gate (closes both GEMMA-04 and the new inbox-drain gap) before `--force` becomes habitual.
3. Add a `LOCK_STOLEN` compliance event for `--force` lock acquisition, since the lock's only enforcement is social.
4. Publish a coverage threshold for `validators/` and surface it in `stackmind doctor`.
5. Address the remaining behavioral items (GEMINI-01, GEMINI-02, LOCAL-LLM-01, GEMMA-03, CLAUDE-02, CLAUDE-03) via `AGENTS.md` contract updates and, where feasible, new Layer 3 checks (e.g. a required `release_target`-style field for "assignment source" and "delegating agent").

---

*This document was generated from runtime analysis of session handoffs dated 2026-06-18, and updated 2026-06-19 against the stackmind engine's documented v1.1.0/v1.2.0 behavior. Verification in Part 0 is based on published docs (README, architecture.md, protocols.md, cli-reference.md); a follow-up pass against the actual `validators/` and `cli/` source is recommended once direct repository access is available, to confirm documented behavior matches shipped code.*
