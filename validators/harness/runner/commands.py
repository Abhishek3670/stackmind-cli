"""Governed and sandboxed command execution.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .models import (
    GovernedToolRuntime,
)

class _CommandsMixin:
    """Governed and sandboxed command execution."""

    def _execute_governed_command(
        self, cmd: str, staged_root: Path, tool_runtime: GovernedToolRuntime | None,
    ) -> subprocess.CompletedProcess[str]:
        if tool_runtime is None:
            return self._execute_sandboxed_command(cmd, staged_root)
        try:
            argv = shlex.split(cmd, posix=os.name != 'nt')
        except ValueError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f'unparseable command: {exc}')
        if not argv:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr='empty command')
        try:
            result = tool_runtime.gateway.run_command(argv)
            return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)
        except Exception as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f'command denied: {exc}')

    def _execute_sandboxed_command(
        self, cmd: str, staged_root: Path
    ) -> subprocess.CompletedProcess[str]:
        """Execute one LLM-declared command string through the hardened sandbox.

        Phase 5 of the sandbox hardening: the runner's former raw
        subprocess.run(cmd, shell=True) bypassed every protection that
        ToolGateway.run_command enforces. This helper routes the same command
        through the hardened path so it inherits, in order:

        1. shlex tokenization (shell=False, no shell interpolation). A string
           that cannot be tokenized (unbalanced quote) is refused outright.
        2. The Phase 2 hard interpreter denylist (bash -c / python -c / ...).
        3. The Phase 1 scrubbed environment (no daemon secrets) via
           ProcessSandbox._child_env, including env_extra (picked up by the
           POSIX limits shim; ignored on Windows).
        4. The Phase 3 resource limits (512 MB / 60 s by default; per-call
           overrides remain possible).

        The process tree is killed and marked failed when either the sandbox's
        wall-clock timeout or a hard limit terminates it; the turn continues
        (the command result is journaled and visible in the report) exactly as
        the raw path did for nonzero exits.
        """
        from validators.kernel.interpreter_denylist import check_command
        from validators.kernel.sandbox import ProcessSandbox
        from validators.kernel.workspace import ScratchWorkspace, WorkspaceEscapeError

        try:
            if os.name == "nt":
                # Windows: shlex posix mode mangles backslash paths (\U -> U),
                # and posix=False preserves quote characters inside tokens.
                # Windows quoting rules are delimiter-only, so strip the
                # surrounding quotes from each token manually.
                argv = [
                    tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok
                    for tok in shlex.split(cmd, posix=False)
                ]
            else:
                argv = shlex.split(cmd, posix=True)
        except ValueError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"unparseable command: {exc}")
        if not argv:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr="empty command")

        denial = check_command(argv)
        if denial is not None:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"command denied: {denial}")

        # Read-only scratch mirror of the staged tree: commands run against a
        # pristine copy of project HEAD (not the live working tree), and the
        # scratch workspace enforces path containment (no ../escape targets).
        scratch = ScratchWorkspace.create(staged_root, attempt_id=f"harness-{self.agent}")
        sandbox = ProcessSandbox(scratch)
        try:
            result = sandbox.run(
                argv,
                timeout=120.0,
                env_extra={
                    "STACKMIND_STAGE_ROOT": str(staged_root),
                    "STACKMIND_AGENT": self.agent,
                },
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                cmd, returncode=-9, stderr="command killed: wall-clock timeout (120s), process tree swept"
            )
        except WorkspaceEscapeError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"command refused: {exc}")
        except OSError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"{type(exc).__name__}: {exc}")
        return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)
