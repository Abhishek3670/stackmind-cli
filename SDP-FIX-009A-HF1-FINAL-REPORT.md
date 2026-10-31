# SDP-FIX-009A-HF1 — Final Report

Cancellation Outcome and Architecture Authorization Integrity

Date: 2026-10-31
Scope: two narrowly scoped hardening defects. No TUI rewrite, no orchestration
architecture change, no governance control weakened, no commit/stash/reset.

---

## 1. Root cause of both defects

### DEFECT 1 — Cancellation communication failure marked operations FAILED

`cli/tui/commands.py`, `_stream_turn_execution()` `except KeyboardInterrupt`
handler. The handler equated a failed cancellation *request* with a failed
*operation*:

```python
elif cancel_err is not None:
    op_status = "FAILED"
```

The exception only proved that the cancellation RPC did not complete. It said
nothing about whether the operation failed or was still running. The fabricated
status was then written into `state.operations[op_id].status` and every RUNNING
role state, so the rest of the TUI (status views, dispatch guards, restart
reconstruction from session/journal) treated the operation as failed without
any authoritative daemon evidence.

Two secondary defects existed in the same block:

- When `cancel_resp` was not a dict and no exception occurred, the code fell
  through to `op_status = "CANCELLED"`, claiming a confirmed cancellation that
  the daemon never confirmed.
- Terminal daemon states (`RUNNING`, `COMPLETED`, `CANCEL_OUTCOME_UNKNOWN`) had
  no representation, so the client could not preserve the last authoritative
  operation state.

### DEFECT 2 — Architecture `.sync/**` deny exemption was too broad

`validators/harness/contract_validation.py` (and the equivalent live gate in
`validators/harness/contract_gate.py`), post-execution deny check:

```python
is_arch = (
    getattr(task, "is_authoring", False)
    or getattr(task, "role", "") == "architecture"
    or getattr(contract, "role", "") == "architecture"
    or (contract.data.get("identity", {}).get("role") == "architecture")
    or agent == "architecture"
)
if is_arch and (p_norm in (".sync/**", ".sync/*", ".sync") or p_norm.startswith(".sync/")):
    continue
```

Three independent flaws:

1. Any one of five weak signals granted Architecture privilege. An authoring
   flag, a `task.role` string, contract metadata, or an agent-name string was
   enough. Worker impersonation through a string field was possible.
2. `p_norm.startswith(".sync/")` matched *every* deny rule under `.sync`, not
   only the intended contract-repair channel. `.sync/policies/`,
   `.sync/governance/`, `.sync/work-orders/`, and `.sync/` root were all
   excludable.
3. The file path being modified was never checked against the exemption, so the
   exemption applied to arbitrary paths whenever a deny rule happened to start
   with `.sync/`.

`contract_gate.py` is the gate actually imported by
`validators/harness/runner/execution.py` and `runner/verification.py`; leaving
the duplicate logic there intact would have kept the live bypass. Both gates
now use one shared, fail-closed helper.

---

## 2. Exact files and functions changed

| File | Change |
|---|---|
| `cli/tui/commands.py` | Added `CANCEL_REQUESTED`, `CANCEL_OUTCOME_UNKNOWN`, `CancellationOutcome`, `resolve_cancellation_outcome()`, `apply_cancellation_outcome()`, `cancellation_message()`, `reconcile_pending_cancellations()`, `unresolved_cancellation_operations()`, `_extract_operation_status()`, `_classify_cancel_status()`, `_reconcile_operation_record()`. Replaced the `except KeyboardInterrupt` status-inference block in `_stream_turn_execution()`. Added reconciliation to `reconnect_and_sync()`. Added the unresolved-outcome dispatch guard in `dispatch_delivery_command()` before `adapter.command(...)`. |
| `cli/tui/state.py` | `AutonomousDeliveryState.__init__`: added `cancel_outcomes: dict[str, dict[str, Any]]`. Authoritative `OperationNode.status` semantics are unchanged. |
| `validators/harness/authoring_role.py` | Added `ARCHITECTURE_ROLES`, `PERMITTED_CONTRACT_REPAIR_ACTIONS`, `normalize_rel_path()`, `is_contract_repair_path()`, `is_sync_deny_pattern()`, `load_repair_authorization()`, `architecture_contract_repair_exempt()`. Uses the existing `is_authorized()` role authority and the existing supervisor recovery-decision channel; no second authorization system. |
| `validators/harness/contract_gate.py` | Replaced the broad `is_arch` exemption with `is_sync_deny_pattern(...) and architecture_contract_repair_exempt(...)`. Imports the shared helper. |
| `validators/harness/contract_validation.py` | Same replacement; imports the shared helper. |
| `tests/test_sdp_fix_009a_tui_interrupt_safety.py` | Replaced `test_17_failed_cancellation_request_sets_failed_status` (which asserted the defect) with `test_17_failed_cancellation_request_never_marks_operation_failed`. |
| `tests/test_sdp_fix_009a_hf1_integrity.py` | New focused adversarial suite (35 tests). |

