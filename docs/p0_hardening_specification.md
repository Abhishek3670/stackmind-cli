# StackMind Core Hardening Specification — Phase 0

**Version:** 1.0.0 (Phase 0 Complete)  
**Status:** CLOSED & ACCEPTED (P0-1 through P0-6)  
**Host Substrate:** Windows 11 (Primary, Empirical Job Object Confinement) · POSIX (UNVERIFIED)

---

## 1. Executive Summary & Core Principles

StackMind is an autonomous multi-agent software engineering runtime enforcing deterministic, fail-closed supervisory control over untrusted LLM-driven code generation, mutation, testing, and deployment.

Phase 0 implements the bedrock security, lifecycle liveness, and workspace integrity controls:
1. **Authoritative State Outside Workspace:** Authoritative state lives strictly in supervisor memory with HMAC-SHA256 signed checkpoints stored outside the workspace boundary. Workspace files are never authoritative inputs.
2. **Process Tree Confinement:** All agent child processes, background commands, and compilers are spawned suspended and assigned immediately to Windows Job Objects with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and breakaway prevention (`JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` disabled).
3. **Filter Pipeline Bypass:** Snapshots and restores completely bypass Git's filter pipeline (`.gitattributes` eol, ident, filters), staging raw bytes via `hash-object -w --no-filters` and restoring directly from `cat-file blob <sha>`.
4. **Derived Commit Tree Binding:** Commits are built from a derived commit tree bounded by pre-turn baseline `.gitignore` rules, preventing accidental commitment of secrets (`.env`, credentials) or line-ending rewrites.
5. **Supervisor-Mediated GitOps & Pipe Delivery:** Git mutations (commit, tag, push) are executed solely by the supervisor. Push tokens reach the Git child strictly over an uninherited anonymous pipe, absent from argv and absent from the child's environment block.
6. **Deterministic Liveness & Resumable Terminal States:** Every state transition is bounded by monotonic deadlines, explicit retry caps, and non-increasing budgets across restarts. No resumable dead ends exist.

---

## 2. State Machine Transition Table

