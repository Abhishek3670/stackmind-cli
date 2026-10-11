"""Execution runner for runtime verification contracts.

Safely executes verification contracts in a sandboxed environment.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from validators.harness.execution_contract import (
    ExecutionContract,
    ExecutionResult,
    VerificationStatus,
)
from validators.kernel.sandbox import ProcessSandbox, CommandResult
from validators.kernel.workspace import ScratchWorkspace


class ExecutionRunner:
    """Runs execution verification contracts in a sandbox."""

    def __init__(self, project_path: Path | str):
        self.project_path = Path(project_path).resolve()

    def _compute_contract_hash(self, contract: ExecutionContract) -> str:
        """Compute deterministic hash of contract for stale detection."""
        data = json.dumps(contract.to_dict(), sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def _check_dependencies(self, contract: ExecutionContract) -> tuple[bool, str]:
        """Check if required dependencies are available."""
        for dep in contract.requires_dependencies:
            # Simple check: is it on PATH or is it a Python module?
            try:
                if dep.endswith(".py") or "." in dep:
                    # Try as Python module
                    import importlib
                    importlib.import_module(dep.split(".")[0])
                else:
                    # Try as command on PATH
                    import subprocess
                    result = subprocess.run(
                        ["which", dep] if sys.platform != "win32" else ["where", dep],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode != 0:
                        return False, f"Dependency '{dep}' not found"
            except (ImportError, subprocess.TimeoutExpired):
                return False, f"Dependency '{dep}' not available"
        return True, ""

    def _run_command_in_sandbox(
        self,
        contract: ExecutionContract,
        scratch: ScratchWorkspace,
    ) -> tuple[CommandResult, float]:
        """Run command in sandbox with timeout."""
        sandbox = ProcessSandbox(scratch)
        cmd_list = list(contract.command)

        start = time.perf_counter()
        try:
            result = sandbox.run(
                cmd_list,
                timeout=contract.timeout_seconds,
                env_extra=contract.env_vars,
            )
            duration = time.perf_counter() - start
            return result, duration
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start
            raise

    def verify_contract(self, contract: ExecutionContract) -> ExecutionResult:
        """Execute a single verification contract."""
        contract_hash = self._compute_contract_hash(contract)

        # Check dependencies
        deps_ok, deps_error = self._check_dependencies(contract)
        if not deps_ok:
            return ExecutionResult(
                requirement_id=contract.requirement_id,
                criterion_id=contract.criterion_id,
                contract_hash=contract_hash,
                command=contract.command,
                working_directory=contract.working_directory,
                timeout_seconds=contract.timeout_seconds,
                status=VerificationStatus.BLOCKED,
                exit_code=None,
                timed_out=False,
                duration_ms=0,
                stdout="",
                stderr=deps_error,
                files_created=(),
                files_modified=(),
            )

        # Create scratch workspace
        scratch = ScratchWorkspace.create(
            self.project_path,
            f"exec-{contract.requirement_id[:8]}-{contract.criterion_id[:8]}"
        )

        try:
            # Run command
            result, duration = self._run_command_in_sandbox(contract, scratch)

            # Determine status
            if result.returncode != contract.expected_exit_code:
                status = VerificationStatus.FAIL
            else:
                status = VerificationStatus.PASS

            # Check stdout/stderr expectations
            for expected in contract.expected_stdout_contains:
                if expected not in result.stdout:
                    status = VerificationStatus.FAIL
                    break

            for expected in contract.expected_stderr_contains:
                if expected not in result.stderr:
                    status = VerificationStatus.FAIL
                    break

            # Check file expectations
            files_created: list[str] = []
            files_modified: list[str] = []
            for expected_file in contract.expected_files_created:
                full_path = scratch.root / expected_file
                if full_path.exists():
                    files_created.append(expected_file)
                else:
                    status = VerificationStatus.FAIL

            return ExecutionResult(
                requirement_id=contract.requirement_id,
                criterion_id=contract.criterion_id,
                contract_hash=contract_hash,
                command=contract.command,
                working_directory=contract.working_directory,
                timeout_seconds=contract.timeout_seconds,
                status=status,
                exit_code=result.returncode,
                timed_out=False,
                duration_ms=duration * 1000,
                stdout=result.stdout[:10000],  # Truncate large outputs
                stderr=result.stderr[:10000],
                files_created=tuple(files_created),
                files_modified=tuple(files_modified),
                source_fingerprint=None,
                config_fingerprint=None,
            )

        except subprocess.TimeoutExpired:
            return ExecutionResult(
                requirement_id=contract.requirement_id,
                criterion_id=contract.criterion_id,
                contract_hash=contract_hash,
                command=contract.command,
                working_directory=contract.working_directory,
                timeout_seconds=contract.timeout_seconds,
                status=VerificationStatus.FAIL,
                exit_code=None,
                timed_out=True,
                duration_ms=contract.timeout_seconds * 1000,
                stdout="",
                stderr="Command timed out",
                files_created=(),
                files_modified=(),
            )

        except Exception as ex:
            return ExecutionResult(
                requirement_id=contract.requirement_id,
                criterion_id=contract.criterion_id,
                contract_hash=contract_hash,
                command=contract.command,
                working_directory=contract.working_directory,
                timeout_seconds=contract.timeout_seconds,
                status=VerificationStatus.BLOCKED,
                exit_code=None,
                timed_out=False,
                duration_ms=0,
                stdout="",
                stderr=f"Execution error: {ex}",
                files_created=(),
                files_modified=(),
            )
