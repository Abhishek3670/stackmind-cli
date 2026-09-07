"""Provider-neutral evidence models for analysis-derived graph edges."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

RELATION_FLOWS_TO = "FLOWS_TO"
SUPPORTED_ANALYSIS_RELATIONS = frozenset({RELATION_FLOWS_TO})


@dataclass(frozen=True)
class AnalysisEvidence:
    """One provider's evidence supporting a graph relationship."""

    provider: str
    evidence_type: str
    confidence: float
    run_id: str
    analyzer_version: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnalysisEvidence":
        return cls(
            provider=str(value.get("provider", "")),
            evidence_type=str(value.get("evidence_type", "")),
            confidence=float(value.get("confidence", 0.0)),
            run_id=str(value.get("run_id", "")),
            analyzer_version=str(value.get("analyzer_version", "")),
            metadata=dict(value.get("metadata", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer_version": self.analyzer_version,
            "confidence": self.confidence,
            "evidence_type": self.evidence_type,
            "metadata": dict(sorted(self.metadata.items())),
            "provider": self.provider,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ObservedRelationship:
    """External analyzer relationship before SKC node identity resolution."""

    source_external_id: str
    target_external_id: str
    edge_kind: str
    evidence: list[AnalysisEvidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        evidence = [
            item if isinstance(item, AnalysisEvidence) else AnalysisEvidence.from_dict(item)
            for item in self.evidence
        ]
        object.__setattr__(self, "evidence", evidence)


class AnalysisProvider(Protocol):
    """Produces observed relationships for later deterministic normalization."""

    name: str

    def analyze(self) -> list[ObservedRelationship]:
        """Return provider observations without mutating SKC state."""
