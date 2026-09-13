"""Stage 3: Canary / shadow execution verification for procedural skills.

Implements Phase 5 & Phase P3 Canary Verification executing in ScratchWorkspace sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from validators.verification.models import StageResult

if TYPE_CHECKING:
    from validators.skill.models import SkillRecord


class CanaryVerifier:
    """Performs authentic sandboxed canary validation on a procedural skill."""

    @classmethod
    def verify(
        cls,
        skill: SkillRecord,
        project_path: Path | str,
        *,
        commands: Sequence[Sequence[str]] | None = None,
        execute_steps: bool = False,
    ) -> StageResult:
        from validators.kernel.verification.canary import SandboxCanaryVerifier

        return SandboxCanaryVerifier.verify(
            skill,
            project_path,
            commands=commands,
            execute_steps=execute_steps,
        )