No changes were made to `validators/kernel/daemon/manager.py`, the supervisor,
or the orchestration architecture. Note: `contract_gate.py` was in scope
because it is the same duplicated defect on the live runner path; patching only
`contract_validation.py` would have left the exploit operative.

---

## 3. Cancellation state transition model

The cancellation result and the operation status are separate fields.
`state.cancel_outcomes[op_id]` carries the typed outcome;
`state.operations[op_id].status` carries only authoritative operation state.

| Condition | `CancellationOutcome.outcome` | `OperationNode.status` |
|---|---|---|
| Cancellation request accepted (`CANCEL_REQUESTED`) | `CANCEL_REQUESTED` | `CANCEL_REQUESTED` |
| Daemon confirms cancellation (`CANCELLED`) | `CANCELLED` | `CANCELLED` |
| Daemon confirms execution failure (`FAILED`) | `FAILED` | `FAILED` |
| Cancellation communication fails and daemon is unreachable | `CANCEL_OUTCOME_UNKNOWN` | last authoritative status preserved |
| Cancellation communication fails, daemon says still running | `RUNNING` | `RUNNING` preserved |
| Cancellation communication fails, daemon says completed | `COMPLETED` | `COMPLETED` preserved |
| Ambiguous/empty acknowledgement and unreachable daemon | `CANCEL_OUTCOME_UNKNOWN` | last authoritative status preserved |

Invariants enforced:

- A `cancel_err` can never produce `FAILED`; only `operation_get()` returning
  `FAILED` does.
- `CANCELLED` is only reported on an authoritative daemon response.
- `CANCEL_OUTCOME_UNKNOWN` and `RUNNING` are unresolved outcomes: role states
  and operation status are not overwritten, and no new turn can be dispatched.
- Reconciliation is attempted immediately on failure and again on
  reconnect/restart and before any new dispatch. A previously acknowledged
  `CANCEL_REQUESTED` is preserved if the daemon later becomes unreachable; it is
  never silently downgraded.
- No duplicate operation is created. `dispatch_delivery_command()` calls
  `unresolved_cancellation_operations()` before submitting a turn, reconciles,
  and refuses with `[CANCEL PENDING] ... no duplicate turn was dispatched` when
  the original may still be active.
- Operation identity, run/session identity, work-order identity, audit activity
  entries, and attempt counters are untouched by the failure path.

---

## 4. Authorization and path-scope model

`architecture_contract_repair_exempt()` returns `True` only when **all** hold:

1. **Trusted role authorization.** `is_authorized(agent)` must pass (worker
   roles `codex`, `gemini`, `local-llm`, `gemma` are rejected outright) and the
   normalized agent must be one of `claude`, `architecture`, `architect`. CEO,
   admin, and system do not inherit this exemption.
