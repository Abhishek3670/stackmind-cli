# SDP-QA-ARCHITECT-REVIEW — Existing-Logic Audit

Scope: mandatory QA review of backend/frontend deliverables and mandatory Architect
final acceptance against the user-approved `PLAN.md`, without duplicating any
existing orchestration, verification, authorization or recovery mechanism.

Repository: `stackmind-cli`, branch `investigate-delivery-quality`.
Audit performed against the working tree *as found* (uncommitted SDP-FIX-006/006A
work preserved, nothing reset, cleaned or stashed).

---

## 1. Method

The audit was performed by reading the actual implementation, not the docs:

* call graph of `LifecycleSupervisor.advance()` and its per-phase handlers;
* `RunState` serialization (`to_dict` / `from_dict` / `load_run_state`);
* daemon RPC surface (`validators/kernel/daemon/manager.py`, `protocol.py`);
* agent contract + permission matrix (`.sync/agents/*.contract.yaml`,
  `validators/kernel/permission_matrix.py`, `validators/harness/contract_gate.py`);
* deterministic verification helpers (`deliverable_verifier`, `d024_gate`,
  `integration_gate`, `dependency_gate`, `authoring_readiness`);
* TUI command model (`cli/tui/commands.py`, `keyboard.py`, `command_dialog.py`,
  `validators/kernel/tui/adapter.py`, `client.py`);
* existing tests that drive the supervisor execution loop
  (`test_lifecycle_supervisor.py`, `test_sdp_fix_006*`, `test_qa_defect_routing.py`,
  `test_evidence_integration_review.py`, `test_gitops_and_security_scan_recovery.py`,
  `test_p0_5_counters_and_budget.py`, `test_p0_6_timeouts_and_liveness.py`).

Baseline before implementation: the repository already contained a large body of
uncommitted SDP-FIX-006/006A work. Those changes were preserved verbatim; a clean
`HEAD` snapshot was used only for comparison, in a throw-away directory that was
deleted again (`.baseline-check/`).

---

## 2. What already existed

### 2.1 Orchestration state machine

`LifecycleSupervisor.advance()` dispatches by `Phase`:

```
AUTHORING_READINESS_GATE → AUTHORING → DISPATCHING → EXECUTING
  → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE
```

* `EXECUTING` (`_advance_executing`) polls worker turns, verifies declared
  deliverables on disk (`_verify_work_order_deliverables` →
  `validators.harness.deliverable_verifier`), and only then marks a work order
  `COMPLETED`.
* `INTEGRATION_REVIEW` (`_advance_integration_review`) runs the Architect
  integration turn and then re-runs `validators.harness.integration_gate`
  deterministically; `gate_decision` blockers override the LLM's own claim
  (anti-fabrication). Clean evidence is stored in `state.integration_evidence`.
* `PRODUCT_READY` / `GITOPS` dispatch the release turn, then archive completed
  work orders and transition to `COMPLETE`.
* Recovery: `escalate_wo_to_recovery`, `ARCHITECT_RECOVERY_DECISION`,
  `_retry_worker`, `retry_counts` / `max_retries`, `ignored_operation_ids`,
  `BLOCKED → ACTIVE` via `_unarchive_work_order` guarded by contract scope.
* `RunState` is persisted atomically per tick (`run-<id>.yaml`) and fully
  restored by `load_run_state`; the daemon rehydrates active runs from that file.

### 2.2 QA machinery that already existed

* **QA agent role**: `gemma` → role `qa` (`_role_for_agent`), agent contract
  `.sync/agents/gemma.contract.yaml`, permission matrix entry.
* **QA verdict parsing**: `validators/harness/d024_gate.py` parses
  `PASS` / `FAIL` / `NEEDS_CHANGES` verdict text from inbox/review artifacts;
  `_check_qa_verdict`, `_get_qa_feedback` surface it to rework prompts.
* **QA defect routing**: `_find_qa_defect_verdicts` +
  `_route_qa_defect_to_developers` turn QA-authored defect verdicts into rework
  turns for the owning backend/frontend workers.
* **Bounded QA rework**: `state.qa_reworks` / `state.max_qa_reworks`, then
  `escalate_wo_to_recovery` → `ARCHITECT_RECOVERY_DECISION`.
* **QA execution evidence**: `_qa_execution_evidence_gap` requires the QA turn
  to have authored a suite and recorded a real test run.

What did **not** exist: a *mandatory, per-work-order, revision-bound* gate in
`_advance_executing`. QA only reacted when a QA agent happened to emit a defect
verdict — a QA worker that never ran, or a stale verdict, could not block
acceptance because nothing consulted it at the completion boundary.

### 2.3 Architect machinery that already existed

* `Phase.INTEGRATION_REVIEW` with the deterministic `integration_gate`
  (routes, tests, build, evidence, anti-fabrication).
* `validators/harness/plan.py` (plan approval state), `authoring_readiness.py`
  (readiness gate), `dependency_gate.py` (dependency freshness).
* Milestone reconciliation in the Architect authoring/repair turns.

