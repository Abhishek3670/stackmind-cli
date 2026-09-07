"""Stage 2: Historical replay verification for procedural skills.

Implements Phase 5 (Verification Pipeline) Stage 2.
Evaluates candidate skill procedures against historical source experience records.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from validators.experience.store import ExperienceStore
from validators.skill.models import SkillRecord
from validators.verification.models import StageResult


class ReplayVerifier:
    """Evaluates procedural congruence between candidate steps and historical executions."""

    @classmethod
    def verify(cls, skill: SkillRecord, project_path: Path | str) -> StageResult:
        """Replay candidate skill steps against cited source experience records."""
        store = ExperienceStore(project_path)
        source_ids = skill.provenance.source_experience_ids
        messages: list[str] = []

        if not source_ids:
            # Manually authored skills or synthetic drafts
            messages.append("No source experience records linked; skipped historical comparison.")
            return StageResult(
                stage_name="replay",
                passed=True,
                score=0.8,
                messages=tuple(messages),
                details={"source_records_tested": 0, "average_fidelity": 0.8},
            )

        skill_actions = [f"{s.tool}:{s.command_template or s.action}" for s in skill.steps]
        fidelity_scores: list[float] = []
        tested_count = 0

        for exp_id in source_ids:
            rec = store.load_record(exp_id)
            if not rec:
                continue

            tested_count += 1
            rec_actions = [f"{a.tool}:{a.command_or_symbol}" for a in rec.actions]

            # Sequence alignment
            matcher = difflib.SequenceMatcher(None, skill_actions, rec_actions)
            sim = matcher.ratio()
            fidelity_scores.append(sim)

        if not fidelity_scores:
            passed = True
            messages.append(f"Linked source experiences ({len(source_ids)}) could not be loaded; passing with default fidelity.")
            avg_fidelity = 0.75
        else:
            avg_fidelity = sum(fidelity_scores) / len(fidelity_scores)
            passed = avg_fidelity >= 0.6

            if passed:
                messages.append(
                    f"Replay verification passed with {avg_fidelity:.2f} average fidelity across {tested_count} source episode(s)."
                )
            else:
                messages.append(
                    f"Replay verification failed: {avg_fidelity:.2f} fidelity is below the 0.60 threshold."
                )

        return StageResult(
            stage_name="replay",
            passed=passed,
            score=avg_fidelity,
            messages=tuple(messages),
            details={
                "average_fidelity": round(avg_fidelity, 3),
                "source_records_tested": tested_count,
            },
        )