2. **Explicitly authorized authoring task.** `task.is_authoring` must be true,
   the task must name a well-formed work order (no `.`, `/`, `\`), and the
   loaded contract's `work_order` must match it. The authoring flag alone is
   insufficient.
3. **Traceable provenance.** `load_repair_authorization()` reads the existing
   supervisor-written `.sync/decisions/recovery/<WO>.decision.json`, requires a
   matching `work_order` and an action in `amend_contract`,
   `split_work_order`, `create_dependency_work_order`, `architect_fix`, or
   `resolve_blocker`. Missing, malformed, mismatched, or non-repair decisions
   fail closed.
4. **Exact path scope.** `is_contract_repair_path()` accepts only
   `.sync/contracts/<name>.yaml|yml` — exactly three segments, no empty/`.`/`..`
   segment, no hidden or extension-only filename.

The exemption only skips matching **deny** rules. The contract's allow scope,
budget, write-mode, expiration, peer-ownership, D024, and D025 checks run
unchanged. `.sync/`, `.sync/policies/`, `.sync/governance/`,
`.sync/work-orders/`, nested contract directories, and every non-contract path
receive no exemption.

---

## 5. Adversarial test inventory

`tests/test_sdp_fix_009a_hf1_integrity.py` (35 tests):

Cancellation (14):

1. `test_cancellation_request_timeout_is_unknown_not_failed`
2. `test_connection_failure_before_daemon_acknowledgement`
3. `test_connection_loss_after_daemon_acknowledgement_keeps_cancel_requested`
4. `test_operation_still_running_after_failed_cancellation_request`
5. `test_daemon_confirms_cancellation`
6. `test_daemon_confirms_execution_failure`
7. `test_daemon_confirms_successful_completion_is_preserved`
8. `test_restart_reconciliation_reconstructs_authoritative_state`
9. `test_restart_reconciliation_preserves_running_when_daemon_unreachable`
10. `test_repeated_cancellation_requests_preserve_accounting_and_escalate_safely`
11. `test_no_duplicate_worker_dispatch_while_outcome_unknown`
12. `test_unknown_outcome_does_not_create_an_operation_node`
13. `test_17_failed_cancellation_request_never_marks_operation_failed` (existing 009A file, replaced)
14. Existing `test_14/15/16/18` cancellation tests retained and still pass.

Authorization (21, some parameterized):

15. `test_authorized_architecture_repair_can_modify_contract_yaml`
16. `test_authorized_repair_also_passes_the_contract_validation_gate`
17. `test_architecture_cannot_modify_unrelated_sync_paths` (policies, governance, work-orders, `.sync/INDEX.yaml`)
18. `test_workers_remain_denied_even_with_authoring_and_provenance` (codex, gemini, local-llm, gitops)
19. `test_worker_cannot_impersonate_architecture_through_string_fields`
20. `test_authoring_flag_alone_cannot_grant_exemption`
21. `test_missing_or_invalid_provenance_fails_closed` (malformed JSON, wrong WO, `escalate_human`, `terminal_block`)
22. `test_non_authoring_task_cannot_use_exemption`
23. `test_path_traversal_and_normalization_cannot_escape_contracts` (`..`, `./`, double slash, nested, absolute)
24. `test_existing_deny_rules_remain_effective_outside_exemption`

Failure injection is deterministic: `TimeoutError`, `ConnectionError`, mock
`operation_get` records, and on-disk decision fixtures. No live provider or
daemon is required.

---

## 6. Exact test commands and actual results

All run from the project root on Windows (bash), Python `pytest`.

```bash
python -m pytest tests/test_sdp_fix_009a_hf1_integrity.py tests/test_sdp_fix_009a_tui_interrupt_safety.py -q
# 68 passed in 1.14s   (35 HF1 + 33 SDP-FIX-009A)  -> exit 0
```

```bash
python -m pytest tests/test_architect_contract_repair_deadlock.py tests/test_contract_deny_precedence.py \
  tests/test_p0_3_diff_gate.py tests/test_worker_autonomy_and_architect_authority.py \
  tests/test_phase0_trust_and_verification.py tests/test_artifact_completion_verification.py -q
# 90 passed in 88.79s  -> exit 0
```

Pre-existing failures, reproduced identically on a clean `HEAD` worktree:

```bash
python -m pytest tests/test_harness_contract.py -q
# 2 failed, 5 passed  (outcome_verified declaration failures, not scope/deny)
python -m pytest tests/test_authoring_compiler.py -q
# 4 failed, 18 passed (normalized_paths expectations)
```

Baseline confirmation (`git worktree add --detach .hf1-baseline HEAD`):

```bash
python -m pytest tests/test_harness_contract.py tests/test_authoring_compiler.py -q
# HEAD: 6 failed, 23 passed  — identical failure set
```

Full TUI suite:

```bash
python -m pytest tests/test_tui_*.py -q
# 5 failed, 497 passed in 194.27s  -> exit 1 (5 pre-existing, see §7)
```

008A–008D, lifecycle, daemon, operation tree, subagent, recovery:

```bash
python -m pytest tests/test_sdp_fix_008*.py tests/test_lifecycle_supervisor.py \
  tests/test_architect_chat_history.py tests/test_authoring_recovery.py \
  tests/test_daemon_runtime.py tests/test_operation_tree.py tests/test_subagent_orchestration.py -q
# 11 failed, 562 passed in 37.03s  -> exit 1 (10 pre-existing, 1 pre-existing working-tree cause)
```

Isolation check for the only ambiguous failure:

```bash
# HEAD + only the five HF1 source files copied in:
python -m pytest "tests/test_authoring_recovery.py::TestRecoveryDecisions::test_split_decision_supersedes_original_and_readies_children" -q
# 1 passed  -> HF1 edits are not the cause
# My full tree: 1 failed (Phase.DISPATCHING vs EXECUTING), caused by the
# pre-existing uncommitted supervisor modules already in the working tree.
```

---

## 7. Full regression results

| Suite | Passed | Failed | Classification |
|---|---|---|---|
| HF1 + SDP-FIX-009A interrupt safety | 68 | 0 | Green |
| Contract repair, deny precedence, diff gate, authorization, trust, verification | 90 | 0 | Green |
| `test_harness_contract.py` | 5 | 2 | Pre-existing on HEAD (identical) |
| `test_authoring_compiler.py` | 18 | 4 | Pre-existing on HEAD (identical) |
| All `test_tui_*.py` | 497 | 5 | Pre-existing on HEAD (identical) |
| 008A–008D + lifecycle + daemon + operation tree + subagent + recovery | 562 | 11 | 10 pre-existing on HEAD; 1 attributable to pre-existing supervisor working-tree changes (HF1-only tree passes) |

Pre-existing TUI failures (all present on `HEAD`):
`test_interactive_delivery_loop_scroll_and_ctrl_l_preserve_layout`,
`test_tui_startup_in_alternate_buffer_renders_single_status_bar_and_composer`,
`test_post_turn_settle_redraw_maintains_atomic_frame_and_cursor`,
`test_tui_repl_interactive_session_execution`,
`test_subsequent_turn_actions_do_not_leak_prior_turn_actions`.

Pre-existing daemon/tree failures (all present on `HEAD`): two
`test_daemon_runtime` cancellation tests, four `test_operation_tree` cascade
tests, four `test_subagent_orchestration` agent-cancel tests. These stem from
`SessionManager.cancel_operation()` treating an operation with no live thread
as already `CANCELLED`, which predates HF1 and was not modified.

No failures were introduced by this change.

---

## 8. Remaining limitations

1. **Pre-existing suite failures remain.** They are outside HF1's scope and
   were confirmed to fail identically on `HEAD`; none are on a path modified by
   HF1. They are reported, not hidden or weakened.
2. **One recovery test is blocked by pre-existing supervisor changes.** The
   uncommitted supervisor modules already in the working tree change the
   `ARCHITECT_REPAIR → EXECUTING` transition. `HEAD + HF1` passes; `HEAD +
   pre-existing supervisor changes` does not. HF1 did not touch that code.
3. **Allow-override interaction is unchanged.** A contract whose own allow rule
   is strictly narrower than its `.sync/**` deny can still use the pre-existing
   allow-override path. HF1 narrows the *exemption*, not the allow-precedence
   contract semantics, because changing that would alter governance behavior
   the request forbade weakening.
4. **Provenance is only as strong as the supervisor-written decision file.** If
   a worker can write `.sync/decisions/recovery/*.decision.json`, it could
   forge provenance; that directory is already protected by the deny rules and
   diff gate, and HF1 does not add a second authority system.
5. **Restart reconciliation depends on reconnection.** Unknown outcomes are
   reconciled on `reconnect_and_sync()` and before new dispatch. If the TUI is
   killed and restarted offline, the outcome remains explicitly unknown (never
   FAILED) until the daemon is reachable.
6. **No live daemon/provider was exercised.** Tests use deterministic mocks and
   on-disk fixtures, as permitted. Cancellation timing against a real provider
   remains a runtime property, not a unit-test guarantee.

---

## 9. Working-tree status

Preserved. No reset, stash, discard, checkout, commit, amend, rebase, or push
was performed. Two temporary `git worktree` checkouts were created for
baseline comparison and removed with `git worktree remove --force`.

HF1-touched files:

```
 M cli/tui/commands.py
 M cli/tui/state.py
 M validators/harness/authoring_role.py
 M validators/harness/contract_gate.py
 M validators/harness/contract_validation.py
 M tests/test_sdp_fix_009a_tui_interrupt_safety.py
?? tests/test_sdp_fix_009a_hf1_integrity.py
?? SDP-FIX-009A-HF1-FINAL-REPORT.md
```

Pre-existing modifications and untracked files remain exactly as found
(supervisor modules, `cli/tui/app.py`, `cli/tui/chat.py`,
`validators/kernel/daemon/manager.py`, `_baseline_copy/`, the 008C/008D test
files, and the user's zip/chat artifacts). They were not touched.

Definition of done: cancellation communication failure never marks FAILED;
unknown outcomes reconcile and block duplicate dispatch; the exemption requires
trusted Architecture authorization plus provenance and exact
`.sync/contracts/` paths; workers and impersonation are denied; targeted and
relevant regression suites pass with only pre-existing failures remaining.
