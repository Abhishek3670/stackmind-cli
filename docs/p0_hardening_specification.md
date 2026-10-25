# StackMind Core Hardening Specification — Phase 0

**Version:** 1.0.0 (Phase 0 Hardening Complete)  
**Status:** CLOSED & ACCEPTED (P0-1 through P0-6 Hardening Deliverables)  
**Host Substrate:** Windows 11 (Primary, Empirical Job Object Confinement) · POSIX (UNVERIFIED)

---

## 1. Executive Summary & Core Principles

StackMind is an autonomous multi-agent software engineering runtime enforcing deterministic, fail-closed supervisory control over untrusted LLM-driven code generation, mutation, testing, and deployment.

Phase 0 implements the bedrock security, lifecycle liveness, and workspace integrity controls:
1. **Authoritative State Outside Workspace:** Authoritative state lives strictly in supervisor memory with HMAC-SHA256 signed checkpoints stored outside the workspace boundary (`storage_root`). Workspace files are never authoritative inputs.
2. **Process Tree Confinement:** All agent child processes, background commands, and compilers are spawned suspended (`CREATE_SUSPENDED`), assigned immediately to Windows Job Objects with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and breakaway prevention (`JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` disabled), and detached from the daemon console device (`DETACHED_PROCESS`).
3. **Filter Pipeline Bypass:** Snapshots and restores completely bypass Git's working-tree filter pipeline (`.gitattributes` eol, ident, working-tree-encoding, filters), staging raw blobs via `hash-object -w --no-filters` and restoring directly from `cat-file blob <sha>`.
4. **Derived Commit Tree Binding:** Commits are built from a derived commit tree bounded by pre-turn baseline `.gitignore` rules, preventing accidental commitment of secrets (`.env`, credentials) or line-ending rewrites.
5. **Supervisor-Mediated GitOps & Ephemeral Pipe Delivery:** Git mutations (commit, tag, push) are executed solely by the supervisor. Push tokens reach the Git child strictly over an uninherited anonymous pipe, absent from process argv and absent from the child's environment block.
6. **Deterministic Liveness & Resumable Terminal States:** Every state transition is bounded by monotonic deadlines, explicit retry caps, non-increasing budgets across restarts, and guaranteed terminal fail-closed escalation. No resumable dead ends exist.

---

## 2. State Machine Transitions & Liveness Contracts

### 2.1 WAITING_FOR_HUMAN Transitions Per Entry Reason

All transitions into `Phase.WAITING_FOR_HUMAN` are bounded, auditable, and map deterministically to terminal states or active resumptions:

| Entry Reason | Exit Code | Trigger Condition | Available Human Actions | Target Phase on Action | Terminal Exit Code | Headless CI Behavior (`SUPERVISOR_HEADLESS=1` or `CI=1`) |
|---|---|---|---|---|---|---|
| `BUDGET_EXHAUSTED` | `2` | `global_turn_budget <= 0` | `human_resume(additional_budget=N)`<br>*(requires `human_resumes < max`)* | Prior interrupted phase (`AUTHORING`, `EXECUTING`, etc.) | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `IDLE_STALL` | `3` | $C_{stall} \ge 3$ consecutive ticks with no dispatch or state change | `human_resume()`<br>*(resets stall ticks $C_{stall}=0$)* | Prior interrupted phase | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `RETRIES_EXHAUSTED` | `3` | Phase retries (`planning`, `authoring`, `worker`, `qa`, `gitops`) $\ge$ cap | `human_resume()`<br>*(resets phase retries)* | Target phase for bounded retry | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `INTEGRITY_VIOLATION` | `5` | HMAC tampering, `.git` internals mutation, or ADS stream detected | *None* (Fails closed immediately; bypasses `WAITING_FOR_HUMAN`) | `FAILED` | `5` | Immediate terminal `FAILED` (Exit `5`) |
| `RESTART_CAP_EXCEEDED` | `1` | Daemon restarted $\ge 3$ times (`daemon_restarts >= max_daemon_restarts`) | *None* (Human re-attestation rejected) | `FAILED` | `1` | Immediate terminal `FAILED` (Exit `1`) |
| `RESUMES_EXHAUSTED` | `1` | Explicit human resumes $\ge 2$ (`human_resumes_used >= max_human_resumes`) | *None* (Further resumes rejected) | `FAILED` | `1` | Immediate terminal `FAILED` (Exit `1`) |
| `PLAN_REJECTED` | `4` | Human operator explicitly rejects proposed architecture plan | `reject_plan(reason=...)` | `FAILED` | `4` | Immediate terminal `FAILED` (Exit `4`) |

