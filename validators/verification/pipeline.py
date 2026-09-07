"""Verification Pipeline orchestrator executing 3-stage validation for procedural skills.

Implements Phase 5 (Verification Pipeline).
Orchestrates:
  Stage 1: Structural Verification
  Stage 2: Historical Replay Verification
  Stage 3: Canary Simulation Verification
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validators.skill.models import SkillRecord
from validators.verification.canary import CanaryVerifier
from validators.verification.models import PipelineResult, StageResult
from validators.verification.replay import ReplayVerifier
from validators.verification.structural import StructuralVerifier


class VerificationPipeline:
    """Orchestrates the 3-stage verification gate before a skill can be promoted."""

    @classmethod
    def verify_skill(cls, skill: SkillRecord, project_path: Path | str) -> PipelineResult:
        """Execute all 3 verification stages on a skill candidate."""
        proj = Path(project_path).resolve()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Stage 1: Structural
        structural_result = StructuralVerifier.verify(skill, proj)

        # Stage 2: Replay
        replay_result = ReplayVerifier.verify(skill, proj)

        # Stage 3: Canary
        canary_result = CanaryVerifier.verify(skill, proj)

        stage_results = (structural_result, replay_result, canary_result)
        passed = all(s.passed for s in stage_results)

        # Composite score
        overall_score = (
            0.35 * structural_result.score
            + 0.35 * replay_result.score
            + 0.30 * canary_result.score
        )

        # Mint deterministic receipt ID
        raw_receipt = f"{skill.skill_id}:v{skill.version}:{now_iso}".encode("utf-8")
        receipt_id = f"RECEIPT-{hashlib.sha256(raw_receipt).hexdigest()[:16]}"

        result = PipelineResult(
            skill_id=skill.skill_id,
            skill_name=skill.name,
            version=skill.version,
            passed=passed,
            stage_results=stage_results,
            overall_score=overall_score,
            executed_at=now_iso,
            receipt_id=receipt_id,
        )

        # Save verification receipt under .sync/skills/receipts/
        receipts_dir = proj / ".sync" / "skills" / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipts_dir / f"{receipt_id}.json"
        tmp_path = receipt_path.with_suffix(".tmp")

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp_path, receipt_path)

        return result
