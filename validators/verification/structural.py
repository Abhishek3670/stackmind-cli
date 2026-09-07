"""Stage 1: Structural verification for procedural skills.

Implements Phase 5 (Verification Pipeline) Stage 1.
Validates JSON schema, step completeness, tool legitimacy, and D025 safety.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from validators.skill.models import SkillRecord
from validators.verification.models import StageResult


class StructuralVerifier:
    """Performs static structural, syntactic, and security safety checks on a skill."""

    _ALLOWED_TOOLS = {"bash", "python", "git", "read_file", "write_file", "grep", "search"}
    _D025_DESTRUCTIVE_PATTERNS = [
        re.compile(r"rm\s+-(?:r|f|rf|fr)\s+(?:/|\*|~|\.\.)", re.IGNORECASE),
        re.compile(r"git\s+(?:reset\s+--hard|clean\s+-fdx|push\s+--force)", re.IGNORECASE),
        re.compile(r"drop\s+(?:database|table|schema)", re.IGNORECASE),
        re.compile(r"mkfs|dd\s+if=", re.IGNORECASE),
    ]

    @classmethod
    def verify(cls, skill: SkillRecord, project_path: Path | str | None = None) -> StageResult:
        """Run structural validation on a skill record."""
        messages: list[str] = []
        passed = True

        # 1. JSON Schema validation
        schema_errors = skill.validate_schema(project_path)
        if schema_errors:
            passed = False
            for err in schema_errors:
                messages.append(f"Schema violation: {err}")

        # 2. Procedure step validation
        if not skill.steps:
            passed = False
            messages.append("Procedure has no execution steps defined.")
        else:
            for s in skill.steps:
                if not s.action or not s.action.strip():
                    passed = False
                    messages.append(f"Step {s.step_index} has empty action description.")
                if s.tool not in cls._ALLOWED_TOOLS:
                    passed = False
                    messages.append(f"Step {s.step_index} specifies unrecognized tool: '{s.tool}'.")

                # 3. D025 Destructive Operations Check
                if s.command_template:
                    for pat in cls._D025_DESTRUCTIVE_PATTERNS:
                        if pat.search(s.command_template):
                            passed = False
                            messages.append(
                                f"Step {s.step_index} command '{s.command_template}' violates D025 Destructive Operations policy."
                            )

        # 4. Applicability boundary check
        if not skill.applicability.target_modules:
            passed = False
            messages.append("Applicability boundary is missing target_modules.")

        score = 1.0 if passed else max(0.0, 1.0 - (0.25 * len(messages)))

        if passed:
            messages.append("Structural and schema validation passed successfully.")

        return StageResult(
            stage_name="structural",
            passed=passed,
            score=score,
            messages=tuple(messages),
            details={
                "schema_valid": len(schema_errors) == 0,
                "step_count": len(skill.steps),
                "target_modules": list(skill.applicability.target_modules),
            },
        )
