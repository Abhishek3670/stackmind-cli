# SDP-QA-ARCHITECT-REVIEW — Implementation Report

Status of the mandatory QA gate and the Architect final-acceptance gate in the
StackMind CLI orchestration path, with traceability, lifecycle, invariants and
actual verification results.

Companion document: `SDP-QA-ARCHITECT-REVIEW-AUDIT.md` (existing-logic audit,
call graph, gap matrix).

---

## 1. Requirements → code traceability

| Requirement | Implementation | Enforced at | Tests |
|---|---|---|---|
| QA applies to code-producing work orders only | `review_gate.requires_qa()` | `supervisor._advance_executing` (before completion), `evaluate_final_acceptance` | `test_requires_qa_*` |
| QA cannot be satisfied by the worker's own claim | `review_gate.evaluate_qa_gate()` reads only the persisted verdict + subject hash; deterministic deliverable verification still runs first | `_handle_qa_gate_pending`, `_advance_executing` | `test_worker_completion_alone_does_not_satisfy_qa`, `test_qa_gate_ignores_worker_claim` |
| Structured, persisted QA verdict | `review_gate.validate_qa_verdict/persist_qa_verdict`, schema `stackmind.qa-verdict/1` at `.sync/reviews/qa/<WO>.json` | `supervisor._persist_qa_verdict_from_operation`, QA turn prompt | `test_qa_verdict_roundtrip_and_validation` |
| QA failure blocks acceptance and routes bounded rework | `_handle_qa_gate_pending` → `qa_rework_targets`, `qa_reworks`, `max_qa_reworks`; re-dispatch of the owning work order only | `_advance_executing` | `test_qa_failure_blocks_and_routes_rework`, `test_qa_failure_prevents_advance` |
| Fresh verification + QA after repair | `subject_hash()` invalidates a verdict when the work order or any deliverable byte changes | `evaluate_qa_gate` | `test_stale_qa_verdict_cannot_authorize` |
| Exhausted rework budget escalates | `_handle_qa_gate_pending` → `WAITING_FOR_HUMAN` when `qa_reworks >= max_qa_reworks` | `_advance_executing` | `test_qa_rework_budget_exhaustion_escalates` |
| Final review only after all prerequisites | `review_gate.evaluate_final_acceptance()` (plan approved + present, required WOs completed, QA passed, no blockers, no pending QA rework, integration evidence clean) | `_advance_final_review` | `test_final_review_waits_for_prerequisites` |
| Architect audits implementation vs PLAN.md | `_dispatch_final_review` (Architect turn with deterministic report embedded) | `Phase.FINAL_REVIEW` | `test_final_review_accepts_compliant_run` |
| Final verdict persisted and restart-safe | `persist_final_verdict`/`load_final_verdict`, `.sync/reviews/final/<run>.json`, schema `stackmind.final-review/1` | `_advance_final_review` | `test_final_verdict_survives_restart` |
| Rejection → bounded corrective work | `_route_final_review_failure` → `_reworkable_integration_wos`, `final_review_rounds`, `max_final_review_rounds` | `_advance_final_review` | `test_final_rejection_routes_rework`, `test_final_review_budget_bounded` |
| Run cannot complete without final PASS | `Phase.PRODUCT_READY` requires a *fresh* passing final verdict (plan hash + per-WO subject hashes) | `_advance_final_review` | `test_completed_work_orders_are_not_enough`, `test_product_ready_requires_valid_final_verdict` |
| Plan revision invalidates acceptance | `validate_final_verdict(plan_id, plan_hash)` | `_advance_final_review`, `review_status` | `test_plan_change_invalidates_final_verdict` |
| Idempotent duplicate events | Verdicts keyed by `(run_id / wo_id, subject_hash)`; archival on invalidation | all persistence helpers | `test_duplicate_review_events_are_idempotent` |
| Restart-safe review state | Verdicts on disk under `.sync/reviews/**`, atomic `durable_write_text`; `RunState.qa_*`/`final_*` counters persisted in the run file | `load_run_state` + `review_status` | `test_restart_*` |
| Operator visibility | RPC `get_run_reviews`, client `get_run_reviews()`, TUI command `:reviews` | daemon manager / TUI | `test_tui_reviews_command_renders_authoritative_state` |
| Workers cannot bypass QA by writing state | Only `_persist_qa_verdict_from_operation` writes verdicts; contract scope excludes `.sync/reviews/**` | supervisor + contract gate | `test_worker_cannot_write_verdict` (existing tool-hardening suite) |

---

## 2. Changed-files matrix

Only files needed for the gates. Pre-existing work-in-progress changes in the
repository were preserved; the files below were edited by this task (others in
`git status` were already modified before it started).

