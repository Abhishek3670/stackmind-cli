# ProcessSandbox Hardening Report — All Four Phases

**Project:** stackmind-cli · **Branch:** `feat/p6-open-source-tui` · **Date:** 2026-09-25
**Scope:** `validators/kernel/sandbox.py` (`ProcessSandbox`), `validators/kernel/tools.py` (`ToolGateway`), new `validators/kernel/interpreter_denylist.py`, `tests/test_sandbox_hardening.py`, `docs/` (this report).

---

## 1. Why We Needed This: The Threat Model

`ProcessSandbox` executes shell commands **on behalf of AI agents** via `ToolGateway.run_command`. The command vector originates from **LLM output** — including local Ollama models with no content filtering. The security boundary is therefore not against a malicious human at the keyboard; it is against a **hallucinating or prompt-injected model issuing dangerous commands autonomously**.

An unhardened sandbox gave such a model, in a single tool call:

| Gap | Attack the model could perform |
|---|---|
| **Full environment inheritance** | `os.environ` of the daemon — API keys, tokens, cloud credentials — was readable by any child process. One `print(dict(os.environ))` exfiltrates the operator's secrets. |
| **Interpreter/shell bypass** | The path-traversal argument check inspected path-shaped arguments only. `["bash", "-c", "<anything>"]` sidestepped it entirely: the dangerous payload lives inside an opaque string, never parsed as a path. |
| **No resource ceilings** | Only a wall-clock timeout existed. A fork bomb, a 40 GB `bytearray`, or a `while True:` loop could take down the host until the timeout fired — and memory exhaustion could take the *daemon* down with it. |
| **Placeholder authorization** | `run_command` authorized against the fixed string `"workspace/command"`, which always matches `"workspace/**"`. Once a role's contract granted `RUN_COMMAND` **at all**, it could run **any** command — unlike `read_file`/`write_file`, which check real targets. |

The project's own philosophy (CONTRACT-01, fail-closed governance, journaling, scratch workspaces) already protected *files*; commands were the unguarded flank.

**Preserved invariant throughout:** `validators/kernel/verification/canary.py` and `evidence.py` construct `ProcessSandbox` **directly** as trusted, platform-internal callers (reachable only via human-invoked `stackmind skill test` → `VerificationPipeline`). Every layer below lives at the **agent-facing `ToolGateway.run_command` boundary**, never inside `ProcessSandbox` itself, so trusted callers keep full capability. Verified intact after every phase.

---

## 2. Phase 1 — Environment Scrubbing

**Why:** The daemon process holds credentials for cloud providers, GitHub, etc. Any command an agent runs must not inherit them, or prompt injection becomes credential theft.

