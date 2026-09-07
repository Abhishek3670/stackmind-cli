"""Analysis-provider contracts for SKC evidence."""

from .base import (
    AnalysisEvidence,
    AnalysisProvider,
    ObservedRelationship,
    RELATION_FLOWS_TO,
    SUPPORTED_ANALYSIS_RELATIONS,
)

__all__ = [
    "AnalysisEvidence",
    "AnalysisProvider",
    "ObservedRelationship",
    "RELATION_FLOWS_TO",
    "SUPPORTED_ANALYSIS_RELATIONS",
]
