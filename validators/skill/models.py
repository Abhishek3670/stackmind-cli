"""Data models and validation for Versioned Procedural Skill Artifacts.

Implements Phase 3 (Skill Storage & Versioning) of Verified Procedural Learning.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence
import jsonschema


class SkillStatus(str, Enum):
    """Unified reconciled lifecycle states for StackMind skills."""

    CANDIDATE = "candidate"
    EXPERIMENTAL = "experimental"
    ACTIVE = "active"
    STALE = "stale"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class RiskTier(str, Enum):
    """Consequence tiers governing promotion gates and autonomous execution."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class SkillStep:
    """One executable or guided step in a procedural skill."""

    step_index: int
    action: str
    tool: str
    command_template: str | None = None
    rationale: str | None = None
    expected_outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "command_template": self.command_template,
            "expected_outcome": self.expected_outcome,
            "rationale": self.rationale,
            "step_index": self.step_index,
            "tool": self.tool,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillStep:
        return cls(
            step_index=int(data["step_index"]),
            action=str(data["action"]),
            tool=str(data["tool"]),
            command_template=data.get("command_template"),
            rationale=data.get("rationale"),
            expected_outcome=data.get("expected_outcome"),
        )


@dataclass(frozen=True)
class SkillApplicability:
    """Contextual applicability boundary and preconditions for a skill."""

    preconditions: tuple[str, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    known_exclusions: tuple[str, ...] = ()
    target_modules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": dict(sorted(self.environment.items())),
            "known_exclusions": list(self.known_exclusions),
            "preconditions": list(self.preconditions),
            "target_modules": list(self.target_modules),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillApplicability:
        return cls(
            preconditions=tuple(data.get("preconditions", ())),
            environment=dict(data.get("environment", {})),
            known_exclusions=tuple(data.get("known_exclusions", ())),
            target_modules=tuple(data.get("target_modules", ())),
        )


@dataclass(frozen=True)
class SkillProvenance:
    """Audit trail and historical lineage connecting a skill to source experiences."""

    source_experience_ids: tuple[str, ...]
    created_at: str
    author_agent: str
    promotion_reason: str | None = None
    previous_version_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "author_agent": self.author_agent,
            "created_at": self.created_at,
            "previous_version_id": self.previous_version_id,
            "promotion_reason": self.promotion_reason,
            "source_experience_ids": list(self.source_experience_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillProvenance:
        return cls(
            source_experience_ids=tuple(data.get("source_experience_ids", ())),
            created_at=str(data["created_at"]),
            author_agent=str(data["author_agent"]),
            promotion_reason=data.get("promotion_reason"),
            previous_version_id=data.get("previous_version_id"),
        )


@dataclass(frozen=True)
class SkillMetrics:
    """Observed runtime track record and confidence score."""

    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 1.0
    last_used_at: str | None = None
    confidence_score: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence_score": self.confidence_score,
            "failure_count": self.failure_count,
            "last_used_at": self.last_used_at,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillMetrics:
        return cls(
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            success_rate=float(data.get("success_rate", 1.0)),
            last_used_at=data.get("last_used_at"),
            confidence_score=float(data.get("confidence_score", 0.5)),
        )


@dataclass(frozen=True)
class SkillRecord:
    """Versioned, verified procedural skill artifact."""

    skill_id: str
    name: str
    version: int
    status: SkillStatus
    risk_tier: RiskTier
    description: str
    applicability: SkillApplicability
    steps: tuple[SkillStep, ...]
    provenance: SkillProvenance
    metrics: SkillMetrics = field(default_factory=SkillMetrics)
    constraints: tuple[str, ...] = ()
    fallback_procedure: str | None = None
    schema_version: int = 1

    @classmethod
    def mint_id(cls, name: str, version: int) -> str:
        """Mint a deterministic SKILL- node ID from name and version."""
        raw = f"{name}:v{version}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return f"SKILL-{digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicability": self.applicability.to_dict(),
            "description": self.description,
            "metrics": self.metrics.to_dict(),
            "name": self.name,
            "procedure": {
                "constraints": list(self.constraints),
                "fallback_procedure": self.fallback_procedure,
                "steps": [step.to_dict() for step in self.steps],
            },
            "provenance": self.provenance.to_dict(),
            "risk_tier": self.risk_tier.value,
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "status": self.status.value,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillRecord:
        proc = data.get("procedure", {})
        steps = tuple(SkillStep.from_dict(s) for s in proc.get("steps", ()))
        constraints = tuple(proc.get("constraints", ()))
        fallback_procedure = proc.get("fallback_procedure")

        return cls(
            skill_id=str(data["skill_id"]),
            name=str(data["name"]),
            version=int(data["version"]),
            status=SkillStatus(data["status"]),
            risk_tier=RiskTier(data["risk_tier"]),
            description=str(data.get("description", "")),
            applicability=SkillApplicability.from_dict(data.get("applicability", {})),
            steps=steps,
            constraints=constraints,
            fallback_procedure=fallback_procedure,
            provenance=SkillProvenance.from_dict(data.get("provenance", {})),
            metrics=SkillMetrics.from_dict(data.get("metrics", {})),
            schema_version=int(data.get("schema_version", 1)),
        )

    def validate_schema(self, project_path: Path | str | None = None) -> list[str]:
        """Validate the skill record against schemas/skill.schema.json."""
        errors: list[str] = []
        schema_path = None
        if project_path:
            cand = Path(project_path) / "schemas" / "skill.schema.json"
            if cand.exists():
                schema_path = cand

        if not schema_path:
            root = Path(__file__).resolve().parent.parent.parent
            schema_path = root / "schemas" / "skill.schema.json"

        if not schema_path.exists():
            return errors

        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            validator = jsonschema.Draft7Validator(schema)
            for err in validator.iter_errors(self.to_dict()):
                errors.append(f"{err.json_path}: {err.message}")
        except Exception as exc:
            errors.append(f"Skill schema validation error: {exc}")
        return errors