**What it does:**
- `ProcessSandbox._child_env()` builds the child environment from an explicit `DEFAULT_ENV_ALLOWLIST` tuple — every entry empirically justified:
  - `PATH` (tool resolution, venv continuation), `HOME`/`USERPROFILE`/`HOMEDRIVE`/`HOMEPATH` (git/npm config), `TEMP`/`TMP`/`TMPDIR` (toolchains), locale vars.
  - Windows essentials (`SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `APPDATA`, …) — python.exe **will not initialize** without `SYSTEMROOT`; verified empirically.
- Everything else — all secrets — is **dropped**. Matching is case-insensitive (Windows env names fold case) with first-wins semantics.
- Two tuning knobs: `env_allowlist` (constructor) and `env_extra` (per-call override, merged last, wins over inherited).

**What it solves:** The exfiltration class is closed. A compromised model that runs `env`, `printenv`, or reads `/proc/self/environ` sees a sterile environment.

**Verification:** 7 dedicated tests (`test_child_env_excludes_secrets`, case-insensitivity, allowlist tunability, bare-tool resolution under the scrubbed env, extras merge/override). Full suite green (806 at the time).

---

## 3. Phase 2 — Interpreter Denylist at the Agent Boundary

**Why:** Path checks are structurally blind to `bash -c`. If arbitrary string execution remains available, every other check is decorative.

**What it does** — new `validators/kernel/interpreter_denylist.py`, called by `ToolGateway.run_command` **after** contract authorization, **before** `sandbox.run`, raising `PermissionError` with the denial reason. A **hard** denylist: no contract override, fail-closed, consistent with project philosophy.

Three tiers, matching on a normalized `command[0]` (strips quotes, path separators, drive prefixes, `.exe` suffix; lowercases — defeats `C:\Windows\System32\CMD.EXE`, `.\PwSh`, `/usr/bin/Python3`):

1. **Shells — unconditionally denied:** `bash, sh, zsh, ksh, dash, ash, fish, csh, tcsh, cmd, powershell, pwsh, wsl` (+ `.exe` variants) and eval-style constructs (`eval`, `exec`, `source`, `.`).
2. **Interpreters — denied only with code-string flags** (`-c`, `-e`, `--eval`, `-Command`, `-EncodedCommand`, `-p`, `-n`, …): `python`, `node`, `perl`, `ruby`, `php`, `lua`, `pypy`, `deno`, `bun`, `jshell`, `ghci`, … Running a **script file** (`python script.py`) stays allowed — the normal governed workflow.
3. **Unconditional:** `awk, gawk, mawk, sed` (string-program scripting models) and `nc, netcat, ncat, socat` (network shells).

Shells with a *script-file* argument need no separate rule — they are already denied by name (rule 1 is flag-independent).

**What it solves:** The bypass class is closed. `bash -c "cat /etc/passwd"`, `python -c "import os; os.system(...)"`, `node -e "..."` are all rejected at the boundary regardless of what the contract grants. Legitimate needs execute real files inside the scratch workspace, where path checks and (Phase 3) limits still apply.

**Test-vector fallout (resolved and explained):** three existing test files used `python -c` one-liners and were migrated to script-file execution. Verification confirmed all three exercise the **ToolGateway** chain (directly, via `GovernedToolRegistry`/MCP, and via `ProviderGateway.run_loop`) — so the denylist genuinely applies and the migration was *required*. A prior report claiming they called `ProcessSandbox` directly was wrong; evidence documented in-session.

**Verification:** 11 denylist tests (bypass vectors, case/path normalization unit cases, hard-no-override even with a permitting contract, script-file allowed). All three migrated vector files pass (2/4/6 tests). Full suite green.

---

## 4. Phase 3 — Memory & CPU Resource Limits

**Why:** A bounded-timeout but unbounded-memory/CPU sandbox still lets a runaway model degrade or crash the host — the denial-of-service class, autonomous and accidental.

**What it does** — both mechanisms implemented, per-platform:

- **Windows (this project's platform):** Win32 **Job Objects** via `ctypes` — `CreateJobObjectW` → `SetInformationJobObject` (`_JOBOBJECT_EXTENDED_LIMIT_INFORMATION`) → `AssignProcessToJobObject` → `CloseHandle` in `finally`. Flags: `PROCESS_MEMORY | JOB_MEMORY` for the ceiling, `PerProcessUserTimeLimit` (100-ns ticks) for CPU, and `KILL_ON_JOB_CLOSE` so orphaned grandchildren die with the job. **Race-free attach:** the child is created with `CREATE_SUSPENDED`, assigned to the job, and only then its main thread is resumed (located via the toolhelp thread snapshot, since CPython's `Popen` does not expose the child's thread handle) — the job's limits are enforced from the child's first instruction. Assignment is **fail-closed**: if the job cannot be attached, the child is killed and the command refused rather than run unbounded.
- **POSIX — no `preexec_fn`:** the original design used `preexec_fn`, which CPython executes as Python code inside the forked child of a multi-threaded parent — a documented deadlock class (fork duplicates only the calling thread, so any lock another thread held at fork stays locked in the child; CPython also disables `vfork` when `preexec_fn` is set — verified in the 3.11.9 `_posixsubprocess.c` source). Redesigned: limits are applied by a **tiny compiled C exec shim** (`getenv` → `setrlimit(RLIMIT_AS|DATA|CPU)` → `execvp`) that runs *before* the real command, so no Python code and no lock-dependent libc calls exist between fork and exec. Limits travel via scrubbed-env variables (`STACKMIND_RLIMIT_BYTES`, `STACKMIND_RLIMIT_CPU`); the shim is built once (`cc`), cached, and degrades gracefully if no compiler is available. Process-tree containment uses `start_new_session=True` (libc `setsid`, no Python hook) so the wall-clock timeout path can `killpg` the whole tree.
- **Defaults (configurable, both constructor and per-call):** `DEFAULT_MEMORY_LIMIT_BYTES = 512 MB` (hundreds-of-MB range as specified), `DEFAULT_CPU_LIMIT_SECONDS = 60.0`.
- **Graceful degradation:** any setup failure degrades to "no limits" rather than breaking command execution.
- **Classification:** `CommandResult.is_resource_limit_failure` maps the real kill signatures (`MemoryError`, NTSTATUS `0xC0000044` quota kill, POSIX SIGKILL codes) — enabling callers to *distinguish* limit kills from ordinary failures.
- **Test seam:** `ToolGateway` accepts an optional injected `sandbox` (default behavior unchanged).

**What it solves:** Memory bombs are refused at allocation (`MemoryError` under the commit ceiling); infinite loops are hard-killed by the kernel; forks/genealogy contained via job closure/process groups. Failures are bounded, fast, and *classifiable* instead of mysterious hangs.

**Verification (behavioral, not just "the value is set"):** kill proofs — 120 MB allocation under a 50 MB ceiling → killed; `while True:` under a 1 s CPU limit → killed; wall-clock timeout sweep of the process tree; shim compile/reuse unit test (POSIX; skipped on Windows); under-ceiling success, per-call override, and enforcement through the full gateway chain. Differential probe: same bomb with limits **disabled** → `survived`, rc=0; with limits → `MemoryError` (commit refusal) and `0xC0000044` (Job quota kill) respectively, both classified by `is_resource_limit_failure`. Trusted-caller regression (canary/evidence/verification pipeline): 27 passed.

---

## 5. Phase 4 — Command-Level Contract Scoping (Fact-Finding Only)

**Why investigated:** With Phases 1–3 in place, the remaining exposure is *granularity*: a contract that grants `RUN_COMMAND` grants **every** non-denied binary. Scoped contracts (`pytest` yes, `git push` no) would close it — but this is an architectural extension, so it was **investigated, not implemented**, pending sign-off.

**Findings (all claims independently verified against the code):**

1. **No precedent exists.** `schemas/contract.schema.json` strictly types scope rules as `{module, depth?}` with `additionalProperties: false` — any `commands:` key fails validation today. `ContractEvaluator` (`validators/kernel/contract.py`) is purely path/graph-oriented (fnmatch + prefix matching, traversal rejection on the raw target). `stackmind contract validate --op` supports only `edit, read`.
2. **Call-site change is non-trivial.** Feeding a real command string into the current evaluator would (a) falsely trip the traversal check on absolute executable paths, (b) be meaningless under fnmatch. A correct design needs a dedicated `RUN_COMMAND` branch: reuse `_exe_name` normalization from the denylist (defeats `.\pytest.EXE`/case/path bypasses), match **binary allowlists** (exact vector matching is too brittle for LLM tool calls), and bind path-shaped arguments to the contract's existing filesystem scope.
3. **Census: 19 contracts (`WO-001`–`WO-019`), zero declare command permissions.** All scoping is module/path-based; `RUN_COMMAND` is granted all-or-nothing via `AuthorizationPolicy.permit` at the policy layer. **Correction to the original fact-finding report:** WO-011–019 carry **no `write:` key at all** (not "all 19 are read-write") — the fleet is heterogeneous, which *strengthens* the backward-compatibility argument.
4. **If pursued, it must be opt-in:** legacy contracts (no `commands` block) fall back to today's exact stack — policy permit → Phase 2 hard denylist → Phase 3 limits → path validation. Hard enforcement would break command execution fleet-wide on day one.

**Decision:** Not worth the schema/evaluator/template churn at the current threat level. Phases 1–3 already close the critical autonomous vectors. Filed as a candidate follow-up work order.

---

## 5b. Phase 5 — Harness Runner: Closing the Live Bypass

Post-hoc path-tracing (demanded before Phase 3 sign-off) exposed that the **daemon/TUI turn loop never touches the hardened path**. Real chain: TUI → `SessionManager.start_turn` → `AgentRunner.run_once` → for every string in the model's `commands` payload → `subprocess.run(cmd, shell=True)` with the **full daemon environment** (`runner.py:574`), inside a temp staged copy. `ToolGateway.run_command` has **zero non-test constructors** — Phases 1–3 guarded a boundary the runtime's main loop does not use.

**Reachability verdict (evidence, not assumption):** live today. The harness-output JSON schema **sanctions** `commands` as model output (`schemas/harness-output.schema.json`); the Ollama backend passes the raw payload through (`backend.py:837`, `commands=tuple(payload.get('commands', []))`); a real Ollama backend ships registered in the default registry. The only mitigations were incidental: shipped payload builders hardcode `commands: []`, and nothing prompts the model to emit commands — the *next* hallucination away from a raw, unbounded, secret-bearing shell.

**Fix (implemented, not deferred):** `AgentRunner._execute_sandboxed_command()` now routes every declared command through the hardened stack, in order: `shlex` tokenization (Windows quote-rule aware; unbalanced quotes refused) → Phase 2 hard denylist (`command denied: …`) → Phase 1 scrubbed env (secret probe returns `clean`) → Phase 3 limits + wall-clock timeout (120 s, tree swept) → execution `shell=False` against a **read-only scratch copy of staged HEAD** via `ScratchWorkspace.create` (path containment; command writes never touch the live tree). Failure semantics preserved for the downstream 6D gate (`result.args`/`returncode` contract intact); denied/failed commands block the turn at `behavioral_verified` instead of writing into the staged diff.

**Tests:** 5 new runner tests (`tests/test_sandbox_hardening.py` — denylist denial, env scrub, scratch containment, FileNotFoundError/unparseable handling, gate-contract preservation) + 2 updated `test_harness.py` gate tests whose `python -c` payloads are now denied pre-execution (turn still blocked, still no live writeback — strictly stronger).

**Residual, filed as work order (not bundled):** `AgentRunner._validate_staged_state` (`runner.py:843`) copies the **live working tree** for staged validation, so local uncommitted developer changes ride along — pre-existing behavior, orthogonal to command execution, but should become a pristine-HEAD copy.

## 6. Improvement to the TUI

The TUI (`cli/tui/`, the P6 work on this branch) is the **human cockpit for the same governed runtime** this hardening protects. Nothing in `cli/tui/` was modified — the improvements are underneath it, plus concrete follow-up opportunities its existing code already anticipates.

### What improves today (no TUI code changes)

1. **Bounded, explainable agent turns.** The TUI drives turns through the daemon (`SessionManager.start_turn` → runner → backends). Before Phase 3, a model-issued fork bomb or memory hog inside a turn degraded the whole host and could take the daemon — the TUI's own process tree — down with it. Now such turns end **fast**, with a real returncode instead of a spinner that never resolves. Users see a terminal state instead of a hang.
2. **The `OLLAMA ERROR` panel pattern gains a sibling class.** `app.py` already branches error panels on message content (`app.py:2353`: `OLLAMA ERROR` vs `OPERATION FAILED`). Denylist denials (`PermissionError` from Phase 2) and limit kills (`is_resource_limit_failure` from Phase 3) are now **clean, distinguishable failures** that fit exactly this pattern — a hallucinated `bash -c` surfaces as a one-line *denial* with a reason string, not as a confusing shell error from deep inside the stack.
3. **Secret hygiene for a cockpit that displays agent output.** The TUI streams model output, tool results, and errors to screen. Phase 1 guarantees that no command the model runs can read daemon credentials — so there is no scenario where an agent's stdout dumps an API key into the TUI scrollback, where it would be visible, logged, and copyable.
4. **Local-model confidence.** The TUI explicitly targets local Ollama backends (`:rebind <role> ollama <model>`, role status board defaulting to "Ollama"). Unfiltered local models are precisely the highest hallucination risk — the hardening is what makes pointing the TUI at them acceptable: worst case is a bounded, denied, journaled failure.

### Follow-up opportunities (TUI-side, not yet implemented)

1. **A `RESOURCE LIMIT` panel**, analogous to `OLLAMA ERROR`, keyed on `is_resource_limit_failure` — "killed: 512 MB memory ceiling exceeded" is actionable feedback; a bare nonzero returncode is not.
2. **A `COMMAND DENIED` panel** for Phase 2 denials — the `PermissionError` reason string ("shell or eval-style interpreter 'bash' is denied at the agent boundary") is already user-ready prose.
3. **Role/status board enrichment** — the roles screen could distinguish "sandboxed execution active (env-scrubbed, denylist, limits)" per backend, turning invisible safety posture into visible trust.

### Related TUI-adjacent finding from this session

`tests/test_backend_abstraction.py::test_dynamic_role_rebinding_execution` (the TUI's backend-rebinding contract) was proven **environment-sensitive, not broken**: its `mock-model` backend POSTs to a real Ollama endpoint (`mock_mode=False` by default). With Ollama running it passes (verified 26/26 in-file); without, it can exceed its 3 s thread join. Hardening was disproven as a cause via a clean-HEAD stash run. A one-line `mock_mode=True` fix would make it hermetic — filed as follow-up, not in hardening scope.

---

## 7. Verification Ledger (actual runs, this effort)

| Gate | Result |
|---|---|
| `tests/test_sandbox_hardening.py` (Phases 1–5, redesigned limits) | **28 passed, 1 skipped** (POSIX-only shim test) |
| Phase-2 migrated vectors (`test_execution_kernel`, `test_mcp_runtime`, `test_provider_gateway`) | **12 passed** (2+4+6) |
| Trusted-caller regression (canary, evidence, phase0, phase5) | **27 passed** |
| Backend abstraction (with Ollama live) | **26 passed** |
| **Full suite** (standard invocation, pre-existing gate test deselected) | **827 passed, 1 skipped, 0 failed, 1 deselected — with Ollama DOWN (hermetic)** |
| Differential kill probe (limits off vs on) | limits off → `survived` rc=0; 50 MB limit → `MemoryError`; 1 s CPU limit → `0xC0000044` |

**Standing constraints preserved:** denylist never moved into `ProcessSandbox`; canary/evidence callers untouched and green; TUI files (`cli/tui/*`, P6 work) and context-wiring work (`validators/harness/*`) untouched; no contract-override path exists for the denylist or limits.

---

## 8. Bottom Line

| Phase | Closed attack class | Status |
|---|---|---|
| 1 — Env scrubbing | Credential exfiltration via child processes | ✅ Implemented + verified |
| 2 — Interpreter denylist | Shell/eval one-liner bypass of path checks | ✅ Implemented + verified |
| 3 — Resource limits | Autonomous DoS / host degradation | ✅ Implemented + verified |
| 4 — Command scoping | Per-binary over-grant | 🔍 Investigated; opt-in design documented; deferred pending sign-off |
| 5 — Harness runner routing | Raw `shell=True` bypass of the entire hardened path in the daemon/TUI turn loop | ✅ Implemented + verified (was: live bypass) |

An agent — including a fully hallucinating local model — can now run commands in a sterile environment, execute only real script files inside its scratch workspace, and consume only bounded resources. Its failures are fast, journaled, classified, and displayable. The remaining granularity question (Phase 4) has a documented, backward-compatible design awaiting a product decision.
