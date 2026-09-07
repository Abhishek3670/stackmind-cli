"""Skill distillation engine transforming validated pattern clusters into candidate skills.

Implements Phase 4 (Pattern Mining) lesson distillation.
Ensures that Candidate Skills retain strict provenance links to source experience IDs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.learning.cluster import PatternCluster
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)


class SkillDistiller:
    """Synthesizes reusable, verified procedural Skill Candidates from Pattern Clusters."""

    @classmethod
    def determine_risk_tier(cls, steps: Sequence[SkillStep]) -> RiskTier:
        """Heuristically assign an initial consequence risk tier based on tool actions."""
        high_risk_keywords = {"drop", "delete", "truncate", "rm", "format", "wipe", "force", "kill"}
        medium_risk_keywords = {"migrate", "upgrade", "deploy", "restart", "patch", "write", "install"}

        for step in steps:
            text = f"{step.action} {step.command_template or ''}".lower()
            if any(kw in text for kw in high_risk_keywords):
                return RiskTier.HIGH
            if any(kw in text for kw in medium_risk_keywords):
                return RiskTier.MEDIUM
        return RiskTier.LOW

    @classmethod
    def distill_candidate_skill(
        cls,
        cluster: PatternCluster,
        *,
        skill_name: str | None = None,
        author_agent: str = "claude",
    ) -> SkillRecord:
        """Distill a verified pattern cluster into an immutable SkillRecord (CANDIDATE)."""
        if not cluster.is_distillation_eligible:
            raise ValueError(
                f"Cluster '{cluster.cluster_id}' is not eligible for distillation "
                f"(samples={cluster.sample_count} < 3 or similarity={cluster.similarity_score:.2f} < threshold)."
            )

        name = skill_name or cluster.intent_slug
        slug = re.sub(r"[^a-z0-9_-]", "_", name.strip().lower()).strip("_")
        now_iso = datetime.now(timezone.utc).isoformat()

        # Build steps
        steps: list[SkillStep] = []
        for idx, act in enumerate(cluster.common_actions, start=1):
            steps.append(
                SkillStep(
                    step_index=idx,
                    action=act.get("action", f"Step {idx}"),
                    tool=act.get("tool", "bash"),
                    command_template=act.get("command_template"),
                    rationale=act.get("rationale"),
                    expected_outcome="Step executed successfully without error",
                )
            )

        if not steps:
            steps.append(SkillStep(step_index=1, action="Execute canonical procedure sequence", tool="bash"))

        risk_tier = cls.determine_risk_tier(steps)

        # Synthesize target modules from touched files across source experiences
        all_touched_modules = set()
        for rec in cluster.source_records:
            for f in rec.verification.observed_diff.all_changed_files:
                parts = Path(f).parts
                if len(parts) > 1:
                    all_touched_modules.add(f"{parts[0]}/*")
                else:
                    all_touched_modules.add(f)

        target_modules = tuple(sorted(all_touched_modules)) if all_touched_modules else ("*",)

        # Calculate initial candidate confidence score
        # Confidence = base (0.5) + sample bonus (up to 0.3) + similarity bonus (up to 0.2)
        sample_factor = min(cluster.sample_count / 10.0, 1.0)
        confidence = round(0.5 + (0.3 * sample_factor) + (0.2 * cluster.similarity_score), 2)

        applicability = SkillApplicability(
            preconditions=(
                "Target environment initialized and reachable",
                "Pre-execution contract boundary validated",
            ),
            target_modules=target_modules,
            known_exclusions=(),
        )

        provenance = SkillProvenance(
            source_experience_ids=cluster.experience_ids,
            created_at=now_iso,
            author_agent=author_agent,
            promotion_reason=f"Distilled from {cluster.sample_count} verified episodes with similarity {cluster.similarity_score:.2f}",
        )

        metrics = SkillMetrics(
            success_count=cluster.sample_count,
            failure_count=0,
            success_rate=1.0,
            confidence_score=confidence,
        )

        skill_id = SkillRecord.mint_id(slug, 1)

        return SkillRecord(
            skill_id=skill_id,
            name=slug,
            version=1,
            status=SkillStatus.CANDIDATE,
            risk_tier=risk_tier,
            description=f"Synthesized procedural skill for {slug.replace('_', ' ')} (distilled from {cluster.sample_count} episodes)",
            applicability=applicability,
            steps=tuple(steps),
            provenance=provenance,
            metrics=metrics,
        )