| Source Phase | Event / Trigger | Guard Conditions | Target Phase | Actions & Invariants |
|---|---|---|---|---|
| `INIT` | `start_run()` | Run initialized | `PLANNING` | Allocates run_id, initializes budget $B = 18 + 5N$, captures genesis HMAC checkpoint. |
| `PLANNING` | Architect turn completes | Proposed plan generated | `AWAITING_APPROVAL` | Creates plan record; stores plan in supervisor storage. |
| `PLANNING` | Planning turn times out | `planning_retries < max` | `PLANNING` | Increments `planning_retries` (does NOT touch `authoring_repair_attempts`), reaps process tree. |
| `PLANNING` | Planning retries exhausted | `planning_retries >= max` | `WAITING_FOR_HUMAN` | Sets `exit_code = 3` (EXIT_RETRIES_EXHAUSTED). |
| `AWAITING_APPROVAL` | Human operator approves | Explicit operator approval | `AUTHORING` | Enters Architecture Turn 2 for work order / contract generation. |
| `AWAITING_APPROVAL` | Human operator rejects | Explicit operator rejection | `FAILED` | Sets `exit_code = 4` (EXIT_PLAN_REJECTED); terminal. |
| `AUTHORING` | Architect turn completes | Artifacts present in `.sync` | `AUTHORING_READINESS_GATE` | Stages work order DAG and capability manifests. |
| `AUTHORING` | Authoring fails / empty | `authoring_repairs < max` | `ARCHITECT_REPAIR` | Increments `authoring_repair_attempts`. |
| `AUTHORING` | Authoring repairs exhausted | `authoring_repairs >= max`| `WAITING_FOR_HUMAN` | Sets `exit_code = 3` (EXIT_RETRIES_EXHAUSTED). |
| `AUTHORING_READINESS_GATE`| Readiness check passes | DAG acyclic, contracts valid | `DISPATCHING` | Pre-computes ready queue based on topological dependencies. |
| `AUTHORING_READINESS_GATE`| Readiness check fails/timeout| Validation failure | `ARCHITECT_REPAIR` | Records structural diagnostics for repair. |
| `ARCHITECT_REPAIR` | Repair turn completes | Work order artifacts fixed | `AUTHORING_READINESS_GATE` | Re-evaluates readiness gate. |
| `ARCHITECT_REPAIR` | Repair times out / exhausted | `authoring_repairs >= max`| `WAITING_FOR_HUMAN` | Sets `exit_code = 3` (EXIT_RETRIES_EXHAUSTED). |
| `DISPATCHING` | Ready work order found | Unblocked dependency in DAG | `EXECUTING` | Decrements global turn budget once upon dispatch. |
| `DISPATCHING` | All work orders complete | Dependency DAG empty | `GITOPS` | Prepares release candidates. |
| `DISPATCHING` | Dispatching times out | Turn stalls without dispatch | `ARCHITECT_RECOVERY_DECISION` | Escalates to architecture recovery. |
| `EXECUTING` | Worker turn completes | Operation status COMPLETED | `INTEGRATION_REVIEW` | Runs post-turn diff gate evaluation against contract. |
| `EXECUTING` | Worker turn times out | `worker_retries[wo] < max` | `EXECUTING` | Increments `worker_retries[wo]`, reaps processes. |
| `EXECUTING` | Worker retries exhausted | `worker_retries[wo] >= max`| `ARCHITECT_RECOVERY_DECISION` | Escalates WO to architecture recovery; blocks downstream. |
| `INTEGRATION_REVIEW` | QA approves diff | Diff gate passes, tests green | `DISPATCHING` / `PRODUCT_READY` | Issues HMAC supervisor approval receipt; marks WO complete. |
| `INTEGRATION_REVIEW` | QA rejects WO (reworkable) | `qa_reworks[wo] < max` | `EXECUTING` | Increments `qa_reworks[wo]`, isolates rework to target WO. |
| `INTEGRATION_REVIEW` | QA reworks exhausted | `qa_reworks[wo] >= max` | `ARCHITECT_RECOVERY_DECISION` | Escalates WO to architecture recovery. |
| `INTEGRATION_REVIEW` | Unidentifiable rejection | Empty reworkable list | `ARCHITECT_RECOVERY_DECISION` | Increments unidentifiable recovery counter; sets SYSTEM_UNIDENTIFIABLE. |
| `ARCHITECT_RECOVERY_DECISION`| Recovery amendment issued | Contract amended | `DISPATCHING` / `EXECUTING` | Resumes blocked WOs after DAG amendment. |
| `ARCHITECT_RECOVERY_DECISION`| Recovery attempts exhausted| `attempts >= max_retries` | `WAITING_FOR_HUMAN` | Sets `exit_code = 3` (EXIT_RETRIES_EXHAUSTED). |
| `GITOPS` | GitOps promotion completes | Release commit/tag created | `COMPLETE` | Sets `exit_code = 0` (EXIT_SUCCESS); terminal. |
| `GITOPS` | GitOps times out / rejects | `gitops_retries < max` | `GITOPS` | Increments `gitops_retries`. |
| `GITOPS` | GitOps retries exhausted | `gitops_retries >= max` | `WAITING_FOR_HUMAN` | Sets `exit_code = 3` (EXIT_RETRIES_EXHAUSTED). |
| `WAITING_FOR_HUMAN` | Headless timeout fires | `now - entered_at > ci_wait`| `FAILED` | Reaps processes, creates rescue ref & crash bundle; `exit_code = 137`. |
| `WAITING_FOR_HUMAN` | Human override resume | `human_resumes < max` | Prior Interrupted Phase | Increments `human_resumes_used`, logs override event. |
| `WAITING_FOR_HUMAN` | Resumes exhausted | `human_resumes >= max` | `FAILED` | Terminal; prevents unresumable dead ends; `exit_code = 1`. |
| Any Phase | Total wall-clock timeout | `now - started > ci_run` | `FAILED` | Hard kill; creates rescue ref & crash bundle; `exit_code = 137`. |
| Any Phase | Integrity violation detected| Tampering / ADS / forgery | `FAILED` | Hard kill; `exit_code = 5` (EXIT_INTEGRITY_VIOLATION). |

