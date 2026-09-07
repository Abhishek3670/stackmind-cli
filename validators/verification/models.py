"""Data models for the 3-Stage Skill Verification Pipeline.

Implements Phase 5 (Verification Pipeline) models and receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class StageResult:
    """Outcome of an individual verification stage."""

    stage_name: str  # 'structural', 'replay', 'canary'
    passed: bool
    score: float
    messages: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "details": self.details,
            "messages": list(self.messages),
            "passed": self.passed,
            "score": round(self.score, 3),
            "stage_name": self.stage_name,
        }


@dataclass(frozen=True)
class PipelineResult:
    """Composite outcome of the full 3-Stage Verification Pipeline."""

    skill_id: str
    skill_name: str
    version: int
    passed: bool
    stage_results: tuple[StageResult, ...]
    overall_score: float
    executed_at: str
    receipt_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed_at": self.executed_at,
            "overall_score": round(self.overall_score, 3),
            "passed": self.passed,
            "receipt_id": self.receipt_id,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "stage_results": [s.to_dict() for s in self.stage_results],
            "version": self.version,
        }