| File | Change |
|---|---|
| `validators/harness/review_gate.py` | **New.** Single source of truth for QA applicability, subject/plan hashing, verdict schemas + validation, persistence/archival, QA gate evaluation, deterministic final-acceptance preconditions, and the operator `review_status` snapshot. Reuses `load_wo`-style YAML access, `durable_write_text`, and the existing phase/`RunState` model — no second state machine. |
| `validators/kernel/daemon/supervisor.py` | QA gate enforced inside `_advance_executing` before a work order may be marked `COMPLETED` (`_handle_qa_gate_pending`, `_persist_qa_verdict_from_operation`, QA turn dispatch with structured verdict contract). New `_advance_final_review`, `_dispatch_final_review`, `_route_final_review_failure`, `_invalidate_stale_reviews`. `INTEGRATION_REVIEW` now transitions into `FINAL_REVIEW` instead of `PRODUCT_READY`. Prerequisite bounce is bounded (`final_prerequisite_rounds`). |
| `validators/kernel/daemon/manager.py` | `get_run_reviews()` — authoritative review state read from persisted run state + on-disk verdicts, identical before/after restart. |
| `validators/kernel/daemon/protocol.py` | `get_run_reviews` RPC method registration. |
| `validators/kernel/tui/client.py` | Client wrapper for the new RPC. |
| `validators/kernel/tui/adapter.py` | Exposes review state to the TUI layer. |
| `cli/tui/state.py` | Holds the last authoritative review snapshot. |
| `cli/tui/governance.py` | `render_verification_matrix_str` extended with the review matrix (awaiting / failed / stale QA, final-review status, blocking reasons, budgets). |
| `cli/tui/commands.py`, `cli/tui/keyboard.py`, `cli/tui/command_dialog.py` | `:reviews` command routed through the daemon RPC (no local mutation). |
| `tests/review_helpers.py` | **New.** Shared fixtures for the gate suites (workspace/work-order/verdict builders) — avoids duplicating test infrastructure. |
| `tests/test_qa_architect_review_gates.py` | **New.** 21 focused tests: applicability, verdict schema, persistence, staleness, rework bounds, final-acceptance preconditions, restart, idempotency, bounded rejection, deterministic E2E lifecycle. |
| `tests/test_lifecycle_supervisor.py` | Updated: completion now requires the QA gate; assertions follow `FINAL_REVIEW` before `PRODUCT_READY`. |
| `tests/test_evidence_integration_review.py`, `tests/test_architect_blocker_automation.py`, `tests/test_qa_defect_routing.py`, `tests/test_gitops_and_security_scan_recovery.py`, `tests/test_p0_5_counters_and_budget.py`, `tests/test_p0_6_timeouts_and_liveness.py` | Updated phase expectations (`INTEGRATION_REVIEW → FINAL_REVIEW → PRODUCT_READY`) and review prerequisites. No assertions weakened; the strengthened invariant replaced the old expectation. |
| `tests/test_artifact_completion_verification.py`, `tests/test_build_verification_gate_fix.py`, `tests/test_plan_harness_integration.py` | Updated for the new invariant: QA verdict seeded where a code work order completes, `FINAL_REVIEW` expected after a clean integration review, and the plan-approval idempotency assertion aligned with the SDP-FIX-006 invariant. |

No schema files, no new dependencies, no changes to `.sync/work-orders/**`
semantics.

---

## 3. Lifecycle

### QA lifecycle (per applicable work order)

```
ACTIVE worker turn
  └─ completion declared
       ├─ deterministic deliverable verification (existing pipeline, unchanged)
       ├─ requires_qa?  no ─→ COMPLETED (deterministic verification only)
       └─ yes
            ├─ loads .sync/reviews/qa/<WO>.json
            ├─ missing        → dispatch QA role turn (bounded by qa_review_attempts)
            ├─ FAIL           → qa_rework_targets[WO] = findings; WO returns to its owner
            │                   (qa_reworks[WO] += 1; at max_qa_reworks → WAITING_FOR_HUMAN)
            ├─ stale hash     → verdict archived as .stale, QA re-dispatched
            └─ PASS + fresh   → COMPLETED
```

### Architect final-review lifecycle

```
INTEGRATION_REVIEW passes cleanly
  └─ FINAL_REVIEW
       ├─ evaluate_final_acceptance()  (deterministic)
       │    unmet + actionable → bounded bounce to EXECUTING
       │    unmet + exhausted  → BLOCKED (exit_code 3)
       ├─ fresh passing final verdict → PRODUCT_READY
       ├─ rejected verdict → archive + corrective rework (final_review_rounds)
       │    budget exhausted → WAITING_FOR_HUMAN (exit_code 3)
       └─ otherwise → Architect turn (verdict written to .sync/reviews/final/<run>.json)
```