What did **not** exist: a final, plan-bound acceptance verdict at the
run-completion boundary. `COMPLETE` was reachable purely because every work order
was `COMPLETED` and the GitOps turn returned success — even with pending QA,
stale integration evidence or a plan that had changed after integration review.

### 2.4 TUI

A `:verify` command / verification matrix (`cli/tui/governance.py`) renders
per-dimension verification. No command exposed QA review state, final-review
state, blocking reasons or rework budgets, and nothing read review state from the
daemon.

---

## 3. Capability gap matrix

| # | Capability | Existing implementation | Evidence | Classification | Action taken |
|---|---|---|---|---|---|
| 1 | Worker completion verification | Deliverable-on-disk + declared-scope verification before `COMPLETED` | `_verify_work_order_deliverables`, `deliverable_verifier.verify_work_order_deliverables`, tests `test_sdp_fix_006*` | EXISTS_AND_ENFORCED | Reused unchanged; QA gate runs *after* it |
| 2 | Backend/frontend QA routing | QA agent role, defect-verdict routing, QA rework counters | `_role_for_agent`, `_find_qa_defect_verdicts`, `_route_qa_defect_to_developers`, `qa_reworks` | EXISTS_BUT_INCOMPLETE (reactive, not mandatory) | Extended: gate-driven QA dispatch for `backend`/`frontend` deliverables |
| 3 | QA verdict persistence | Text verdicts in `.sync/inbox/**`, `.sync/reviews/**`, `.sync/qa/verdicts` | `_get_qa_feedback`, `d024_gate` | EXISTS_BUT_INCOMPLETE (no revision identity) | Minimal JSON schema `stackmind.qa-verdict.v1` under `.sync/reviews/qa/`; reuses `durable_write_text` |
| 4 | QA failure + bounded rework | `qa_reworks` / `max_qa_reworks`, `_retry_worker`, recovery escalation | `_advance_executing`, `_handle_qa_evidence_gap` | EXISTS_AND_ENFORCED | Reused: gate FAIL verdicts feed the same counters, prompt builder and escalation |
| 5 | Architect plan reconciliation | Integration gate + milestone reconciliation | `integration_gate`, `plan.py`, `_advance_integration_review` | EXISTS_AND_ENFORCED | Reused; final review consumes `integration_evidence` instead of duplicating it |
| 6 | Architect final acceptance gate | None at `COMPLETE`; completion depended on `COMPLETED` + GitOps success | `_advance_gitops` success branch, `_advance_complete` | MISSING | Added `Phase.FINAL_REVIEW` + persisted `stackmind.final-review.v1`; enforced at `COMPLETE` and in the GitOps success branch |
| 7 | Restart-safe review state | `RunState` atomic persistence `run-<id>.yaml` | `to_dict`/`from_dict`/`load_run_state` | EXISTS_BUT_INCOMPLETE (no review fields) | Added review fields to `RunState` + verdict files on disk; nothing else persisted twice |
| 8 | TUI review visibility | `:verify` matrix only | `cli/tui/governance.py`, adapter `:verify` | EXISTS_BUT_INCOMPLETE | Added `run.reviews` RPC, `:reviews` command and review panel; daemon state is authoritative |
| 9 | Regression / E2E coverage | SDP-FIX-006/006A suites, lifecycle supervisor suite | `tests/test_sdp_fix_006*.py`, `test_lifecycle_supervisor.py` | EXISTS_BUT_INCOMPLETE (no gate coverage) | Added 21 focused tests + shared helpers; extended 9 existing suites to satisfy the new invariant |

---

## 4. Call-graph / state-transition analysis

### 4.1 Completion path after this change

```
worker turn (codex/gemini)
  └─ _poll_worker_turns → deliverable verification (unchanged, deterministic)
       └─ review_gate.requires_qa(ws, wo_id)
            ├─ False → mark COMPLETED (unchanged behaviour)
            └─ True  → review_gate.evaluate_qa_gate(ws, wo_id)
                 ├─ PASS (fresh, evidence-backed) → mark COMPLETED
                 └─ otherwise → _handle_qa_gate_pending
                      ├─ dispatch independent QA turn (role=qa, agent gemma,
                      │   no work_order_id → QA contract, not the WO contract)
                      ├─ FAIL verdict → archive verdict, bounded rework
                      │   (state.qa_reworks++ ≤ max_qa_reworks)
                      └─ budget exhausted → escalate_wo_to_recovery →
                          ARCHITECT_RECOVERY_DECISION
```

The worker never writes `.sync/reviews/**`: its contract does not authorise that
channel, and the QA turn is started without the work-order id so it runs under
the QA agent contract.

### 4.2 Final acceptance path