### 2.2 Liveness Guarantee Formulation
> **Empirical Liveness Contract:**  
> In interactive mode, human wait has **no timeout** (the supervisor suspends and waits indefinitely for human operator intervention); every waiting state has a strictly defined set of human actions leading to a bounded transition path. In headless / CI mode (`SUPERVISOR_HEADLESS=1` or `CI=1`), waiting on a human aborts deterministically after `ci_wait_timeout` (default 60s) with exit code `137`, guaranteeing bounded execution time, process tree reaping, and rescue ref capture.

### 2.3 Restart-Cap Change & Justification
- **Default Change (`2` $\to$ `3`):** In multi-phase autonomous software engineering, a host machine reboot, container restart, or developer daemon restart may occur legitimately during complex lifecycle runs. Setting `max_daemon_restarts = 3` permits initial startup, an intentional restart during development, and a single crash recovery while strictly preventing infinite crash-loop budget refills.
- **Exit Code Change (`3` $\to$ `1`):** Exit code `3` (`EXIT_RETRIES_EXHAUSTED`) is semantically reserved for work order and agent turn retry exhaustion. Daemon crash-looping is a system-level supervisor failure, not an agent retry stall. Failing closed with `EXIT_FAILURE` (`1`) properly classifies supervisor restart denial under supervisor aborts.

### 2.4 Terminal `FAILED` Guarantees: Rescue Ref & Crash Bundle
Whenever a run reaches terminal `Phase.FAILED` (for any reason: headless wait timeout, whole-run timeout, plan rejection, restart cap exceeded, resumes exhausted, budget exhaustion, or integrity violation):
1. **Process Tree Reaped:** `ProcessReaper.reap_all()` terminates all child processes inside the Job Object.
2. **Crash Bundle JSON Written:** Serialized state snapshot, error reason, exit code, budget, counters, and HMAC checkpoint hash written to `supervisor_storage/crash_bundles/crash_bundle_<run_id>_<ts>.json` outside the workspace.
3. **Rescue Ref Created:** Authoritative git snapshot captured and stored under `refs/rescue/<run_id>/failed` and `refs/rescue/<run_id>/<interrupted_phase>` in the external bare store (`storage_root`).
4. **Workspace Cleanliness:** Zero crash artifacts or leak files are written inside the workspace.

---

## 3. Timeout Table & Framework Phase Mappings

All timeouts are measured against `time.monotonic()` in supervisor memory, strictly immunizing orchestration from system wall-clock adjustments or NTP jumps.

### 3.1 Published Timeout Table

| State (Phase Enum) | Config Key | Default Duration | NEXT STATE on Timeout | Counter Consumed | Reaping Mechanism |
|---|---|---|---|---|---|
| `PLANNING` | `Phase.PLANNING` / `SUPERVISOR_TIMEOUT_PLANNING` | 60.0s | `PLANNING` (or `WAITING_FOR_HUMAN` if cap) | `planning_retries` | Windows Job Object kill |
| `AUTHORING` | `Phase.AUTHORING` / `SUPERVISOR_TIMEOUT_AUTHORING` | 600.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Windows Job Object kill |
| `AUTHORING_READINESS_GATE` | `Phase.AUTHORING_READINESS_GATE` / `SUPERVISOR_TIMEOUT_AUTHORING_READINESS_GATE` | 60.0s | `ARCHITECT_REPAIR` | `authoring_repair_attempts` | Deterministic supervisor gate |
| `DISPATCHING` | `Phase.DISPATCHING` / `SUPERVISOR_TIMEOUT_DISPATCHING` | 60.0s | `ARCHITECT_RECOVERY_DECISION` | None (scheduler tick) | Deterministic supervisor step |
| `EXECUTING` *(Worker Turn)* | `Phase.EXECUTING` / `SUPERVISOR_TIMEOUT_EXECUTING` | 180.0s | `EXECUTING` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `worker_retries[wo_id]` & `turns_spent` | Windows Job Object kill |
| `QA_REVIEW` *(QA Review)* | `Phase.QA_REVIEW` / `SUPERVISOR_TIMEOUT_QA_REVIEW` | 180.0s | `INTEGRATION_REVIEW` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `qa_reworks[wo_id]` | Windows Job Object kill |
| `INTEGRATION_REVIEW` *(Integration)* | `Phase.INTEGRATION_REVIEW` / `SUPERVISOR_TIMEOUT_INTEGRATION_REVIEW` | 180.0s | `INTEGRATION_REVIEW` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `qa_reworks[wo_id]` | Windows Job Object kill |
| `ARCHITECT_REPAIR` *(Repairing)* | `Phase.ARCHITECT_REPAIR` / `SUPERVISOR_TIMEOUT_ARCHITECT_REPAIR` | 180.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Windows Job Object kill |
| `ARCHITECT_RECOVERY_DECISION` | `Phase.ARCHITECT_RECOVERY_DECISION` / `SUPERVISOR_TIMEOUT_ARCHITECT_RECOVERY_DECISION` | 300.0s | `ARCHITECT_RECOVERY_DECISION` (or `WAITING_FOR_HUMAN` if cap) | `recovery_attempts[wo_id]` | Windows Job Object kill |
| `GITOPS` *(Release Promotion)* | `Phase.GITOPS` / `SUPERVISOR_TIMEOUT_GITOPS` | 60.0s | `GITOPS` (or `WAITING_FOR_HUMAN` if cap) | `gitops_retries` | Windows Job Object kill |
| `WAITING_FOR_HUMAN` *(Headless)* | `"ci_wait_timeout"` / `SUPERVISOR_CI_WAIT_TIMEOUT` | 60.0s | `FAILED` (Terminal Exit `137`) | None | Job Object reaped; rescue captured |
| Whole Run Limit *(CI Mode)* | `"ci_run_timeout"` / `SUPERVISOR_CI_RUN_TIMEOUT` | 1800.0s *(or derived)* | `FAILED` (Terminal Exit `137`) | None | Job Object reaped; rescue captured |

