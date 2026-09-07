"""Dynamic Staleness, Empirical Decay, and Revalidation Engine for Procedural Skills.

Implements Phase 8 (Staleness & Decay).
Handles:
1. Environment/Code Drift: Detects when target modules change and marks skills as STALE.
2. Empirical Decay: Updates confidence on execution feedback and downgrades failing skills.
3. Revalidation: Re-runs 3-stage verification pipeline to restore STALE skills to ACTIVE.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.skill.models import RiskTier, SkillMetrics, SkillRecord, SkillStatus
from validators.skill.store import SkillStore
from validators.verification.pipeline import VerificationPipeline


class StalenessReason(str, enum.Enum):
    CODE_DRIFT = "code_drift"
    LOW_CONFIDENCE = "low_confidence"
    HIGH_FAILURE_RATE = "high_failure_rate"
    MISSING_TARGET_FILES = "missing_target_files"
    MANUAL_FLAG = "manual_flag"


@dataclass(frozen=True)
class StalenessReport:
    """Diagnostic report for a stale or decayed skill."""

    skill_id: str
    skill_name: str
    version: int
    current_status: SkillStatus
    reasons: tuple[StalenessReason, ...]
    details: tuple[str, ...]
    confidence_score: float
    recommended_action: str  # 'downgrade_to_stale', 'deprecate', 'revalidate'

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence_score": round(self.confidence_score, 3),
            "current_status": self.current_status.value,
            "details": list(self.details),
            "reasons": [r.value for r in self.reasons],
            "recommended_action": self.recommended_action,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "version": self.version,
        }


class SkillDecayManager:
    """Manages dynamic confidence decay, code drift detection, and revalidation."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.skill_store = SkillStore(self.project_path)

    def record_feedback(
        self,
        skill_name: str,
        *,
        success: bool,
        error_message: str | None = None,
        version: int | None = None,
    ) -> SkillRecord:
        """Record runtime execution outcome and update confidence/metrics with decay."""
        skill = self.skill_store.get_skill(skill_name, version=version)
        if skill is None:
            raise ValueError(f"Skill '{skill_name}' not found.")

        now_iso = datetime.now(timezone.utc).isoformat()
        current_metrics = skill.metrics

        if success:
            new_success_count = current_metrics.success_count + 1
            new_failure_count = current_metrics.failure_count
            # Positive reinforcement with diminishing returns
            new_confidence = min(0.99, current_metrics.confidence_score + (0.05 * (1.0 - current_metrics.confidence_score)))
        else:
            new_success_count = current_metrics.success_count
            new_failure_count = current_metrics.failure_count + 1
            # Aggressive empirical decay penalty (-0.20 per failure)
            new_confidence = max(0.0, current_metrics.confidence_score - 0.20)

        total_runs = new_success_count + new_failure_count
        new_rate = new_success_count / total_runs if total_runs > 0 else 1.0

        updated_metrics = SkillMetrics(
            success_count=new_success_count,
            failure_count=new_failure_count,
            success_rate=round(new_rate, 3),
            last_used_at=now_iso,
            confidence_score=round(new_confidence, 3),
        )

        # Check if decay triggers automatic status downgrade
        new_status = skill.status
        if skill.status == SkillStatus.ACTIVE:
            if new_confidence < 0.50 or (total_runs >= 3 and new_rate < 0.60):
                new_status = SkillStatus.STALE

        updated_record = SkillRecord(
            skill_id=skill.skill_id,
            name=skill.name,
            version=skill.version,
            status=new_status,
            risk_tier=skill.risk_tier,
            description=skill.description,
            applicability=skill.applicability,
            steps=skill.steps,
            constraints=skill.constraints,
            fallback_procedure=skill.fallback_procedure,
            provenance=skill.provenance,
            metrics=updated_metrics,
            schema_version=skill.schema_version,
        )

        self.skill_store.save_version(updated_record)

        # If downgraded to STALE, remove active pointer so it is immediately excluded from retrieval
        if new_status == SkillStatus.STALE and skill.status == SkillStatus.ACTIVE:
            active_file = self.skill_store.active_dir / f"{skill.name}.json"
            if active_file.exists():
                active_file.unlink()

        return updated_record

    def audit_staleness(
        self,
        skill_name: str | None = None,
        *,
        auto_downgrade: bool = True,
    ) -> list[StalenessReport]:
        """Audit active skills for code drift, missing modules, or confidence decay."""
        if skill_name:
            skill = self.skill_store.get_skill(skill_name)
            skills = [skill] if skill else []
        else:
            skills = self.skill_store.list_skills(status=SkillStatus.ACTIVE)

        reports: list[StalenessReport] = []

        for s in skills:
            reasons: list[StalenessReason] = []
            details: list[str] = []

            # 1. Check empirical confidence & failure rate
            if s.metrics.confidence_score < 0.50:
                reasons.append(StalenessReason.LOW_CONFIDENCE)
                details.append(f"Confidence score {s.metrics.confidence_score:.2f} is below 0.50 minimum threshold.")

            total_runs = s.metrics.success_count + s.metrics.failure_count
            if total_runs >= 3 and s.metrics.success_rate < 0.60:
                reasons.append(StalenessReason.HIGH_FAILURE_RATE)
                details.append(f"Success rate {s.metrics.success_rate:.1%} is below 60% threshold ({s.metrics.failure_count} failures).")

            # 2. Check target module drift / existence
            for mod in s.applicability.target_modules:
                if mod in {"*", "**"}:
                    continue
                clean_path = mod.rstrip("/*").rstrip("*")
                mod_path = self.project_path / clean_path
                if not mod_path.exists() and not any(self.project_path.glob(mod)):
                    reasons.append(StalenessReason.MISSING_TARGET_FILES)
                    details.append(f"Target module pattern '{mod}' does not match any existing project files.")
                    break

            if reasons:
                action = "downgrade_to_stale"
                if StalenessReason.LOW_CONFIDENCE in reasons and s.metrics.failure_count >= 5:
                    action = "deprecate"

                report = StalenessReport(
                    skill_id=s.skill_id,
                    skill_name=s.name,
                    version=s.version,
                    current_status=s.status,
                    reasons=tuple(reasons),
                    details=tuple(details),
                    confidence_score=s.metrics.confidence_score,
                    recommended_action=action,
                )
                reports.append(report)

                if auto_downgrade and s.status == SkillStatus.ACTIVE:
                    self.downgrade_to_stale(s.name, s.version, reason="; ".join(details))

        return reports

    def downgrade_to_stale(
        self,
        skill_name: str,
        version: int | None = None,
        *,
        reason: str = "Flagged as stale due to code drift or confidence decay",
    ) -> SkillRecord:
        """Explicitly downgrade an active skill to STALE status and remove active pointer."""
        skill = self.skill_store.get_skill(skill_name, version=version)
        if skill is None:
            raise ValueError(f"Skill '{skill_name}' not found.")

        stale_record = SkillRecord(
            skill_id=skill.skill_id,
            name=skill.name,
            version=skill.version,
            status=SkillStatus.STALE,
            risk_tier=skill.risk_tier,
            description=skill.description,
            applicability=skill.applicability,
            steps=skill.steps,
            constraints=skill.constraints,
            fallback_procedure=skill.fallback_procedure,
            provenance=skill.provenance,
            metrics=skill.metrics,
            schema_version=skill.schema_version,
        )

        self.skill_store.save_version(stale_record)

        # Remove from active directory
        active_file = self.skill_store.active_dir / f"{skill.name}.json"
        if active_file.exists():
            active_file.unlink()

        return stale_record

    def revalidate_skill(
        self,
        skill_name: str,
        version: int | None = None,
    ) -> tuple[bool, SkillRecord, str]:
        """Re-run verification pipeline on a STALE skill to restore ACTIVE status or DEPRECATE."""
        skill = self.skill_store.get_skill(skill_name, version=version)
        if skill is None:
            raise ValueError(f"Skill '{skill_name}' not found.")

        pipeline_result = VerificationPipeline.verify_skill(skill, self.project_path)

        if pipeline_result.passed:
            # Restore to ACTIVE with confidence refresh
            refreshed_metrics = SkillMetrics(
                success_count=skill.metrics.success_count,
                failure_count=skill.metrics.failure_count,
                success_rate=skill.metrics.success_rate,
                last_used_at=datetime.now(timezone.utc).isoformat(),
                confidence_score=max(0.75, skill.metrics.confidence_score),
            )
            reactivated = SkillRecord(
                skill_id=skill.skill_id,
                name=skill.name,
                version=skill.version,
                status=SkillStatus.ACTIVE,
                risk_tier=skill.risk_tier,
                description=skill.description,
                applicability=skill.applicability,
                steps=skill.steps,
                constraints=skill.constraints,
                fallback_procedure=skill.fallback_procedure,
                provenance=skill.provenance,
                metrics=refreshed_metrics,
                schema_version=skill.schema_version,
            )
            self.skill_store.save_version(reactivated)
            # Re-create active pointer
            self.skill_store.promote_version(skill.name, skill.version, skip_pipeline=True, skip_governor=True)
            return True, reactivated, "Revalidation succeeded: Skill restored to ACTIVE status."
        else:
            # Deprecate skill
            deprecated = self.skill_store.deprecate_skill(
                skill.name,
                version=skill.version,
                reason=f"Revalidation failed: {pipeline_result.receipt_id}",
            )
            return False, deprecated, f"Revalidation failed (Score: {pipeline_result.overall_score:.2f}): Skill DEPRECATED."