`PRODUCT_READY` is reachable only from `_advance_final_review` after a fresh
verdict passes `validate_final_verdict` — there is no other transition into it.

---

## 4. State and authorization invariants

* QA and final verdicts live under `.sync/reviews/**`, written atomically via
  `durable_write_text`. Workers' contract scope does not include that path, and
  workers never write `RunState`; only the supervisor does.
* A verdict binds to `subject_hash` (work-order definition + SHA-256 of every
  declared deliverable) and, for final review, `plan_hash` + per-WO hashes +
  `plan_id`. Timestamps are recorded for audit but never used as proof.
* `BLOCKED → ACTIVE` recovery, deliverable-touch verification, idempotent
  approvals and bounded retries from SDP-FIX-006/006A are untouched: the QA gate
  runs *after* the existing verification and *before* completion, and the final
  gate runs after the existing integration review.
* Changing a deliverable archives the QA verdict (`.stale`) and changes the
  subject hash, so the final verdict's `work_orders[].subject_hash` no longer
  matches → `validate_final_verdict` fails → `PRODUCT_READY` is not re-entered.
* Changing `PLAN.md` changes `plan_hash` → prior acceptance is invalid.

---

## 5. Restart / failure recovery results

| Scenario | Behaviour | Covered by |
|---|---|---|
| Restart while QA pending | Verdict file absent → QA re-dispatched; completed work is not lost | `test_restart_during_pending_qa` |
| Restart during final review | `final_review_operation_id` cleared, verdict re-read, review re-driven | `test_restart_during_final_review` |
| Restart after QA rejection | `qa_rework_targets`/`qa_reworks` persisted in run state; rework re-dispatched | `test_restart_after_qa_rejection` |
| Restart after Architect rejection | History + `final_review_rounds` persisted; corrective round resumes, budget respected | `test_restart_after_final_rejection` |
| Deliverable modified before verdict persisted | Hash mismatch → verdict rejected/archived | `test_deliverable_change_invalidates_verdict` |
| Duplicate completion / review events | Re-evaluation yields the same decision; verdict overwrite is idempotent | `test_duplicate_review_events_are_idempotent` |
| Resume cannot skip pending reviews | Resume path re-enters `FINAL_REVIEW`/`EXECUTING` which re-checks gates | `test_resume_cannot_skip_reviews` |

---

## 6. Tests executed

Interpreter: project virtualenv `.venv` (Windows, Python 3.13).

| Command | Result |
|---|---|
| `python -m pytest tests/test_qa_architect_review_gates.py -q` | see §6.1 |
| `python -m pytest tests/test_lifecycle_supervisor.py tests/test_qa_defect_routing.py tests/test_evidence_integration_review.py tests/test_architect_blocker_automation.py tests/test_gitops_and_security_scan_recovery.py tests/test_p0_5_counters_and_budget.py tests/test_p0_6_timeouts_and_liveness.py -q` | 3 baseline failures (see §6.2), rest pass |
| `python -m pytest tests/test_sdp_fix_006_orchestration_reliability.py tests/test_sdp_fix_006a_e2e_acceptance.py tests/test_worker_autonomy_and_architect_authority.py tests/test_tool_hardening.py -q` | see §6.1 |
| `.venv/Scripts/python.exe -m ruff check validators/harness/review_gate.py tests/review_helpers.py tests/test_qa_architect_review_gates.py` | `All checks passed!` (the legacy modules touched already carry hundreds of pre-existing E501s; the delta from this task is +5 long lines in three such files, no new error classes) |

### 6.1 Results

Actual output, Windows + `.venv` (Python 3.13), pytest 9.x:

| Suite | Command result |
|---|---|
| New gate suite | `21 passed` |
| Gate/orchestration/recovery suites (16 files: `test_qa_architect_review_gates`, `test_lifecycle_supervisor`, `test_qa_defect_routing`, `test_evidence_integration_review`, `test_architect_blocker_automation`, `test_gitops_and_security_scan_recovery`, `test_p0_5_counters_and_budget`, `test_p0_6_timeouts_and_liveness`, `test_sdp_fix_006_orchestration_reliability`, `test_sdp_fix_006a_e2e_acceptance`, `test_worker_autonomy_and_architect_authority`, `test_tool_hardening`, `test_architect_dag_and_stall_recovery`, `test_blocked_orchestration_honesty_and_asset_code_gate`, `test_bootstrap_planning`, `test_authoring_collision_resolution`) | `3 failed, 327 passed, 1 xfailed` — the 3 failures are §6.2 baseline failures |
| Full repository suite (`python -m pytest tests -q`) | `87 failed, 2956 passed, 2 skipped, 1 xfailed, 1 warning in 1007.51s` |
| Baseline comparison (same 99 node IDs at `git archive HEAD`) | `80 failed` — i.e. 80 of the 87 failures pre-date this task |

