# StackMind Core Hardening Specification — Phase 0

**Version:** 1.0.0 (Phase 0 Hardening Complete)  
**Status:** CLOSED & ACCEPTED (P0-1 through P0-6 Hardening Deliverables Verified)  
**Host Substrate:** Windows 11 (Primary, Empirical Job Object Confinement) · POSIX (UNVERIFIED)

---

## 1. Executive Summary & Core Principles

StackMind is an autonomous multi-agent software engineering runtime enforcing deterministic, fail-closed supervisory control over untrusted LLM-driven code generation, mutation, testing, and deployment.

Phase 0 establishes the bedrock security, lifecycle liveness, and workspace integrity controls:
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
| `BUDGET_EXHAUSTED` | `4` (`EXIT_BUDGET_EXHAUSTED`) | `global_turn_budget <= 0` | `human_resume(additional_budget=N)`<br>*(requires `human_resumes < max`)* | Prior interrupted phase (`AUTHORING`, `EXECUTING`, etc.) | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `IDLE_STALL` | `3` (`EXIT_RETRIES_EXHAUSTED`) | $C_{stall} \ge 3$ consecutive ticks with no dispatch or state change | `human_resume()`<br>*(resets stall ticks $C_{stall}=0$)* | Prior interrupted phase | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `RETRIES_EXHAUSTED` | `3` (`EXIT_RETRIES_EXHAUSTED`) | Phase retries (`planning`, `authoring`, `worker`, `qa`, `gitops`) $\ge$ cap | `human_resume()`<br>*(resets phase retries)* | Target phase for bounded retry | N/A (Resumed) | Aborts on `ci_wait_timeout` $\to$ terminal `FAILED` (Exit `137`) |
| `INTEGRITY_VIOLATION` | `6` (`EXIT_INTEGRITY_VIOLATION`) | HMAC tampering, `.git` internals mutation, or ADS stream detected | *None* (Fails closed immediately; bypasses `WAITING_FOR_HUMAN`) | `FAILED` | `6` | Immediate terminal `FAILED` (Exit `6`) |
| `RESTART_CAP_EXCEEDED` | `1` (`EXIT_FAILURE`) | Daemon restarted $\ge 3$ times (`daemon_restarts >= max_daemon_restarts`) | *None* (Human re-attestation rejected) | `FAILED` | `1` | Immediate terminal `FAILED` (Exit `1`) |
| `RESUMES_EXHAUSTED` | `1` (`EXIT_FAILURE`) | Explicit human resumes $\ge 2$ (`human_resumes_used >= max_human_resumes`) | *None* (Further resumes rejected) | `FAILED` | `1` | Immediate terminal `FAILED` (Exit `1`) |
| `PLAN_REJECTED` | `4` (`EXIT_PLAN_REJECTED`) | Human operator explicitly rejects proposed architecture plan | `reject_plan(reason=...)` | `FAILED` | `4` | Immediate terminal `FAILED` (Exit `4`) |

### 2.2 Liveness Guarantee Formulation
> **Empirical Liveness Contract:**  
> In interactive mode, human wait has **no timeout** (the supervisor suspends and waits indefinitely for human operator intervention); every waiting state has a strictly defined set of human actions leading to a bounded transition path. In headless / CI mode (`SUPERVISOR_HEADLESS=1` or `CI=1`), waiting on a human aborts deterministically after `ci_wait_timeout` (default 60s) transitioning to `Phase.FAILED` with exit code `137` (`EXIT_HEADLESS_TIMEOUT`), guaranteeing bounded execution time, process tree reaping, and rescue ref capture. There is NO separate `ABORTED_HEADLESS` enum state; `Phase.FAILED` is the single authoritative terminal error phase.

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

All timeouts are measured against monotonic timestamps (`time.monotonic()`) in supervisor memory, strictly immunizing orchestration from system wall-clock adjustments or NTP jumps.

### 3.1 Published Timeout Table