---

## 3. Timeout Configuration Table

All phase timeouts are enforced via monotonic deadlines (`time.monotonic()`) in memory, strictly preventing clock-skew vulnerabilities.

| Phase / State | Config Dict Key | Environment Variable | Default Duration | Target Phase on Timeout | Counter Consumed | Reaping Behavior |
|---|---|---|---|---|---|---|
| `PLANNING` | `Phase.PLANNING` / `"planning"` | `SUPERVISOR_TIMEOUT_PLANNING` | 300.0s | `PLANNING` (or `WAITING_FOR_HUMAN` if cap) | `planning_retries` | Job Object process tree killed |
| `AUTHORING` | `Phase.AUTHORING` / `"authoring"` | `SUPERVISOR_TIMEOUT_AUTHORING` | 600.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Job Object process tree killed |
| `AUTHORING_READINESS_GATE` | `Phase.AUTHORING_READINESS_GATE` | `SUPERVISOR_TIMEOUT_AUTHORING_READINESS_GATE` | 60.0s | `ARCHITECT_REPAIR` | None (deterministic gate) | N/A (Internal supervisor) |
| `DISPATCHING` | `Phase.DISPATCHING` / `"dispatching"` | `SUPERVISOR_TIMEOUT_DISPATCHING` | 60.0s | `ARCHITECT_RECOVERY_DECISION` | None (escalation gate) | N/A (Internal supervisor) |
| `EXECUTING` | `Phase.EXECUTING` / `"executing"` | `SUPERVISOR_TIMEOUT_EXECUTING` | 600.0s | `EXECUTING` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `worker_retries[wo_id]` | Job Object process tree killed |
| `INTEGRATION_REVIEW` (QA) | `Phase.INTEGRATION_REVIEW` | `SUPERVISOR_TIMEOUT_INTEGRATION_REVIEW` | 600.0s | `INTEGRATION_REVIEW` (or `ARCHITECT_RECOVERY_DECISION`) | `qa_reworks[wo_id]` | Job Object process tree killed |
| `ARCHITECT_REPAIR` | `Phase.ARCHITECT_REPAIR` | `SUPERVISOR_TIMEOUT_ARCHITECT_REPAIR` | 600.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Job Object process tree killed |
| `ARCHITECT_RECOVERY_DECISION` | `Phase.ARCHITECT_RECOVERY_DECISION`| `SUPERVISOR_TIMEOUT_ARCHITECT_RECOVERY_DECISION`| 300.0s | `ARCHITECT_RECOVERY_DECISION` (or `WAITING_FOR_HUMAN`)| `recovery_attempts[wo_id]` | Job Object process tree killed |
| `GITOPS` | `Phase.GITOPS` / `"gitops"` | `SUPERVISOR_TIMEOUT_GITOPS` | 300.0s | `GITOPS` (or `WAITING_FOR_HUMAN` if cap) | `gitops_retries` | Job Object process tree killed |
| Human Wait Timeout (Headless)| `"ci_wait_timeout"` | `SUPERVISOR_CI_WAIT_TIMEOUT` | 60.0s | `FAILED` | None (terminal exit 137) | Job Object killed; rescue ref captured |
| Total Run Timeout (Whole-Run)| `"ci_run_timeout"` | `SUPERVISOR_CI_RUN_TIMEOUT` | 1800.0s | `FAILED` | None (terminal exit 137) | Job Object killed; rescue ref captured |

---

## 4. Counter & Budget Table