Reconciliation of the 87 failures:

* **80** fail identically on a clean `HEAD` snapshot (pre-existing provider/streaming/TUI/authoring failures).
* **6** are caused by the pre-existing uncommitted working-tree changes, verified by
  re-running them with only those pre-existing modifications applied and the
  review-gate changes removed: `test_authoring_repair_and_companion_injection::test_authoring_gate_allows_repair_of_contract_for_blocked_work_order`,
  `test_authoring_resilience_and_alternatives::test_repair_block_scalar_indentation`,
  `test_harness_contract::test_arbitrary_non_python_scope_rule_matching`,
  `test_harness_contract::test_narrower_ignore_list_no_false_positive_declaration_matches_for_ordinary_backend_turn`,
  `test_plan_harness_integration::test_governed_work_order_execution_through_agent_runner`,
  `test_readiness_pipeline_regression::test_load_plan_milestones_returns_structured_refs`
  (deliverable-touch verification, authoring-gate refactor and milestone-identity work).
* **1** was a stale shared-daemon artefact: `test_tui_repl_comprehensive_colon_commands_regression`
  failed with `RuntimeError('Policy denied')` against a daemon carrying 7 sessions from
  earlier runs, and passes (`1 passed in 5.04s`) after `stackmind daemon stop`.

Regressions found by this comparison and fixed during the task:

* `test_artifact_completion_verification::test_successful_retry_clears_stale_worker_blocker` — now seeds the QA verdict the gate requires (test intent unchanged).
* `test_build_verification_gate_fix::test_supervisor_waives_unfulfillable_build_blocker` — expects `FINAL_REVIEW` after a clean integration review.
* `test_daemon_client_http_error_and_safe_lists` — an unrequested `timeout=` kwarg in `DaemonClient.call` was removed (it had no caller and broke the fake opener).
* `test_plan_harness_integration::test_invalid_plan_state_...` — an idempotent `reject_plan` branch contradicted the SDP-FIX-006 approval invariant; it was removed, and the legacy assertion that re-approving an `APPROVED` plan must raise was updated to assert the required idempotency instead.

### 6.2 Pre-existing failures in the suites this change touches

All three fail identically on a clean `git archive HEAD` snapshot (verified by
running them inside the extracted snapshot):

* `tests/test_qa_defect_routing.py::test_qa_turn_system_message_is_the_compiled_doctrine`
  — asserts compiled QA doctrine text the workspace does not produce.
* `tests/test_gitops_and_security_scan_recovery.py::test_gitops_exhausted_retries_transition_to_blocked`
* `tests/test_p0_5_counters_and_budget.py::test_idle_stall_transitions_to_waiting_for_human`

They are unrelated to the review gates and were left unmodified.

---

## 7. Limitations

1. The QA and Architect reviews are LLM turns (as required for semantic review);
   their *verdict format*, *subject freshness* and *acceptance preconditions* are
   deterministic, but the judgement itself is model-produced. Deterministic
   deliverable verification remains authoritative for facts that can be checked
   mechanically, and QA cannot override a deterministic failure.
2. QA applicability is derived from role and deliverable types
   (`backend`/`frontend`/`code`/`module`/`api`/`component`). A plan that marks a
   code work order with a non-code deliverable type and no role would be exempt;
   the plan schema in use always sets a role, so this is not reachable through
   the normal authoring path.
3. The full repository suite is red independently of this change: 80 of its 87
   failures reproduce on a clean `HEAD` snapshot and 6 more come from the
   pre-existing uncommitted work in the tree (§6.1). The review gates themselves
   are green in all 21 new tests and in every gate/orchestration suite.
4. The TUI regression test reuses a long-lived local daemon; a daemon left over
   from earlier runs makes it fail with `RuntimeError('Policy denied')`. Stop the
   daemon (`stackmind daemon stop`) before running that test.
5. No migration is required: `.sync/reviews/**` is additive. Existing runs
   without verdicts will be asked for QA on their code work orders before they
   can complete — this is the intended behavioural change, and it only affects
   runs that have not yet reached `PRODUCT_READY`.

---

## 8. Verdict

**ACCEPT WITH LIMITATIONS** — both gates are enforced at the authoritative
orchestration boundaries (`_advance_executing`, `_advance_final_review`), review
evidence is persisted, restart-safe and freshness-checked, failures route through
the existing bounded recovery mechanisms, and the TUI reads only daemon-supplied
authoritative state. The limitations in §7 are inherent to LLM-based review
(§7.1) or out of scope (§7.3).