| State (Phase Enum) | Real Config Key / Environment Variable | Default Duration | NEXT STATE on Timeout | Counter Consumed | Reaping Mechanism |
|---|---|---|---|---|---|
| `PLANNING` | `Phase.PLANNING` / `SUPERVISOR_TIMEOUT_PLANNING` | 60.0s | `PLANNING` (or `WAITING_FOR_HUMAN` if cap) | `planning_retries` | Windows Job Object kill |
| `AUTHORING` | `Phase.AUTHORING` / `SUPERVISOR_TIMEOUT_AUTHORING` | 600.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Windows Job Object kill |
| `AUTHORING_READINESS_GATE` | `Phase.AUTHORING_READINESS_GATE` / `SUPERVISOR_TIMEOUT_AUTHORING_READINESS_GATE` | 60.0s | `ARCHITECT_REPAIR` | `authoring_repair_attempts` | Deterministic supervisor gate |
| `DISPATCHING` | `Phase.DISPATCHING` / `SUPERVISOR_TIMEOUT_DISPATCHING` | 60.0s | `ARCHITECT_RECOVERY_DECISION` | None (scheduler tick) | Deterministic supervisor step |
| `EXECUTING` *(Worker Turn)* | `Phase.EXECUTING` / `SUPERVISOR_TIMEOUT_EXECUTING` | 180.0s | `EXECUTING` (or `ARCHITECT_RECOVERY_DECISION` if cap reached; cap = 2) | `worker_retries[wo_id]` & `turns_spent` | Windows Job Object kill |
| `QA_REVIEW` *(QA Review)* | `Phase.QA_REVIEW` / `SUPERVISOR_TIMEOUT_QA_REVIEW` | 180.0s | `INTEGRATION_REVIEW` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `qa_reworks[wo_id]` | Windows Job Object kill |
| `INTEGRATION_REVIEW` *(Integration)* | `Phase.INTEGRATION_REVIEW` / `SUPERVISOR_TIMEOUT_INTEGRATION_REVIEW` | 180.0s | `INTEGRATION_REVIEW` (or `ARCHITECT_RECOVERY_DECISION` if cap) | `qa_reworks[wo_id]` | Windows Job Object kill |
| `ARCHITECT_REPAIR` *(Repairing)* | `Phase.ARCHITECT_REPAIR` / `SUPERVISOR_TIMEOUT_ARCHITECT_REPAIR` | 180.0s | `ARCHITECT_REPAIR` (or `WAITING_FOR_HUMAN` if cap) | `authoring_repair_attempts` | Windows Job Object kill |
| `ARCHITECT_RECOVERY_DECISION` | `Phase.ARCHITECT_RECOVERY_DECISION` / `SUPERVISOR_TIMEOUT_ARCHITECT_RECOVERY_DECISION` | 300.0s | `ARCHITECT_RECOVERY_DECISION` (or `WAITING_FOR_HUMAN` if cap) | `recovery_attempts[wo_id]` | Windows Job Object kill |
| `GITOPS` *(Release Promotion)* | `Phase.GITOPS` / `SUPERVISOR_TIMEOUT_GITOPS` | 60.0s | `GITOPS` (or `WAITING_FOR_HUMAN` if cap) | `gitops_retries` | Windows Job Object kill |
| `WAITING_FOR_HUMAN` *(Headless)* | `"ci_wait_timeout"` / `SUPERVISOR_CI_WAIT_TIMEOUT` | 60.0s | `FAILED` (Terminal Exit `137`) | None | Job Object reaped; rescue captured |
| Whole Run Limit *(CI Mode)* | `"ci_run_timeout"` / `SUPERVISOR_CI_RUN_TIMEOUT` | 1800.0s *(or derived $B \times \max(T_{turn})$)* | `FAILED` (Terminal Exit `137`) | None | Job Object reaped; rescue captured |

### 3.2 Real Phase Enum Mapping & Verification Execution
In the StackMind architecture:
- **`TESTING`** maps to **`Phase.INTEGRATION_REVIEW`** (where integration testing, automated test execution, and verification gates run) and **`Phase.QA_REVIEW`** (where the dedicated QA agent executes acceptance testing).
- **Clean `npm ci` + Build (Amendment C):** Runs strictly inside **`Phase.INTEGRATION_REVIEW`** with a timeout of **180.0s** (`SUPERVISOR_TIMEOUT_INTEGRATION_REVIEW`). This ensures a clean, isolated dependency installation and build verification before any code can be promoted to QA review or GitOps release.
- **`REPAIRING`** maps to **`Phase.ARCHITECT_REPAIR`** (where Architecture remediates work order contract defects) and **`Phase.ARCHITECT_RECOVERY_DECISION`** (where Architecture makes structural DAG recovery decisions).