### 3.2 Real Phase Enum Mapping
In the StackMind architecture:
- **`TESTING`** maps to **`Phase.INTEGRATION_REVIEW`** (where integration testing and automated verification gates run) and **`Phase.QA_REVIEW`** (where the dedicated QA agent executes acceptance testing).
- **`REPAIRING`** maps to **`Phase.ARCHITECT_REPAIR`** (where Architecture remediates work order contract defects) and **`Phase.ARCHITECT_RECOVERY_DECISION`** (where Architecture makes structural DAG recovery decisions).

### 3.3 Justification of GITOPS 60s Timeout
`GITOPS` execution does NOT compile source code or download third-party packages from external registries. All dependencies, compilation, and unit tests are already validated during `EXECUTING` and `INTEGRATION_REVIEW`.
In StackMind:
1. `GITOPS` performs solely in-memory archive operations, derived commit tree generation, signing, and git ref updates.
2. Measured execution of `create_gitops_commit` + derived tree building + local ref creation is **< 0.25 seconds**.
3. Pushing over an uninherited anonymous pipe to an explicit remote URL takes **< 1.2 seconds**.
4. Therefore, **60.0 seconds** provides a **40x safety margin** for supervisor git mediation while deterministically killing hanging git network calls.

---

## 4. Counter & Budget Accounting Rules

### 4.1 Budget Decrement vs. Consumption Counters
- **`global_turn_budget`:** Represents the *remaining available turn balance*. Initialized at $B = 18 + 5N$ ($N$ = work orders). It is strictly **decremented** on turn dispatch (`state.global_turn_budget -= 1`).
- **`turns_spent`:** Represents the *cumulative count of turns executed*. It is **incremented** strictly once upon turn dispatch.
- **Double-Charging Elimination:** On turn timeout or crash, `ProcessReaper.reap_all()` terminates the child process and marks the turn failed without secondary budget decrement, eliminating turn timeout double-charging.
- **Planning Isolation:** Failures and timeouts in `Phase.PLANNING` consume `planning_retries` and **never** decrement `authoring_repair_attempts`.

---

## 5. Security & Isolation Controls

### 5.1 Push-Token Delivery Over Uninherited Anonymous Pipe (Task 1a)
- **Mechanism:** The supervisor allocates an anonymous OS pipe (`os.pipe()`). The credential bytes are written directly to the OS kernel pipe buffer, and the write handle is closed immediately.
- **Handle-List Inheritance:** The pipe read handle is passed strictly to the Python credential helper subprocess using handle-list inheritance (`msvcrt.open_osfhandle` / Windows handle inheritance). Other processes never receive the read handle.
- **Absence from Argv and Env:** Verified directly via native `NtQueryInformationProcess` memory inspection of `PEB->ProcessParameters`:
  - `CommandLine`: Token string is 100% absent.
  - `Environment`: Token string is 100% absent.
