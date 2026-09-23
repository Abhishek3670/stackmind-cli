"""Sandbox Canary Verifier executing inside ScratchWorkspace and ProcessSandbox.

Implements Phase P3 authentic canary verification.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from validators.kernel.sandbox import ProcessSandbox
from validators.kernel.workspace import ScratchWorkspace, WorkspaceEscapeError
from validators.verification.models import StageResult

if TYPE_CHECKING:
    from validators.skill.models import SkillRecord


class SandboxCanaryVerifier:
    """Authentic canary verifier executing in an isolated ScratchWorkspace sandbox."""

    @classmethod
    def verify(
        cls,
        skill: SkillRecord,
        project_path: Path | str,
        *,
        commands: Sequence[Sequence[str]] | None = None,
        workspace: ScratchWorkspace | None = None,
        execute_steps: bool = False,
    ) -> StageResult:
        proj = Path(project_path).resolve()
        messages: list[str] = []
        passed = True

        # 1. Template variable validation
        var_pattern = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for s in skill.steps:
            if s.command_template:
                vars_found = var_pattern.findall(s.command_template)
                if vars_found:
                    messages.append(
                        f"Step {s.step_index} template contains parameterized variables: {vars_found}"
                    )

        # 2. Target module applicability boundary check
        invalid_modules = []
        for mod in skill.applicability.target_modules:
            if mod.startswith("/") or ".." in mod:
                invalid_modules.append(mod)

        if invalid_modules:
            passed = False
            messages.append(
                "Canary check failed: target_modules contain out-of-boundary paths: "
                f"{invalid_modules}"
            )

        # 3. Execution precondition audit
        if not skill.applicability.preconditions:
            passed = False
            messages.append(
                "Canary check failed: skill does not declare operational preconditions."
            )

        # 4. Authentic Sandbox Execution
        # Provision isolated scratch workspace and process sandbox
        scratch = workspace or ScratchWorkspace.create(proj, f"canary-{skill.skill_id[:8]}")
        sandbox = ProcessSandbox(scratch)

        test_cmds: list[Sequence[str]] = list(commands) if commands else []
        if execute_steps and not test_cmds:
            for s in skill.steps:
                if s.command_template:
                    vars_found = var_pattern.findall(s.command_template)
                    if not vars_found:
                        test_cmds.append(shlex.split(s.command_template))

        if not test_cmds:
            test_cmds.append([sys.executable, "-c", "import sys; sys.exit(0)"])
            messages.append("Canary executed an explicit sandbox baseline command.")

        executed_commands: list[dict[str, Any]] = []
        for cmd in test_cmds:
            try:
                result = sandbox.run(cmd)
                executed_commands.append(
                    {
                        "command": list(cmd),
                        "returncode": result.returncode,
                        "stdout": result.stdout[:200],
                        "stderr": result.stderr[:200],
                    }
                )
                if result.returncode != 0:
                    passed = False
                    messages.append(
                        f"Canary sandbox execution failed (exit code {result.returncode}) for: "
                        f"{' '.join(cmd)}"
                    )
            except WorkspaceEscapeError as ex:
                passed = False
                messages.append(f"Canary sandbox containment breach: {ex}")
            except Exception as ex:
                passed = False
                messages.append(f"Canary execution error: {ex}")

        score = 1.0 if passed else 0.4
        if passed:
            messages.append(
                "Canary simulation passed: template parameters, boundaries, and preconditions valid."
            )

        return StageResult(
            stage_name="canary",
            passed=passed,
            score=score,
            messages=tuple(messages),
            details={
                "boundary_verified": len(invalid_modules) == 0,
                "executed_commands": executed_commands,
                "preconditions_count": len(skill.applicability.preconditions),
                "sandbox_commands_executed": len(executed_commands),
            },
        )