### 3.3 Rationale: AUTHORING Timeout (120s $\to$ 600s)
Lead Architect authoring in `Phase.AUTHORING` does not simply generate unstructured prose. It performs comprehensive system decomposition, authoring the complete multi-work-order decomposition DAG (WO-001 through WO-N), dependency relationships, schema interface specifications, and acceptance criteria in a single structured turn. Under cloud or local LLM rate limits and token streaming latency, multi-WO graph synthesis routinely exceeded 120s. Increasing the ceiling to **600.0s** provides sufficient headroom for deep structural synthesis and schema self-validation while still enforcing bounded execution.

### 3.4 Planning Isolation: `planning_retries` vs. `authoring_repair_attempts`
- **`planning_retries`:** Consumed exclusively by failures and timeouts during high-level project goal planning (`Phase.PLANNING`). Capped at 2. Exhaustion transitions to `WAITING_FOR_HUMAN`.
- **`authoring_repair_attempts`:** Consumed exclusively by contract defects, schema validation errors, and readiness gate rejections during detailed work order decomposition (`Phase.AUTHORING`, `Phase.AUTHORING_READINESS_GATE`, `Phase.ARCHITECT_REPAIR`). Capped at 2.
- **Strict Isolation:** Planning failures **never** decrement `authoring_repair_attempts`, and authoring defects **never** decrement `planning_retries`.

### 3.5 Dynamic CI Run Timeout Derivation
When `ci_run_timeout` is not explicitly set via constructor argument or `SUPERVISOR_CI_RUN_TIMEOUT` environment variable, the supervisor derives the effective whole-run timeout dynamically:
$$\text{effective\_ci\_run\_timeout} = \text{actual\_turn\_budget} \times \max(T_{\text{turn}})$$
where $\text{actual\_turn\_budget} = \text{state.initial\_turn\_budget}$ ($18 + 5N$), and $\max(T_{\text{turn}})$ is the largest configured turn timeout (`600.0s`). This ensures the whole-run ceiling scales proportionally with project size rather than relying on a hard-coded static multiplier of 40.

### 3.6 Justification of GITOPS 60s Timeout & Remote Latency Assumption
`GITOPS` execution does NOT compile source code or download third-party packages from external registries. All dependencies, compilation, and unit tests are already validated during `EXECUTING` and `INTEGRATION_REVIEW`.
In StackMind:
1. `GITOPS` performs solely in-memory archive operations, derived commit tree generation, signing, and git ref updates.
2. Measured execution of `create_gitops_commit` + derived tree building + local ref creation is **< 0.25 seconds**.
3. Pushing over an uninherited anonymous pipe to a local repository takes **< 1.2 seconds**.
4. **Non-Local Remote Assumption:** Under the documented operational assumption that the remote git host is accessible over a standard network connection with latency and packfile transfer completing in under 5–10 seconds, **60.0 seconds** provides an ample 6x–10x safety margin for network transport while deterministically killing hanging git network calls without stalling the supervisor.

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
- **Process Tree:**
  $$\text{supervisor (Python)} \longrightarrow \text{git (subprocess)} \longrightarrow \text{credential helper (Python)}$$
- **Pipe Handle Ownership:**
  1. `supervisor`: Allocates an anonymous OS kernel pipe (`os.pipe()`), writes push token bytes directly to the kernel buffer, and immediately closes the write handle (`os.close(w_pipe)`). On Windows, converts the read file descriptor to an OS handle (`msvcrt.get_osfhandle(r_pipe)`) and marks it inheritable (`os.set_handle_inheritable(h_r, True)`).
  2. `git`: Spawned by supervisor with `close_fds=False` and explicit argument `-c credential.helper=!"<python_exe>" -c "<helper_code>" <handle>`. Inherits the OS pipe read handle.
  3. `helper`: Spawned by `git` using the **absolute interpreter path** (`sys.executable`). Opens the inherited OS handle (`msvcrt.open_osfhandle`), reads the token bytes, outputs username/password to stdout, and terminates immediately.
  4. Once helper exits, EOF is reached on the pipe. No other process holds or receives the pipe handle.
