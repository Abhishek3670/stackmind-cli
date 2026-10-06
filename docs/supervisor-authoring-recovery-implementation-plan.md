# Implementation Plan: Authoring Failure Recovery and Contract-Safe Dispatch

## Objective

Make the governed delivery lifecycle fail closed when Architect authoring fails, and route recoverable worker failures back to the Architect with durable evidence. The supervisor remains responsible for scheduling and deterministic validation; the Architect remains responsible for business work-order design, contract changes, task splitting, and recovery decisions.

This plan addresses two observed behaviors:

1. An invalid YAML payload from the Architect is rejected by `AuthoringGate`, but the supervisor then synthesizes generic child work orders and dispatches them.
2. Synthesized contracts use a fixed `max_files_touched: 10`, which can contradict the actual size of a scaffold task and cause an otherwise valid worker execution to be blocked.

## Verified Current Flow

| Concern | Current source | Current behavior |
| --- | --- | --- |
| YAML validation | `validators/harness/authoring_gate.py` | Rejects invalid governed-artifact YAML before write. Correct fail-closed behavior. |
| Authoring failure | `validators/kernel/daemon/supervisor.py::_advance_authoring` | If no authored work orders exist, calls `synthesize_child_work_orders()` and continues to dispatch. |
| Fallback contracts | `validators/kernel/daemon/authoring.py::_build_contract` | Generates broad scope and a fixed `max_files_touched: 10`. |
| Worker enforcement | `validators/harness/contract_gate.py::verify_post_execution` | Blocks on the observed scratch-workspace diff when changed-file count exceeds the contract limit. Correct enforcement. |

## Non-Goals

- Do not weaken `AuthoringGate` or allow malformed YAML to persist.
- Do not let workers edit their own contracts, work orders, or budgets.
- Do not auto-increase scope or file budgets after a worker failure.
- Do not change the existing QA-before-GitOps requirement.
- Do not remove deterministic fallback authoring utilities; restrict when they can be used.

## Target Lifecycle

```text
PLAN_APPROVED
  -> AUTHORING
  -> AUTHORING_READINESS_GATE
       pass -> DISPATCHING -> EXECUTING -> QA -> downstream dependency
       fail -> ARCHITECT_REPAIR

EXECUTING
  -> verified worker blocker
  -> ARCHITECT_RECOVERY_DECISION
       retry unchanged contract -> DISPATCHING
       amend/split work order -> ARCHITECT_REPAIR
       needs product/risk authority -> WAITING_FOR_HUMAN
       no safe recovery -> BLOCKED
```

The supervisor may make the transition and assemble evidence. It must not invent or publish replacement business work orders after an Architect authoring failure.

## Work Breakdown

### 1. Add an atomic authoring readiness gate

Create a pure validator, for example `validators/harness/authoring_readiness.py`, that receives the workspace and expected plan milestones. It must verify:

- each expected implementation milestone has exactly one work order and matching contract;
- all governed YAML parses and passes the existing schema/semantic validation;
- every work order has an assigned permitted worker role;
- contract `work_order` and `agent_id` match the work order;
- dependency IDs exist and are acyclic;
- each work order has a deliverable path/type;
- contract scope authorizes its deliverable path;
- budget is positive and explicitly present;
- no duplicate work-order IDs or orphan contracts are present.

Return a structured result, not an exception string:

```python
@dataclass(frozen=True)
class AuthoringReadinessResult:
    ready: bool
    plan_id: str
    artifact_paths: tuple[str, ...]
    issues: tuple[ReadinessIssue, ...]
```

`ReadinessIssue` should include a stable code, affected WO ID/path, message, and remediation category (`yaml`, `schema`, `coverage`, `dependency`, `budget`, or `scope`).

### 2. Change supervisor authoring failure semantics

Update `RunState` and `_advance_authoring()` in `validators/kernel/daemon/supervisor.py`.

- When the authoring operation is failed or blocked, transition to `ARCHITECT_REPAIR`; do not call `synthesize_child_work_orders()`.
- When the authoring operation reports completion, invoke the readiness gate before `DISPATCHING`.
- If the readiness gate fails, persist its structured evidence and transition to `ARCHITECT_REPAIR`.
- Dispatch a dedicated Architect repair turn containing only bounded diagnostics and the affected artifact IDs/paths.
- Maintain an `authoring_revision` or recovery-attempt counter to prevent an endless repair loop.
- At the retry limit, transition to `WAITING_FOR_HUMAN` or terminal `BLOCKED` with the stored evidence packet.

Do not re-use the original broad authoring prompt verbatim. The repair prompt must state what failed and ask the Architect to correct only the affected governed artifacts.

### 3. Make work-order publication atomic

Architect tool writes should remain validated per file by `AuthoringGate`, but they must not make a partially authored set dispatchable.

- Mark newly authored child artifacts as pending for the current authoring revision.
- Only mark the revision publishable after the readiness gate passes.
- The supervisor dispatches only artifacts belonging to the latest publishable revision.
- Preserve rejected artifacts and their parser/schema diagnostics for audit, preferably in the attempt workspace or a governed report path rather than silently overwriting them.

This avoids the current ambiguity where one malformed artifact and several valid files can lead to a mixed, partially synthesized task graph.

### 4. Replace fixed contract budget generation with plan-derived estimates

Refactor `validators/kernel/daemon/authoring.py::_build_contract` so it does not unconditionally emit `max_files_touched: 10`.

Add explicit plan/work-order fields:

```yaml
implementation_estimate:
  expected_files:
    - requirements.txt
    - src/app.py
  max_files_touched: 2
  rationale: "Dependency manifest and application entrypoint."
```

Rules:

- The Architect authors `max_files_touched` based on the task's expected file set.
- The readiness gate checks that `max_files_touched >= len(expected_files)`.
- A scaffold task that creates more than a small, bounded set of files must be split into multiple WOs instead of receiving an unbounded budget.
- A contract amendment after execution requires an Architect recovery decision and creates a new contract revision; workers cannot mutate the prior revision.
- Keep a safe default only for explicitly designated bootstrap/internal WOs, never for a failed Architect authoring turn.

### 5. Preserve full worker-blocker evidence

Update `AgentRunner` and the supervisor handoff path to retain a complete failure packet when `verify_post_execution()` blocks.

Required fields:

- work-order ID, contract revision/hash, and attempt ID;
- `observed_files` in stable sorted order;
- declared `modified_files` and the declaration mismatch, when relevant;
- file budget and observed count;
- scope violations, command results, test results, and validation diagnostics;
- a stable `failure_code` such as `CONTRACT_FILE_BUDGET_EXCEEDED`.

Persist this before discarding the scratch workspace. The user and Architect must be able to see the exact eleven files, not only `11 > 10`.

### 6. Introduce a recovery decision contract

Architect repair output should be machine-readable. Add a schema, for example `schemas/recovery-decision.schema.json`:

```json
{
  "work_order": "WO-001",
  "action": "retry_unchanged | amend_contract | split_work_order | create_dependency_work_order | escalate_human | terminal_block",
  "reason": "...",
  "replacement_work_orders": [],
  "contract_revision": 2
}
```

Enforcement rules:

- `retry_unchanged` is permitted only for transient failures and unchanged contract hash.
- `amend_contract` requires a replacement contract revision and readiness revalidation.
- `split_work_order` supersedes the original WO and creates dependency-safe children.
- `escalate_human` is required for material scope, security, or destructive-operation authority changes.
- the supervisor validates and applies the decision; it does not infer it.

### 7. Normalize duplicate diagnostics

The UI currently repeats the same budget reason because multiple fields are joined. Create one canonical failure code/message and deduplicate the display list by `(failure_code, work_order_id, contract_revision)`.

Do not remove raw evidence; keep it nested under the canonical error instead.

## Test Plan

Add focused tests before changing broad end-to-end fixtures.

1. Invalid Architect YAML
   - Feed malformed content containing `Completed task via tools` to `write_file`.
   - Assert `AuthoringGate` rejects it.
   - Assert no child WOs/contracts are dispatched or synthesized.
   - Assert run transitions to `ARCHITECT_REPAIR` with a YAML diagnostic.

2. Incomplete artifact set
   - Create a valid WO without its contract.
   - Assert readiness gate fails and dispatch does not begin.

3. Valid complete artifact set
   - Create matching WOs/contracts with valid dependencies.
   - Assert readiness gate passes and the supervisor dispatches only dependency-ready WOs.

4. File-budget breach
   - Execute a fixture that changes eleven files under a ten-file contract.
   - Assert the result includes all eleven normalized paths and failure code `CONTRACT_FILE_BUDGET_EXCEEDED`.
   - Assert the supervisor creates an Architect recovery task, not an automatic same-contract worker retry.

5. Architect split decision
   - Supply a valid recovery decision that splits one scaffold WO into two.
   - Assert original WO is superseded, child dependencies are correct, and contracts pass readiness validation.

6. Retry policy
   - Assert only explicitly classified transient failures can use `retry_unchanged`.
   - Assert budget/scope/schema failures cannot auto-retry.

7. Regression tests
   - Preserve existing `AuthoringGate`, contract gate, D024, QA, and worker role-segregation tests.
   - Run the existing lifecycle supervisor suite plus new recovery tests.

## Implementation Sequence

1. Add data types, readiness validator, and unit tests.
2. Add `ARCHITECT_REPAIR` transition and prevent fallback synthesis on authoring failure.
3. Wire readiness validation into `AUTHORING -> DISPATCHING`.
4. Add durable worker-blocker evidence packet.
5. Add recovery-decision schema and supervisor application logic.
6. Refactor synthesized/default budget behavior and add plan-derived estimates.
7. Update TUI state/rendering for canonical recovery status and deduplicated diagnostics.
8. Run focused tests, then the full test suite and `stackmind validate .`.

## Acceptance Criteria

- A malformed architect YAML write cannot dispatch any worker work order.
- A failed authoring operation never triggers automatic generic WO synthesis for product work.
- Every dispatched WO has a validated matching contract and explicit budget.
- A file-budget blocker retains the exact observed file list after scratch cleanup.
- Scope/budget/schema failures route to an Architect recovery decision, not automatic worker retry.
- Only transient failures retry unchanged work.
- QA remains mandatory before downstream release/GitOps progression.
- The UI shows one canonical blocker with linked evidence rather than repeated strings.

## Files Expected to Change

- `validators/kernel/daemon/supervisor.py`
- `validators/kernel/daemon/authoring.py`
- `validators/harness/runner.py`
- `validators/harness/contract_gate.py`
- `validators/harness/authoring_gate.py` (only if readiness metadata needs an extension)
- `validators/kernel/daemon/manager.py`
- `cli/tui/state.py`
- `cli/tui/governance.py`
- `schemas/recovery-decision.schema.json` (new)
- `validators/harness/authoring_readiness.py` (new)
- focused tests in `tests/`

## Review Questions for the Implementing Agent

1. Should contract amendments be versioned beside the original (`WO-001.r2.yaml`) or stored as revision history inside one governed artifact?
2. Is deterministic work-order synthesis still needed for explicit bootstrap mode? If yes, require an explicit operator/CEO-approved `bootstrap_mode` flag and keep it unavailable after an Architect failure.
3. Which report location is canonical for failed scratch-attempt evidence while preserving the current `.sync` write restrictions?