| Metric / Counter | Scope | Initial / Formula | Max Cap | Consumption Event | Escalation Rule on Exhaustion |
|---|---|---|---|---|---|
| `global_turn_budget` | Global Run | $B = 18 + 5N$ ($N$ = WOs) | Decrement only | Each turn dispatched | Transitions to `WAITING_FOR_HUMAN`, exit code 2. Zero double-charge. |
| `planning_retries` | Phase (Planning) | 0 | 2 | Timeout or invalid output | Transitions to `WAITING_FOR_HUMAN`, exit code 3. Isolated from authoring. |
| `authoring_repair_attempts`| Phase (Authoring) | 0 | 2 | Empty plan or repair timeout | Transitions to `WAITING_FOR_HUMAN`, exit code 3. |
| `worker_retries[wo_id]` | Per Work Order | 0 | 2 | Worker timeout or execution error | Escalates WO to `ARCHITECT_RECOVERY_DECISION`; blocks dependents. |
| `qa_reworks[wo_id]` | Per Work Order | 0 | 2 | QA rejection of turn output | Escalates WO to `ARCHITECT_RECOVERY_DECISION`; blocks dependents. |
| `recovery_attempts[wo_id]` | Per Recovery Target| 0 | 3 | Recovery timeout or rejection | Transitions to `WAITING_FOR_HUMAN`, exit code 3. |
| `gitops_retries` | Phase (GitOps) | 0 | 2 | Promotion timeout or rejection | Transitions to `WAITING_FOR_HUMAN`, exit code 3. |
| `stall_ticks` ($C_{stall}$) | Run-wide | 0 | 10 consecutive ticks | No phase change, no turn dispatched | Transitions to `WAITING_FOR_HUMAN`, exit code 3 (idle stall). |
| `daemon_restarts` | Run-wide | 0 | 2 | Run re-attestation after restart | Fails closed to `FAILED`, exit code 3. Crash-loops cannot refill budget. |
| `human_resumes_used` | Run-wide | 0 | 2 | Explicit human resume override | Fails closed to `FAILED`, exit code 1. Resumes are strictly bounded. |

---

## 5. Exit-Code Table

The following exit codes are immutable, published contracts:

| Code | Constant Name | Phase / Trigger | Semantic Meaning |
|---|---|---|---|
| `0` | `EXIT_SUCCESS` | `Phase.COMPLETE` | Run completed successfully; all work orders merged, tested, and released. |
| `1` | `EXIT_FAILURE` | `Phase.FAILED` | Unrecoverable failure, operator abort, or human resumes exhausted. |
| `2` | `EXIT_BUDGET_EXHAUSTED` | `Phase.WAITING_FOR_HUMAN` | Global turn budget exhausted ($B \le 0$). Requires human override. |
| `3` | `EXIT_RETRIES_EXHAUSTED`| `Phase.WAITING_FOR_HUMAN` / `FAILED` | Retry counter exceeded, daemon restart cap reached, or idle stall ($C_{stall}$). |
| `4` | `EXIT_PLAN_REJECTED` | `Phase.FAILED` | Human operator rejected proposed architecture plan. |
| `5` | `EXIT_INTEGRITY_VIOLATION`| `Phase.FAILED` | HMAC signature mismatch, .git internals tampering, or ADS violation. |
| `137`| `EXIT_HEADLESS_TIMEOUT` | `Phase.FAILED` | Headless human wait timeout (`ci_wait_timeout`) or total run timeout exceeded. |

---

## 6. Threat Model & Security Controls (P0-1 through P0-6)

### 6.1 External Store & Filter Pipeline Bypass (P0-1 / P0-2)
- **Threat:** A malicious workspace child writes `.gitattributes` containing `* text eol=crlf`, `ident`, or custom filters (`filter=clean/smudge`), causing checkout or restore to tamper with bytes or execute code.
- **Defense:** Snapshots stage blobs via `hash-object -w --no-filters` and `update-index --add --cacheinfo`. Restore writes blobs directly via `cat-file blob <sha>`, completely bypassing Git's working-tree filter pipeline.
- **External Store Isolation:** All snapshots and rescue refs are stored in a dedicated bare repository outside the workspace root (`storage_root`). Tampering with store configs, hooks, or alternates is detected via pre/post metadata hashing.