```
INTEGRATION_REVIEW (deterministic integration_gate passes)
  └─ Phase.FINAL_REVIEW
       ├─ review_gate.evaluate_final_acceptance(state, ws)
       │    plan approved + PLAN.md hash + all required WOs COMPLETED
       │    + QA gate passed for every applicable WO + no blocked WOs
       │    + no pending QA rework + integration evidence clean
       ├─ prerequisites unmet → bounded bounce to EXECUTING
       │    (final_prerequisite_rounds ≤ max_final_review_rounds + 2) → else BLOCKED
       ├─ dispatch Architect review turn → writes
       │    .sync/reviews/final/<run>.json (stackmind.final-review.v1)
       ├─ PASS + fresh → PRODUCT_READY → GITOPS
       └─ FAIL → archive verdict, invalidate QA verdicts of reworked WOs,
            bounded corrective round (final_review_rounds ≤ max_final_review_rounds)
            → EXECUTING; exhausted → WAITING_FOR_HUMAN (exit 3)

GITOPS success branch
  └─ _final_acceptance_status(state, ws)  ← authoritative invariant
       ├─ valid → COMPLETE
       └─ invalid → FINAL_REVIEW (never COMPLETE)

COMPLETE phase (re-entry / restart)
  └─ _advance_complete → _final_acceptance_status
       ├─ valid → COMPLETE
       └─ invalid → FINAL_REVIEW
```

Both boundaries call the *same* `_final_acceptance_status`, so there is one
definition of "accepted", not two.

### 4.3 Freshness model

No timestamp is used as proof:

* `subject_hash` = sha256 over the work-order definition (deliverables, criteria,
  tasks, dependencies) **and** the bytes of every declared deliverable.
* `plan_hash` = sha256 of `PLAN.md`.
* QA verdicts record `subject_hash`; final verdicts record `plan_hash` plus the
  `subject_hash` of every reviewed work order.
* On mismatch the verdict is rejected as stale; the QA verdict is archived with a
  `stale`/`rejected` suffix so it can never authorise acceptance again, and the
  final verdict is archived as `rejected`/`superseded`.

---

## 5. Duplicate-logic risks and how they were avoided

| Risk | Avoidance |
|---|---|
| Second verification framework | Gate calls the existing `deliverable_verifier`, `integration_gate` evidence and `d024_gate` verdict text; it adds no new file/test inspection logic |
| Second QA agent / dispatch pipeline | Reuses `manager.start_turn(role="qa", agent_id="gemma")` and the existing agent contract + permission matrix |
| Parallel state machine | Adds one `Phase.FINAL_REVIEW` to the existing enum and review fields to the existing `RunState`; no new store |
| Second source of truth | Verdicts live on disk under `.sync/reviews/**`; `RunState` keeps only pointers/counters. The TUI reads `run.reviews` RPC, never local state |
| Duplicate rework logic | FAIL verdicts feed the existing `qa_reworks` / `_retry_worker` / `escalate_wo_to_recovery` path |
| Duplicate deliverable hashing | Same sha256 convention as `snapshot.py` / `integration_gate.py` |
| Separate TUI view stack | Panel is added to `cli/tui/governance.py` next to the verification matrix and rendered through the existing command output path |

---

## 6. Implementation decisions

1. **Gate module** `validators/harness/review_gate.py` — pure, deterministic,
   path-in/path-out; importable by supervisor, daemon and tests without cycles.
2. **QA applicability** derived from existing roles/deliverable types
   (`backend`/`frontend` roles, or `code|module|api|component` deliverable types).
   QA, GitOps, architecture and `WO-000` bookkeeping work orders keep their
   previous treatment; documentation-only work still gets deterministic
   verification.
3. **New phase `FINAL_REVIEW`** inserted between `INTEGRATION_REVIEW` and
   `PRODUCT_READY`. It was the smallest way to give the Architect a real review
   turn at the right moment without reusing `INTEGRATION_REVIEW` (whose
   deterministic gate has different semantics).
4. **Invariant enforced twice on purpose** (GitOps success branch and `COMPLETE`
   handler): restart/re-entry paths must not be able to skip acceptance.
5. **Bounded loops**: `max_qa_review_attempts` (per-WO QA turn attempts),
   `max_qa_reworks` (existing), `max_final_review_rounds`,
   `final_prerequisite_rounds` cap. Exhaustion escalates with exit code 3; no
   unbounded Architect/QA loop.
6. **Plan changes**: the final verdict binds the plan hash. A changed `PLAN.md`
   invalidates acceptance. The approved plan is never silently replaced; plan
   approval remains in `validators/harness/plan.py` and the existing approval
   RPC/TUI flow.
7. **TUI**: `:reviews` → adapter → `DaemonClient.run_reviews` → `run.reviews` RPC
   → `review_gate.review_status(state, ws)`; nothing is computed TUI-side.

---

## 7. Compatibility / migration notes

* `RunState` fields are additive with defaults; old `run-*.yaml` files load
  unchanged (missing keys default to `{}` / `0` / `2`).
* No existing verdict text format was removed: `d024_gate` parsing and
  `qa_reworks` routing remain active. The new JSON verdict is an additional,
  authoritative record for the mandatory gate.
* Old runs continued after upgrade will require a final-review verdict before
  `COMPLETE`; that is the intended invariant, and the operator-visible reason is
  surfaced via `:reviews`.
* `Phase.FINAL_REVIEW` was added to the phase mirrors in `cli/tui/state.py` and
  the daemon's phase→status mapping (`manager.py`).