- **Absence from Argv and Env:** Verified directly via native `NtQueryInformationProcess` memory inspection of child `PEB->ProcessParameters`:
  - `CommandLine`: Token string is 100% absent.
  - `Environment`: Token string is 100% absent (`GIT_CONFIG_COUNT` is not used).
- **Push from Storage Root with Git Config Neutralization:** Supervisor pushes directly from the external bare store (`storage_root`) with hardened options:
  - `-c core.fsmonitor=false -c core.hooksPath= -c core.excludesFile=`
  - Explicit environment variables: `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=NUL`, `GIT_CONFIG_SYSTEM=NUL`, `GIT_CONFIG=NUL`.
  - Attacker rewrites in workspace `.git/config` or user `~/.gitconfig` (`insteadOf`, `remote.url`, `credential.helper`) receive zero traffic.
- **Zero Disk Footprint:** No push token bytes are ever written to disk or configuration files.

### 5.2 Console Injection Defense (Items 7 & 8)
- **Threat:** A detached child process or persistent service calls `kernel32.FreeConsole()`, `kernel32.AttachConsole(parent_pid)`, and `kernel32.WriteConsoleInputW()` to inject `'y\n'` into the supervisor's interactive administrative confirmation prompt (`confirm_on_console`).
- **Mitigation:**
  1. `ProcessReaper` launches all agent processes inside Job Objects with `DETACHED_PROCESS` (`0x00000008`).
  2. Prior to displaying any confirmation prompt, `ServiceJobManager.suspend_all()` suspends all active background service processes (dev servers, background daemons).
  3. `confirm_on_console` calls `kernel32.GetConsoleProcessList()`. If any PID registered in either the turn reaper (`reaper._registered_pids`) OR the service manager (`service_mgr._get_all_service_pids()`) is detected attached to the console session, an intrusion is flagged and the prompt **fails closed immediately (`return False`)**.
  4. `kernel32.FlushConsoleInputBuffer()` wipes any pre-injected keystrokes before `input()` is called.
  5. In `finally:`, `ServiceJobManager.resume_all()` restores background services cleanly once prompt handling completes.
  6. In headless mode (`SUPERVISOR_HEADLESS=1` or `CI=1`), interactive prompts are disabled and fail closed immediately.

---

## 6. Phase 1 Risk Register (Open & Deferred Items)

The following items represent known architectural risks identified during Phase 0 audits. All rows are classified **OPEN-P1** and scheduled for implementation under Phase 1 confinement and isolation work:

| Item / Area | Threat Description | Status in P0 | Evidence Label | Owner / Phase Target | Scheduled Phase 1 Remediation |
|---|---|---|---|---|---|
| **Task 1(b) Per-Turn Home** | Agent writing persistent dotfiles, caches, or binaries to `%USERPROFILE%` across turns | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Sandboxing (P1-1) | Ephemeral `%USERPROFILE%` / `%APPDATA%` generated per turn and wiped on turn exit. |
| **Credential Files** | Host credential files (`~/.ssh`, `~/.git-credentials`, `.npmrc`) readable by untrusted same-user child processes | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Confinement (P1-1) | Filesystem isolation / AppContainer sandbox boundary preventing access to user home secrets. |
| **Windows Persistence** | Child writing to Registry Run keys (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`), Startup folders, or Scheduled Tasks (`schtasks`). *Note: A Job Object does NOT address or constrain persistence mechanisms.* | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Confinement (P1-1) | Registry write virtualization, Low-Integrity token restrictions, and AppContainer isolation. |
| **Process Ownership & Ports (Audit A6)** | Child binding to local TCP/UDP ports or squatting daemon port (8765); same-user port hijacking | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Network (P1-2) | Windows Filtering Platform (WFP) network egress & local port bind firewall filtering rules. |
| **Tool-Output Escaping** | Raw tool output containing adversarial prompt injection payloads. *Note: ANSI console sanitization does NOT prevent LLM prompt injection.* | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Gateway (P1-3) | Delimiter neutralization, output tagging, and hardened structured schema validation gateway. |
| **Network Egress** | Child initiating outbound TCP/UDP connections to exfiltrate secrets or establish remote C2 | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Network (P1-2) | AppContainer network isolation rules denying outbound internet connectivity. |
| **Writes Outside Workspace** | Child creating or modifying files on host filesystem outside the designated workspace boundary | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Sandboxing (P1-1) | AppContainer filesystem write virtualization restricting disk write access strictly to workspace directory. |
| **Daemon Token File** | `~/.stackmind/daemon/.daemon_token` readable by same-user agents on Windows. *Note: The anonymous pipe in Task 1(a) carries the git push token only; daemon auth token remains on disk.* | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Security | Per-session anonymous pipe token delivery to CLI; remove static token file from disk. |
| **Daemon Memory** | Same-user child inspecting supervisor memory via `OpenProcess(PROCESS_VM_READ)` / `ReadProcessMemory` | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Confinement (P1-1) | Windows Low-Integrity / AppContainer token confinement boundary restricting handle access. |
| **POSIX Substrate Parity** | Windows-specific NT Job Objects, named pipes, and console defenses lack empirical POSIX equivalents in P0 | **OPEN-P1** | `[REASONED ONLY]` | Phase 1 Runtime (P1-4) | Linux `cgroups v2` and macOS sandbox execution harnesses with identical contract enforcement. |

---

## 7. Final Deliverable Summary Table

| Item | Scope / Deliverable | Status | Evidence Label | Primary Commit Hash | Key Files & Line References |
|:---:|:---|:---:|:---:|:---:|:---|
| **P0-1** | Git command allowlist & option abbreviation filtering; rejection of driver/path injection (`--no-textconv`, `--paginate`, etc.) | **DONE** | `[RAN AGAINST CODE]` | `a3832b8` | [git_allowlist.py:1-250](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/git_allowlist.py#L1-L250)<br>[test_p0_1_git_allowlist.py:1-230](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_1_git_allowlist.py#L1-L230) |
| **P0-2** | Turn snapshot isolation & baseline state capture; junction reparse point handling; ADS directory auditing | **DONE** | `[RAN AGAINST CODE]` | `b1514a6` | [turn_snapshot.py:1-350](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/turn_snapshot.py#L1-L350)<br>[test_p0_2_turn_snapshot.py:1-410](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_2_turn_snapshot.py#L1-L410) |
| **P0-3** | Post-turn diff gate; git internals mutation rejection; workspace boundary enforcement; rogue write rejection | **DONE** | `[RAN AGAINST CODE]` | `d6b072d` | [diff_gate.py:1-550](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/diff_gate.py#L1-L550)<br>[test_p0_3_diff_gate.py:1-550](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_3_diff_gate.py#L1-L550) |
| **P0-4** | QA-approval-to-commit tree binding & push token delivery over uninherited anonymous pipe | **DONE** | `[RAN AGAINST CODE]` | `f4a9731` | [diff_gate.py:1160-1240](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/diff_gate.py#L1160-L1240)<br>[test_p0_3_diff_gate.py:560-690](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_3_diff_gate.py#L560-L690) |
| **Derived Tree** | Supervisor independently derives commit tree from verified turn diffs and signs/attests it in external storage root | **DONE** | `[RAN AGAINST CODE]` | `d6b072d` | [diff_gate.py:900-1050](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/diff_gate.py#L900-L1050)<br>[test_p0_3_diff_gate.py:700-850](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_3_diff_gate.py#L700-L850) |
| **Counters & Budget** | Strict decrement on turn dispatch; bounded recovery counters; elimination of turn timeout double-charging | **DONE** | `[RAN AGAINST CODE]` | `410abb6` | [supervisor.py:610-675](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L610-L675)<br>[test_p0_5_counters_and_budget.py:1-450](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L1-L450) |
| **Exit-Code Table** | Published exit-code table constants (`0` SUCCESS, `1` USER_CANCELLED, `2` UNRECOVERABLE_FAILURE, `3` RETRIES_EXHAUSTED, `4` BUDGET_EXHAUSTED, `5` RECOVERY_DECLINED, `6` INTEGRITY_VIOLATION, `137` HEADLESS_TIMEOUT) with immutable verified mapping | **DONE** | `[RAN AGAINST CODE]` | `3dd9615` | [supervisor.py:113-120](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L113-L120)<br>[test_p0_5_counters_and_budget.py:210-245](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L210-L245) |
| **Human Resume** | Elimination of resumable dead ends; bounded human resume attempts with fail-closed transition to terminal `FAILED` on exhaustion | **DONE** | `[RAN AGAINST CODE]` | `500d046` | [supervisor.py:495-555](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L495-L555)<br>[test_p0_6_timeouts_and_liveness.py:436-502](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L436-L502) |
| **Re-Attestation** | Enforce human re-attestation on daemon restart; un-attested runs fail closed with `EXIT_INTEGRITY_VIOLATION` to `WAITING_FOR_HUMAN` | **DONE** | `[RAN AGAINST CODE]` | `5ffa69a` | [supervisor.py:928-935](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L928-L935)<br>[test_p0_5_counters_and_budget.py:995-1035](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L995-L1035) |
| **Reachability Model** | WAITING_FOR_HUMAN transition reachability model checker traversing all 12 entry/action paths in simulated test environment | **DONE** | `[RAN IN MODEL]` | `1926aef` | [verify_waiting_for_human_reachability.py:1-135](file:///W:/Aatish/Stuff/stackmind-cli/scripts/verify_waiting_for_human_reachability.py#L1-L135) |
| **P0-6 Timeouts & Liveness** | Published full timeout table; row-level unit tests; $7 \times 8 = 56$ parameterized stub matrix (< 30 steps, < 3s); rescue ref & crash bundle verification | **DONE** | `[RAN AGAINST CODE]` | `d1b5b32` | [supervisor.py:121-136](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/supervisor.py#L121-L136)<br>[test_p0_6_timeouts_and_liveness.py:500-750](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_6_timeouts_and_liveness.py#L500-L750) |
| **Console Injection Defense** | Detached child console injection defense; ServiceJobManager suspension during prompts; GetConsoleProcessList detection against turn reaper and service manager PIDs | **DONE** | `[RAN AGAINST CODE]` | `4859d43` | [protocol.py:85-135](file:///W:/Aatish/Stuff/stackmind-cli/validators/kernel/daemon/protocol.py#L85-L135)<br>[test_p0_5_counters_and_budget.py:1275-1360](file:///W:/Aatish/Stuff/stackmind-cli/tests/test_p0_5_counters_and_budget.py#L1275-L1360) |
| **Test Collection Manifest** | Test suite inventory synchronization verified against `pytest --collect-only` | **DONE** | `[DONE (documentation)]` | `688edbb` | [collected_tests.txt:1-482](file:///W:/Aatish/Stuff/stackmind-cli/collected_tests.txt#L1-L482) |
| **Hardening Specification** | Core hardening specification, contracts, and threat model documentation | **DONE** | `[DONE (documentation)]` | `7dd8c3b` | [p0_hardening_specification.md:1-260](file:///W:/Aatish/Stuff/stackmind-cli/docs/p0_hardening_specification.md#L1-L260) |
| **P1-1** | Low-Integrity AppContainer confinement boundary & child memory inspection protection (`ReadProcessMemory`) | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 Confinement (P1-1) |
| **P1-2** | Windows Filtering Platform (WFP) network egress & local port bind filtering rules | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 Network (P1-2) |
| **P1-3** | Delimiter escaping & prompt injection boundary sanitization gateway | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 Gateway (P1-3) |
| **P1-4** | POSIX substrate parity (Linux `cgroups v2` and macOS sandbox execution harnesses) | **OPEN-P1** | `[REASONED ONLY]` | Deferred | Scheduled for Phase 1 Runtime (P1-4) |\n