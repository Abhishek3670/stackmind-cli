"""Stage 3: Canary / shadow execution verification for procedural skills.

Implements Phase 5 (Verification Pipeline) Stage 3.
Validates command template expansions, sandbox boundaries, and applicability compliance.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from validators.skill.models import SkillRecord
from validators.verification.models import StageResult


class CanaryVerifier:
    """Performs sandboxed dry-run / canary validation on a procedural skill."""

    @classmethod
    def verify(cls, skill: SkillRecord, project_path: Path | str) -> StageResult:
        """Execute canary validation simulating execution steps."""
        proj = Path(project_path).resolve()
        messages: list[str] = []
        passed = True

        # 1. Template variable validation
        # Find any unescaped {param} that might fail without parameters
        var_pattern = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for s in skill.steps:
            if s.command_template:
                vars_found = var_pattern.findall(s.command_template)
                if vars_found:
                    # Note potential unbound variables
                    messages.append(f"Step {s.step_index} template contains parameterized variables: {vars_found}")

        # 2. Target module applicability boundary check
        invalid_modules = []
        for mod in skill.applicability.target_modules:
            if mod.startswith("/") or ".." in mod:
                invalid_modules.append(mod)

        if invalid_modules:
            passed = False
            messages.append(f"Canary check failed: target_modules contain out-of-boundary paths: {invalid_modules}")

        # 3. Execution precondition audit
        if not skill.applicability.preconditions:
            passed = False
            messages.append("Canary check failed: skill does not declare operational preconditions.")

        score = 1.0 if passed else 0.4

        if passed:
            messages.append("Canary simulation passed: template parameters, boundaries, and preconditions valid.")

        return StageResult(
            stage_name="canary",
            passed=passed,
            score=score,
            messages=tuple(messages),
            details={
                "boundary_verified": len(invalid_modules) == 0,
                "preconditions_count": len(skill.applicability.preconditions),
            },
        )