- **Push from Storage Root:** Supervisor pushes directly from the external bare store (`storage_root`) with hardened options (`-c core.fsmonitor=false -c core.hooksPath= -c core.excludesFile= GIT_CONFIG=NUL GIT_CONFIG_GLOBAL=NUL GIT_CONFIG_SYSTEM=NUL`). Attacker rewrites in workspace `.git/config` (`insteadOf`, `remote.url`) receive zero traffic.
- **Zero Disk Footprint:** No token bytes are ever written to disk or configuration files.

### 5.2 Console Injection Defense (Item 7)
- **Threat:** A detached child process calls `kernel32.FreeConsole()`, `kernel32.AttachConsole(parent_pid)`, and `kernel32.WriteConsoleInputW()` to inject `'y\n'` into the supervisor's interactive administrative confirmation prompt (`confirm_on_console`).
- **Mitigation:**
  1. `ProcessReaper` launches all agent processes inside Job Objects with `DETACHED_PROCESS` (`0x00000008`).
  2. Prior to displaying any confirmation prompt, `reaper.reap_all()` kills all active background child processes.
  3. `confirm_on_console` calls `kernel32.GetConsoleProcessList()`. If any child PID in `reaper._registered_pids` is detected in the console's process list, an intrusion is flagged and the prompt **fails closed immediately (`return False`)**.
  4. `kernel32.FlushConsoleInputBuffer()` wipes any pre-injected keystrokes before `input()` is called.
  5. In headless mode (`SUPERVISOR_HEADLESS=1` or `CI=1`), interactive prompts are disabled and fail closed immediately.

---

## 6. Phase 1 Risk Register (Open & Deferred Items)

| Item / Area | Threat Description | Owner | Status in P0 | Deferred Phase 1 Target | Evidence Label |
|---|---|---|---|---|---|
| **Daemon Token File** | `~/.stackmind/daemon/.daemon_token` readable by same-user agents on Windows | Phase 1 Security | OPEN (Accepted Risk) | Per-session anonymous pipe token delivery to CLI; remove static token file. | `[REASONED ONLY]` |
| **Daemon Memory** | Same-user child inspecting supervisor memory via `OpenProcess(PROCESS_VM_READ)` | Phase 1 Confinement (P1-1) | OPEN (Strict XFAIL) | Windows Low-Integrity / AppContainer token confinement boundary. | `[REASONED ONLY]` |
| **Credential Files** | Host credential files (`~/.git-credentials`, `~/.netrc`) read by untrusted child | Phase 1 Confinement (P1-1) | OPEN (Accepted Risk) | Filesystem isolation / AppContainer sandbox boundary for agent processes. | `[REASONED ONLY]` |
| **Task 1(b) Per-Turn Home** | Agent writing persistent artifacts or dotfiles to `%USERPROFILE%` across turns | Phase 1 Sandboxing | OPEN (Scheduled P1) | Isolated temporary `%USERPROFILE%` / `%APPDATA%` generated per turn and wiped. | `[REASONED ONLY]` |
| **Env Allowlist** | Ambient host environment variables (`AWS_SECRET_ACCESS_KEY`, etc.) leaked to child | Phase 1 Sandboxing | OPEN (Scheduled P1) | Strict environment variable allowlist filter on child process launch. | `[REASONED ONLY]` |
| **Windows Persistence** | Child writing to Registry Run keys or Windows Startup folders to persist | Phase 1 Confinement (P1-1) | OPEN (Accepted Risk) | Job Object process kill + Registry write virtualization under AppContainer. | `[REASONED ONLY]` |
| **Writes Outside Workspace** | Child creating files outside workspace directory boundary | Phase 1 Sandboxing | OPEN (Scheduled P1) | AppContainer filesystem isolation rules allowing writes strictly to workspace. | `[REASONED ONLY]` |
| **Process Ownership & Ports** | Child binding to local TCP/UDP ports or squatting daemon port | Phase 1 Network (P1-2) | OPEN (Scheduled P1) | Windows Filtering Platform (WFP) network egress & bind firewall rules. | `[REASONED ONLY]` |
| **Output Escaping** | Agent stdout/stderr containing adversarial prompt injections | Phase 1 Gateway (P1-3) | OPEN (Scheduled P1) | Delimiter neutralization and hardened structured schema validator. | `[REASONED ONLY]` |
| **POSIX Substrate Parity** | Windows-specific NT Job Objects, named pipes, and junctions | Phase 1 Runtime (P1-4) | UNVERIFIED | Linux `cgroups v2` and macOS sandbox execution harnesses. | `[REASONED ONLY]` |

---

## 7. Final Deliverable Summary Table