### 6.2 Job Object Process Containment (P0-2)
- **Threat:** Agents spawn background child processes or daemons that persist across turns, writing unauthorized files or planting symlinks/junctions.
- **Defense:** All child processes are spawned suspended (`CREATE_SUSPENDED`), assigned to a Windows Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and no breakaway, and assigned `DETACHED_PROCESS` so they cannot access the supervisor console. Before any post-turn diff or restore, `ProcessReaper.reap_all()` terminates the entire job tree.

### 6.3 Derived Commit Tree Binding (P0-3 / P0-4)
- **Threat:** Committing directly from the raw snapshot tree includes `.env` secrets, build logs, and changes line endings.
- **Defense:** Computes a derived commit tree:
  1. Path set = tracked files at HEAD + new untracked files not matching the pre-turn baseline `.gitignore`.
  2. Modes = inherited from HEAD for existing files (preserving 100755); 100644 for new regular files.
  3. Content = raw approved snapshot bytes normalized via repository attributes.
  4. The approved tree hash is computed over exactly this derived set.

### 6.4 Ephemeral Push Token Delivery (Task 1a)
- **Threat:** Git push credentials exposed on argv (`ps`, `GetCommandLineW`) or in the child process environment block (`PEB->ProcessParameters->Environment`).
- **Defense:**
  1. Supervisor generates an anonymous pipe (`os.pipe()`).
  2. Token is written directly to the pipe's kernel memory buffer and the write handle is closed.
  3. Git is configured with `-c credential.helper` invoking a lightweight helper that reads the read handle.
  4. Verified via `NtQueryInformationProcess`: token is 100% absent from `CommandLine` and `Environment`.
  5. Token is zeroed in supervisor memory immediately upon completion.
  6. Phase 1 memory caveat: Same-user memory inspection is an accepted risk until low-integrity AppContainer agent confinement (P1-1) lands.

### 6.5 Inter-Turn Persistent Services Blame Isolation
- **Threat:** A background service writes to the workspace between turns, evading turn diff gating if the next turn takes a fresh pre-turn snapshot.
- **Defense:** Turn diff evaluation evaluates against the *previous post-turn snapshot*, not a fresh pre-turn snapshot. Any inter-turn modification is immediately flagged as an unapproved diff violation and reverted.

### 6.6 Daemon Admin IPC & Console Confirmation
- **Threat:** Sandboxed child processes or unauthenticated localhost connections trigger administrative actions (resume, budget increase, plan rejection).
- **Defense:**
  1. Daemon binds strictly to `127.0.0.1` (loopback only).
  2. Administrative RPC methods require HMAC token authentication.
  3. In headless mode (`SUPERVISOR_HEADLESS=1` / `CI=1` / non-tty), console confirmation is strictly disabled and fails closed.
  4. In interactive mode, prompts on daemon's own console (`sys.stdin`), which is completely detached from sandboxed child processes.

---

## 7. Phase 1 Risk Register (Open & Deferred Items)

The following items are explicitly tracked as **OPEN** and deferred to Phase 1:

| ID | Risk Description | Proposed Phase 1 Mitigation | Current Status |
|---|---|---|---|
| **P1-1** | Same-user child memory inspection (`ReadProcessMemory` against supervisor) | Low-integrity AppContainer token confinement | **OPEN (Accepted Risk in P0)** |
| **P1-2** | Network egress policy enforcement at process boundary | Windows Filtering Platform (WFP) egress firewall rules | **OPEN (Scheduled for P1)** |
| **P1-3** | Tool output escaping against LLM prompt injection | Structured schema validation & delimiter neutralization | **OPEN (Scheduled for P1)** |
| **P1-4** | POSIX substrate parity for Job Object containment | cgroups v2 / Linux pid namespace isolation | **UNVERIFIED (Windows 11 primary)** |
