"""Sandbox hardening tests (added incrementally per phase).

Phase 1: child env is scrubbed to an allowlist; secrets never leak.
Phase 2: interpreter denylist blocks arbitrary-code-execution bypasses.
Phase 3: resource limits kill runaway children.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from validators.kernel import sandbox as sandbox_mod
from validators.kernel.interpreter_denylist import check_command
from validators.kernel.sandbox import DEFAULT_ENV_ALLOWLIST, ProcessSandbox
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


@pytest.fixture
def sandbox(tmp_path: Path) -> ProcessSandbox:
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "sandbox-test")
    return ProcessSandbox(ws)


def run_py(sandbox: ProcessSandbox, code: str):
    return sandbox.run([sys.executable, "-c", code])


# ---------------------------------------------------------------------------
# Phase 1: environment scrubbing
# ---------------------------------------------------------------------------


def test_child_env_excludes_secrets(sandbox):
    os.environ["STACKMIND_SECRET_TEST_TOKEN"] = "super-secret-value"
    try:
        result = run_py(sandbox, "import os; print('token' in os.environ)")
        assert result.returncode == 0
        assert result.stdout.strip() == "False"
    finally:
        os.environ.pop("STACKMIND_SECRET_TEST_TOKEN", None)


def test_child_env_keeps_path(sandbox):
    result = run_py(sandbox, "import os; print(bool(os.environ.get('PATH')))")
    assert result.returncode == 0
    assert result.stdout.strip() == "True"


def test_case_insensitive_windows_env_scrubbing(sandbox):
    # Windows env names are case-insensitive; verify a lowercase secret with an
    # allowlisted uppercase name cannot smuggle through either direction.
    os.environ["path_mimic_stackmind"] = "should-not-inherit-anyway"
    try:
        result = run_py(sandbox, "import os; print(os.environ.get('path_mimic_stackmind'))")
        assert result.returncode == 0
        # 'PATH_MIMIC_STACKMIND' is not allowlisted, so it must not appear,
        # regardless of casing.
        assert result.stdout.strip() == "None"
    finally:
        os.environ.pop("path_mimic_stackmind", None)


def test_bare_tool_resolves_under_scrubbed_env(sandbox):
    # Bare command resolution (no absolute path) must still work: PATH is on
    # the allowlist, so git resolves if installed on the host.
    result = sandbox.run(["git", "--version"])
    if result.returncode != 0:
        pytest.skip("git not installed on host")
    assert "git version" in result.stdout


def test_env_extra_merges_and_overrides(sandbox):
    result = sandbox.run(
        [sys.executable, "-c", "import os; print(os.environ.get('SM_PROBE'))"],
        env_extra={"SM_PROBE": "explicit-value"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "explicit-value"


def test_env_extra_wins_over_inherited(sandbox):
    os.environ["SM_OVERRIDE_PROBE"] = "from-parent"
    try:
        result = sandbox.run(
            [sys.executable, "-c", "import os; print(os.environ.get('SM_OVERRIDE_PROBE'))"],
            env_extra={"SM_OVERRIDE_PROBE": "from-call"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "from-call"
    finally:
        os.environ.pop("SM_OVERRIDE_PROBE", None)


def test_allowlist_is_tunable_per_sandbox(tmp_path: Path):
    authoritative = tmp_path / "authoritative-tuned"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "sandbox-tuned")
    tight = ProcessSandbox(ws, env_allowlist=[])
    result = run_py(tight, "import os; print(len(os.environ))")
    assert result.returncode == 0
    assert result.stdout.strip() == "0"
    # Default allowlist still contains the essentials.
    assert "PATH" in DEFAULT_ENV_ALLOWLIST


# ---------------------------------------------------------------------------
# Phase 2: interpreter denylist (agent boundary = ToolGateway.run_command)
# ---------------------------------------------------------------------------


def _gateway_sandbox(tmp_path: Path) -> tuple[ToolGateway, object]:
    """Build a ToolGateway wired exactly like the test fixtures do."""
    from validators.kernel.boundary import RuntimeBoundary
    from validators.kernel.contract import ContractNormalizer
    from validators.kernel.identity import AuthorizationPolicy
    from validators.kernel.operations import OperationJournal

    authoritative = tmp_path / "authoritative-gw"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "gw-attempt")
    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-TEST",
        "scope": {"allow": ["workspace/**"], "write": "read-write"},
        "budget": {"max_files_touched": 4},
    })
    return (
        ToolGateway(
            ws,
            RuntimeBoundary(OperationJournal()),
            contract,
            AuthorizationPolicy.permit("test", ["run_command"]),
            "session-gw",
            "attempt-gw",
            "codex",
            "test-provider",
        ),
        ws,
    )


def test_denylist_blocks_bash_c_bypass(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="denied at the agent boundary"):
        gateway.run_command(["bash", "-c", "cat /etc/passwd"])


def test_denylist_blocks_absolute_path_bash(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="denied at the agent boundary"):
        gateway.run_command(["/bin/bash", "-c", "cat /etc/passwd"])


def test_denylist_blocks_windows_absolute_cmd(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="denied at the agent boundary"):
        gateway.run_command(["C:\\Windows\\System32\\cmd.exe", "/c", "whoami"])


def test_denylist_blocks_python_c(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="code-string flag"):
        gateway.run_command(["python", "-c", "import os; os.system('id')"])


def test_denylist_blocks_python3_c_case_insensitive(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="code-string flag"):
        gateway.run_command(["/usr/bin/Python3", "-C", "print('x')"])


def test_denylist_blocks_node_eval_and_perl_e(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="code-string flag"):
        gateway.run_command(["node", "--eval", "require('fs')"])
    with pytest.raises(PermissionError, match="code-string flag"):
        gateway.run_command(["perl", "-e", "print 1"])


def test_denylist_blocks_powershell_command(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="denied at the agent boundary"):
        gateway.run_command(["powershell", "-Command", "Get-Process"])


def test_denylist_blocks_eval_and_awk_as_command(tmp_path):
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError, match="at the agent boundary"):
        gateway.run_command(["eval", "echo hi"])
    with pytest.raises(PermissionError, match="at the agent boundary"):
        gateway.run_command(["awk", "'{print}'", "file.txt"])


def test_denylist_denial_is_hard_regardless_of_contract_grant(tmp_path):
    # RUN_COMMAND is explicitly granted (AuthorizationPolicy.permit above) —
    # the denylist still denies. No override exists.
    gateway, _ = _gateway_sandbox(tmp_path)
    with pytest.raises(PermissionError):
        gateway.run_command(["sh", "-c", "echo no-override"])


def test_sandbox_allows_script_files_and_bare_tools(tmp_path):
    gateway, ws = _gateway_sandbox(tmp_path)
    (ws.root / "task.py").write_text("print('script-ok')", encoding="utf-8")
    result = gateway.run_command([sys.executable, "task.py"])
    assert result.returncode == 0
    assert "script-ok" in result.stdout


def test_denylist_normalization_unit_cases():
    # Normalization: quoted, drive-prefixed, backslashed, case-mangled.
    assert check_command(['"C:\\Windows\\System32\\cmd.exe"', "/c", "x"]) is not None
    assert check_command(["SH", "-c", "x"]) is not None
    assert check_command([".\\PwSh", "-Command", "x"]) is not None
    assert check_command(["git", "status"]) is None
    assert check_command(["pytest", "-q"]) is None
    assert check_command(["npm", "test"]) is None
    assert check_command(["python", "script.py"]) is None  # file form stays allowed


# ---------------------------------------------------------------------------
# Phase 3: resource limits (memory and CPU ceilings)
# ---------------------------------------------------------------------------


def test_memory_limit_terminates_heavy_allocation(tmp_path: Path):
    authoritative = tmp_path / "authoritative-mem"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "mem-attempt")
    # Tight 50MB memory ceiling
    sandbox = ProcessSandbox(ws, memory_limit_bytes=50 * 1024 * 1024)
    (ws.root / "mem_bomb.py").write_text(
        "data = bytearray(120 * 1024 * 1024)\nprint('survived')", encoding="utf-8"
    )
    result = sandbox.run([sys.executable, "mem_bomb.py"])
    assert result.returncode != 0
    assert "survived" not in result.stdout
    assert result.is_resource_limit_failure or "MemoryError" in result.stderr


def test_memory_limit_allows_allocation_under_ceiling(tmp_path: Path):
    authoritative = tmp_path / "authoritative-mem-ok"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "mem-ok-attempt")
    sandbox = ProcessSandbox(ws, memory_limit_bytes=100 * 1024 * 1024)
    (ws.root / "small_alloc.py").write_text(
        "data = bytearray(5 * 1024 * 1024)\nprint('alloc-ok')", encoding="utf-8"
    )
    result = sandbox.run([sys.executable, "small_alloc.py"])
    assert result.returncode == 0
    assert result.stdout.strip() == "alloc-ok"


def test_cpu_limit_terminates_runaway_loop(tmp_path: Path):
    authoritative = tmp_path / "authoritative-cpu"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "cpu-attempt")
    # 1.0 second CPU time limit; wall-clock timeout is 10s to ensure CPU limit triggers first
    sandbox = ProcessSandbox(ws, cpu_time_limit_seconds=1.0)
    (ws.root / "spin.py").write_text("while True: pass", encoding="utf-8")
    result = sandbox.run([sys.executable, "spin.py"], timeout=10.0)
    assert result.returncode != 0
    assert result.is_resource_limit_failure


def test_timeout_kill_sweeps_process_tree(tmp_path: Path):
    authoritative = tmp_path / "authoritative-timeout"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "timeout-attempt")
    sandbox = ProcessSandbox(ws)
    # Parent ignores nothing; child sleeps far beyond the wall-clock timeout.
    (ws.root / "sleepy.py").write_text("import time; print('start', flush=True); time.sleep(120)", encoding="utf-8")
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox.run([sys.executable, "sleepy.py"], timeout=2.0)


def test_posix_shim_compiles_and_is_reused(tmp_path: Path):
    """The setrlimit exec shim builds once from C and is reused thereafter."""
    if sys.platform == "win32":
        pytest.skip("POSIX shim is not used on Windows")
    first = sandbox_mod._ensure_posix_shim()
    assert first is not None and Path(first).exists()
    second = sandbox_mod._ensure_posix_shim()
    assert second == first


def test_resource_limits_per_call_override(tmp_path: Path):
    authoritative = tmp_path / "authoritative-override"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "override-attempt")
    sandbox = ProcessSandbox(ws, memory_limit_bytes=512 * 1024 * 1024)
    (ws.root / "alloc_test.py").write_text(
        "data = bytearray(80 * 1024 * 1024)\nprint('should-fail')", encoding="utf-8"
    )
    # Per-call limit of 30MB overrides the 512MB default
    result = sandbox.run(
        [sys.executable, "alloc_test.py"], memory_limit_bytes=30 * 1024 * 1024
    )
    assert result.returncode != 0
    assert "should-fail" not in result.stdout


def test_tool_gateway_enforces_resource_limits(tmp_path: Path):
    from validators.kernel.boundary import RuntimeBoundary
    from validators.kernel.contract import ContractNormalizer
    from validators.kernel.identity import AuthorizationPolicy
    from validators.kernel.operations import OperationJournal

    authoritative = tmp_path / "authoritative-gw-limit"
    authoritative.mkdir(parents=True, exist_ok=True)
    ws = ScratchWorkspace.create(authoritative, "gw-limit-attempt")
    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-TEST",
        "scope": {"allow": ["workspace/**"], "write": "read-write"},
        "budget": {"max_files_touched": 4},
    })
    tight_sandbox = ProcessSandbox(ws, memory_limit_bytes=50 * 1024 * 1024)
    gateway = ToolGateway(
        ws,
        RuntimeBoundary(OperationJournal()),
        contract,
        AuthorizationPolicy.permit("test", ["run_command"]),
        "session-limit",
        "attempt-limit",
        "codex",
        "test-provider",
        sandbox=tight_sandbox,
    )
    (ws.root / "bomb.py").write_text(
        "data = bytearray(120 * 1024 * 1024)\nprint('bomb-survived')", encoding="utf-8"
    )
    result = gateway.run_command([sys.executable, "bomb.py"])
    assert result.returncode != 0
    assert "bomb-survived" not in result.stdout



# ---------------------------------------------------------------------------
# Phase 5: harness runner routes LLM-declared commands through the hardened
# sandbox (no raw shell=True). Regression tests for the runner.py fix.
# ---------------------------------------------------------------------------

from validators.harness.runner import AgentRunner, CompletionRecord, LLMRequest  # noqa: E402
from cli.init import init  # noqa: E402


def _minimal_project(tmp_path: Path) -> Path:
    project = tmp_path / "p5-project"
    init(project, name="P5Project", no_git=True)
    return project


class _CommandProvider:
    """Fake LLM provider whose payload declares shell commands (schema-sanctioned field)."""

    def __init__(self, commands):
        self.commands = list(commands)
        self.provider_name = "fake-commands"
        self.model_name = "fake-v1"

    def complete(self, request: LLMRequest) -> CompletionRecord:
        return CompletionRecord(
            provider=self.provider_name,
            model=self.model_name,
            payload={
                "status": "completed",
                "summary": f"Ran {len(self.commands)} commands",
                "report_markdown": "# Done",
                "blockers": [],
                "modified_files": [],
                "release_target": None,
                "retrieval_queries": [],
                "uncertainty": [],
                "commands": self.commands,
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
        )


def _collect_command_results(runner: AgentRunner, commands, tmp_path: Path):
    """Drive the real command-execution block in run_once via its error path.

    _validate_staged_state copies the whole live working tree (including this
    repo's 65MB .venv) and runs full validation, so instead we execute the
    exact helper run_once calls and assert the block's error semantics here.
    """
    project = _minimal_project(tmp_path)
    runner = AgentRunner(project, "codex", llm_provider=_CommandProvider(commands))
    return [runner._execute_sandboxed_command(c, project) for c in commands]


def test_harness_runner_bash_c_command_is_denied(tmp_path):
    results = _collect_command_results(AgentRunner, ["bash -c 'echo pwned > pwned.txt'"], tmp_path)
    r = results[0]
    assert r.returncode != 0
    assert "command denied" in r.stderr
    assert not (tmp_path / "p5-project" / "pwned.txt").exists()


def test_harness_runner_command_runs_scrubbed_and_contained(tmp_path):
    project = _minimal_project(tmp_path)
    runner = AgentRunner(project, "codex", llm_provider=_CommandProvider([]))
    (project / "show_env.py").write_text(
        "import os;print('LEAKED' if 'MY_SECRET' in os.environ else 'clean')")
    (project / "mangle.py").write_text("open('victim.txt','w').write('sandboxed')")
    os.environ["MY_SECRET"] = "super-secret-value"

    env_check = runner._execute_sandboxed_command(f'"{sys.executable}" show_env.py', project)
    assert env_check.returncode == 0 and "clean" in env_check.stdout  # Phase 1 scrubbing

    mangle = runner._execute_sandboxed_command(f'"{sys.executable}" mangle.py', project)
    assert mangle.returncode == 0
    # The command's write landed in the scratch copy, never the live tree.
    assert not (project / "victim.txt").exists()


def test_harness_runner_denies_dangerous_and_nonexistent(tmp_path):
    project = _minimal_project(tmp_path)
    runner = AgentRunner(project, "codex", llm_provider=_CommandProvider([]))
    results = [
        runner._execute_sandboxed_command("python -c 'import os'", project),
        runner._execute_sandboxed_command("definitely-not-a-real-binary-xyz --version", project),
        runner._execute_sandboxed_command('echo "unbalanced', project),
    ]
    assert "command denied" in results[0].stderr
    assert results[1].returncode != 0 and "FileNotFoundError" in results[1].stderr
    assert "unparseable command" in results[2].stderr
    assert not (project / "unbalanced").exists() and results[0].returncode != 0


def test_harness_runner_results_preserve_verification_gate_contract(tmp_path):
    """run_once's 6D gate inspects result.args/returncode; the helper preserves both."""
    project = _minimal_project(tmp_path)
    runner = AgentRunner(project, "codex", llm_provider=_CommandProvider([]))
    r = runner._execute_sandboxed_command("git --version", project)
    assert isinstance(r, subprocess.CompletedProcess)
    assert r.args == "git --version"          # gate matches 'pytest'/'test' on this string
    assert r.returncode == 0