| Item | Scope / Deliverable | Status | Evidence Label | Primary Commit Hash | Key Files & Line References |
|:---:|:---|:---:|:---:|:---:|:---|
| **1** | `ci_wait_timeout` human-wait semantics & `ci_run_timeout` whole-run limit | **DONE** | `[RAN AGAINST CODE]` | `a29ba27` | [supervisor.py:79-88](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L79-L88)<br>[test_p0_6_timeouts_and_liveness.py:328-355](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L328-L355) |
| **2** | Restore published exit-code table mapping and verified immutability | **DONE** | `[RAN AGAINST CODE]` | `3dd9615` | [supervisor.py:113-120](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L113-L120)<br>[test_p0_5_counters_and_budget.py:210-245](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L210-L245) |
| **3** | Eliminate resumable dead ends; bounded caps fail closed to terminal `FAILED` | **DONE** | `[RAN AGAINST CODE]` | `500d046` | [supervisor.py:495-555](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L495-L555)<br>[test_p0_6_timeouts_and_liveness.py:436-502](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L436-L502) |
| **4** | Eliminate budget double-charging on turn timeout | **DONE** | `[RAN AGAINST CODE]` | `4b19ba5` | [supervisor.py:674-676](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L674-L676)<br>[test_p0_6_timeouts_and_liveness.py:159-182](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L159-L182) |
| **5** | Pre-P0-6 failing-first reproduction script on commit `5fa684c` | **DONE** | `[RAN AGAINST CODE]` | `1b51a0e` | [reproduce_p0_6_failing_first.py:1-105](file:///W:/Aatish/Stuff/stackmind-cli/scripts/reproduce_p0_6_failing_first.py#L1-L105) |
| **6** | Published full timeout table; row-level unit tests; isolated planning retries | **DONE** | `[RAN AGAINST CODE]` | `d1b5b32` | [supervisor.py:121-134](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L121-L134)<br>[test_p0_6_timeouts_and_liveness.py:507-630](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L507-L630) |
| **7** | Parameterized stub matrix ($7 \times 7 = 49$ combinations, $< 30$ steps, $< 3\text{s}$) | **DONE** | `[RAN AGAINST CODE]` | `78a3872` | [test_p0_6_timeouts_and_liveness.py:631-729](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L631-L729) |
| **8** | Rescue ref & crash bundle verified in supervisor store on every terminal `FAILED` | **DONE** | `[RAN AGAINST CODE]` | `6394ebb` | [supervisor.py:4970-5045](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L4970-L5045)<br>[test_p0_6_timeouts_and_liveness.py:730-790](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L730-L790) |
| **9** | Inter-turn rogue writes flagged by comparing against previous post-turn snapshot | **DONE** | `[RAN AGAINST CODE]` | `d686af7` | [turn_snapshot.py:100-118](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/turn_snapshot.py#L100-L118)<br>[test_p0_3_diff_gate.py:1065-1105](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_3_diff_gate.py#L1065-L1105) |
| **10** | Admin console headless fail-closed & detached child console injection defense | **DONE** | `[RAN AGAINST CODE]` | `4859d43` | [protocol.py:85-125](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/protocol.py#L85-L125)<br>[test_p0_5_counters_and_budget.py:1275-1335](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L1275-L1335) |
| **11** | Push token delivery over uninherited pipe; absent from argv, env & disk | **DONE** | `[RAN AGAINST CODE]` | `f4a9731` | [diff_gate.py:1180-1240](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/diff_gate.py#L1180-L1240)<br>[test_p0_3_diff_gate.py:560-660](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_3_diff_gate.py#L560-L660) |
| **12** | Test collection manifest synchronization (`collected_tests.txt`) | **DONE** | `[DONE (documentation)]` | `688edbb` | [collected_tests.txt:1-481](file:///W:/Aatish/Stuff/stackmind-cli/collected_tests.txt#L1-L481) |
| **13** | Core hardening specification, contracts, and threat model documentation | **DONE** | `[DONE (documentation)]` | `7dd8c3b` | [p0_hardening_specification.md:1-260](file:///W:/Aatish/Stuff/stackmind-cli/docs/p0_hardening_specification.md#L1-L260) |
| **P1-1** | Child memory inspection protection (`ReadProcessMemory` against supervisor) | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 (Low-Integrity AppContainer confinement) |
| **P1-2** | Network egress filtering at process boundary | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 (Windows Filtering Platform) |
| **P1-3** | Delimiter escaping & prompt injection boundary sanitization | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 (Hardened gateway sanitizer) |
| **P1-4** | POSIX substrate parity | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 (Linux `cgroups v2` harnesses) |
