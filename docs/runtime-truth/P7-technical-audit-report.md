# P7 Technical Audit Report

**Work order:** WO-025  
**Auditor:** Codex (Backend Lead)  
**Date:** 2026-09-12  
**Scope:** P7 delivery code and its associated regression tests; source code was read-only under the WO-025 contract.

## Executive result

The targeted P7 regression suite passed (**76 passed in 19.81s**) in the project `.venv` (Python 3.11.9, pytest 9.1.1). Core lifecycle, cancellation, contract-gate, daemon, TUI, and fault-injection behaviours covered by those tests are functioning as exercised.

The complete `pytest -q` suite did not complete: it made no progress after 105 tests (about 18% of the reported suite) and was stopped after several minutes. This is a release blocker under the project environment policy: a whole-suite pass must be obtained or the hanging test isolated before claiming P7 regression completion.

The code review found three implementation risks that need separate bug-fix work orders before a production/security sign-off.

## Verification record

| Check | Result | Evidence |
| --- | --- | --- |
| Designated interpreter | PASS | `.venv\\Scripts\\python.exe`, Python 3.11.9 |
| Test runner | PASS | pytest 9.1.1 from the designated venv |
| Targeted P7 regression | PASS | `76 passed in 19.81s` |
| Full regression | BLOCKED | `pytest -q` stalled after 105 tests with no completion summary; partial output retained in `docs/runtime-truth/.wo-025-full-pytest.out` |
| Memory benchmark | NOT AVAILABLE | pytest produced no peak-memory metric; no benchmark framework was configured for the audited run |

Targeted command:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_p7_headless_integration.py tests/test_p7_security_fault_injection.py tests/test_operation_tree.py tests/test_harness.py tests/test_harness_contract.py tests/test_harness_cancellation.py tests/test_backend_abstraction.py tests/test_subagent_orchestration.py tests/test_daemon_turn.py tests/test_daemon_streaming.py tests/test_daemon_runtime.py tests/test_daemon_protocol.py
```

## Confirmed strengths

- `SessionManager` persists sessions, journals, role bindings, and events through `DaemonStorage`; recovered records rebuild active cancellation events. Parent/child links, child-result aggregation, cascade cancellation, and prevention of parent completion while a child remains active are implemented in `validators/kernel/daemon/manager.py`.
- The role-normalization fix is present: `_run_turn()` maps logical role identifiers through `_ROLE_TO_PRIMARY_AGENT` before constructing `AgentRunner`, preventing logical-role citizenship failures during runtime updates.
- `AgentRunner.run_once()` supports an ad-hoc `prompt` when work-order discovery returns no task by constructing an in-memory `HarnessTask`; it does not write an ad-hoc task file merely to execute the prompt.
- Contract gates run before model execution and again against the observed workspace diff. D025 command classification is also applied before harness-issued shell commands.
- The Ollama path constructs a POST request to `/api/generate`, uses JSON with `stream: false`, identifies itself as `StackMind-CLI/3.3`, and enforces at least a 300-second HTTP timeout.
- TUI startup uses `workspace/.sync/runtime/daemon` as durable daemon state, supporting state reuse across local daemon/TUI restarts.
- Credential sanitation, output scanning/redaction, D025 classification, scope narrowing, and deterministic fault-injection helpers are present and exercised by the targeted suite.

## Findings requiring follow-up

### P7-AUD-01 — Backend registry is not thread-safe (High)

`BackendRegistry` in `validators/harness/backend.py` mutates and iterates the shared `_backends` dictionary without a lock. Concurrent `register`, `unregister`, `get`, or `list_backends` calls can race; dictionary mutation during `list_backends()` can raise or expose a partially updated registry. This conflicts with WO-020's thread-safety requirement.

**Recommended work order:** guard registry operations with an `RLock` and return a stable snapshot from `list_backends`; add a concurrent register/unregister/list regression test.

### P7-AUD-02 — Live model endpoint failures are converted to successful completions (High)

`ModelExecutionBackend.complete()` catches every exception from the `/api/generate` call and appends a notice to `report_markdown`, then returns a normal completion record. Thus connection refusal, malformed JSON, and HTTP errors can be represented as a completed (or work-order-deferred) execution rather than `BackendUnavailableError`/`BackendExecutionError`. The runner consequently cannot reliably classify a live Ollama failure as failed.

**Recommended work order:** convert transport/HTTP/JSON failures to typed backend errors after sanitizing their messages; retain only an explicitly configured offline/mock mode for synthetic completion. Add tests for refused connection, HTTP 5xx, invalid JSON, and timeout.

### P7-AUD-03 — Claimed 6D gate is not implemented as six independent checks (Medium)

The run path in `validators/harness/runner.py` creates `VerificationDimensions` using `scope_verified`, `state_verified`, `code_verified`, `behavioral_verified`, `security_verified`, and `outcome_verified`. It does not independently perform or record the documented Structural, Replay, Canary, Schema, Scope, and Budget checks. Several values are assigned from local bookkeeping (`True` or completion status), so the audit record is not evidence that the named verification dimensions ran.

**Recommended work order:** define and execute each named dimension explicitly, persist its evidence/result, and make write-back contingent on all six passing.

## Additional observations

- The current adapter layer provides a generic agent backend and generic model/Ollama backend; it does not contain distinct live Gemini, OpenAI, Claude, and Local adapter implementations. If those names are a promised P7 deliverable rather than role bindings, the release baseline should clarify the gap.
- `_run_turn()` retries `runner.run_once()` without the prompt after any `TypeError`. This preserves compatibility with legacy runner signatures, but it can also hide a genuine internal `TypeError` and potentially invoke a runner twice. Narrow the fallback to a signature-capability check or a clearly identified unsupported-keyword error.
- The `OperationJournal` utility is append-only in memory and correctly rejects duplicate operation IDs; durable operation reconstruction is implemented separately by the daemon manager's stored journal.

## Conclusion

P7 targeted behaviour is substantially covered and currently green, including the requested post-completion fixes. Do not mark the audit fully approved until the complete-suite hang is isolated and resolved, and until the three findings above are dispositioned through explicit bug-fix work orders or accepted risk decisions.
