"""Data models and serialization for StackMind Durable Memory Records.

Implements the Memory Layer: reusable, provenance-backed facts, decisions, and lessons
extracted from verified experience records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from jsonschema import Draft7Validator

from validators.experience.models import ExperienceRecord
from validators.harness.snapshot import TrustLevel
from validators.knowledge.registry import node_id_for


class MemoryType(str, Enum):
    """Classification of durable memory content."""

    FACT = "fact"
    DECISION = "decision"
    LESSON = "lesson"
    PREFERENCE = "preference"
    PATTERN = "pattern"


class MemoryStatus(str, Enum):
    """Lifecycle states for durable memories."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


@dataclass(frozen=True)
class MemoryProvenance:
    """Audit trail connecting memory to source experience(s)."""

    source_experience_ids: tuple[str, ...]
    extraction_method: str  # "manual", "compiler", "llm_distillation"
    extracted_at: str
    extractor_agent: str
    confidence: float  # 0.0 - 1.0, how well evidence supports the memory claim

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_experience_ids": list(self.source_experience_ids),
            "extraction_method": self.extraction_method,
            "extracted_at": self.extracted_at,
            "extractor_agent": self.extractor_agent,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryProvenance:
        return cls(
            source_experience_ids=tuple(data.get("source_experience_ids", ())),
            extraction_method=str(data.get("extraction_method", "manual")),
            extracted_at=str(data.get("extracted_at", datetime.now(timezone.utc).isoformat())),
            extractor_agent=str(data.get("extractor_agent", "memory-compiler")),
            confidence=float(data.get("confidence", 0.5)),
        )


@dataclass(frozen=True)
class MemoryEvidence:
    """Specific evidence from source experience supporting the memory."""

    experience_id: str
    evidence_type: str  # "verification_dimension", "action_outcome", "correction", "final_state"
    excerpt: str
    dimension_scores: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "evidence_type": self.evidence_type,
            "excerpt": self.excerpt,
            "dimension_scores": self.dimension_scores,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEvidence:
        return cls(
            experience_id=str(data.get("experience_id", "")),
            evidence_type=str(data.get("evidence_type", "")),
            excerpt=str(data.get("excerpt", "")),
            dimension_scores=dict(data.get("dimension_scores", {})),
        )


@dataclass(frozen=True)
class MemoryRecord:
    """Canonical durable memory artifact extracted from verified experiences."""

    memory_id: str
    memory_type: MemoryType
    content: str
    normalized_content: str  # Search-optimized representation
    project_id: str
    provenance: MemoryProvenance
    evidence: tuple[MemoryEvidence, ...] = field(default_factory=tuple)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_validated_at: Optional[str] = None
    expires_at: Optional[str] = None
    superseded_by: Optional[str] = None  # memory_id of replacement
    supersedes: tuple[str, ...] = field(default_factory=tuple)  # memory_ids this replaces
    tags: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = 1

    @classmethod
    def mint_id(cls, project_id: str, content_hash: str) -> str:
        """Mint a deterministic NodeID for a memory record."""
        return node_id_for("memory", f"{project_id}:{content_hash}")

    @classmethod
    def from_experience(
        cls,
        project_id: str,
        experience: ExperienceRecord,
        memory_type: MemoryType,
        content: str,
        normalized_content: str,
        evidence: tuple[MemoryEvidence, ...],
        extractor_agent: str = "memory-compiler",
        confidence: float = 0.5,
    ) -> MemoryRecord:
        """Create a memory candidate from an experience record."""
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(f"{project_id}:{normalized_content}".encode()).hexdigest()[:16]
        memory_id = cls.mint_id(project_id, content_hash)

        # Eligibility: only from learning-eligible, completed experiences with sufficient evidence
        if not experience.learning_eligible or experience.outcome != "completed":
            raise ValueError(f"Experience {experience.experience_id} not eligible for memory extraction")

        # Confidence based on trust level and verification dimensions
        trust_bonus = {
            TrustLevel.OBSERVABLE: 0.1,
            TrustLevel.VERIFIED: 0.3,
            TrustLevel.LEARNING_ELIGIBLE: 0.5,
        }.get(experience.trust_level, 0.0)

        dim = experience.verification.dimensions
        verification_bonus = sum([
            dim.scope_verified,
            dim.state_verified,
            dim.code_verified,
            dim.behavioral_verified,
            dim.security_verified,
            dim.outcome_verified,
        ]) * 0.05  # up to 0.3

        final_confidence = min(0.95, confidence + trust_bonus + verification_bonus)

        provenance = MemoryProvenance(
            source_experience_ids=(experience.experience_id,),
            extraction_method="compiler",
            extracted_at=now,
            extractor_agent=extractor_agent,
            confidence=final_confidence,
        )

        return cls(
            memory_id=memory_id,
            memory_type=memory_type,
            content=content,
            normalized_content=normalized_content,
            project_id=project_id,
            provenance=provenance,
            evidence=evidence,
            status=MemoryStatus.CANDIDATE,
            created_at=now,
            updated_at=now,
        )

    def validate_schema(self, schema_path: Path | None = None) -> None:
        if schema_path is None:
            schema_path = Path(__file__).resolve().parent.parent.parent / "schemas" / "memory.schema.json"
        if not schema_path.exists():
            raise FileNotFoundError(f"Memory schema not found at {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema)
        errors = list(validator.iter_errors(self.to_dict()))
        if errors:
            msg = "; ".join(e.message for e in errors)
            raise ValueError(f"Memory schema validation failed: {msg}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "created_at": self.created_at,
            "evidence": [e.to_dict() for e in self.evidence],
            "expires_at": self.expires_at,
            "last_validated_at": self.last_validated_at,
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.value,
            "normalized_content": self.normalized_content,
            "project_id": self.project_id,
            "provenance": self.provenance.to_dict(),
            "schema_version": self.schema_version,
            "status": self.status.value,
            "superseded_by": self.superseded_by,
            "supersedes": list(self.supersedes),
            "tags": list(self.tags),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryRecord:
        return cls(
            memory_id=str(data["memory_id"]),
            memory_type=MemoryType(data["memory_type"]),
            content=str(data["content"]),
            normalized_content=str(data["normalized_content"]),
            project_id=str(data["project_id"]),
            provenance=MemoryProvenance.from_dict(data.get("provenance", {})),
            evidence=tuple(MemoryEvidence.from_dict(e) for e in data.get("evidence", ())),
            status=MemoryStatus(data.get("status", "candidate")),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
            last_validated_at=data.get("last_validated_at"),
            expires_at=data.get("expires_at"),
            superseded_by=data.get("superseded_by"),
            supersedes=tuple(data.get("supersedes", ())),
            tags=tuple(data.get("tags", ())),
            schema_version=int(data.get("schema_version", 1)),
        )


import hashlib