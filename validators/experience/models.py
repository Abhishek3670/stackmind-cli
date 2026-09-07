"""Data models and serialization for StackMind Experience Records.

Implements Phase 1 (Experience Capture) of Verified Procedural Learning.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft7Validator

from validators.harness.snapshot import TrustLevel, VerificationDimensions, WorkspaceDiff
from validators.knowledge.registry import node_id_for


@dataclass(frozen=True)
class ExperienceAction:
    """A discrete tool call or command executed during a task."""

    tool: str
    command_or_symbol: str
    input_summary: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_or_symbol": self.command_or_symbol,
            "input_summary": self.input_summary,
            "timestamp": self.timestamp,
            "tool": self.tool,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceAction:
        return cls(
            tool=str(data.get("tool", "")),
            command_or_symbol=str(data.get("command_or_symbol", "")),
            input_summary=str(data.get("input_summary", "")),
            timestamp=str(data.get("timestamp", "")),
        )


@dataclass(frozen=True)
class ExperienceObservation:
    """The concrete observed result of an action."""

    output_summary: str
    is_error: bool
    exit_code: int | None = None
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "exit_code": self.exit_code,
            "is_error": self.is_error,
            "output_summary": self.output_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceObservation:
        return cls(
            output_summary=str(data.get("output_summary", "")),
            is_error=bool(data.get("is_error", False)),
            exit_code=data.get("exit_code"),
            error_type=data.get("error_type"),
        )


@dataclass(frozen=True)
class ExperienceFailure:
    """A failure encountered during execution before recovery/correction."""

    phase: str
    error_message: str
    attempted_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted_action": self.attempted_action,
            "error_message": self.error_message,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceFailure:
        return cls(
            phase=str(data.get("phase", "")),
            error_message=str(data.get("error_message", "")),
            attempted_action=data.get("attempted_action"),
        )


@dataclass(frozen=True)
class ExperienceCorrection:
    """A corrective action taken to resolve a failure."""

    failure_ref: str
    correction_action: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "correction_action": self.correction_action,
            "failure_ref": self.failure_ref,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceCorrection:
        return cls(
            failure_ref=str(data.get("failure_ref", "")),
            correction_action=str(data.get("correction_action", "")),
            rationale=str(data.get("rationale", "")),
        )


@dataclass(frozen=True)
class ExperienceVerification:
    """Multi-dimensional verification evidence for an experience record."""

    dimensions: VerificationDimensions
    observed_diff: WorkspaceDiff
    contract_id: str | None = None
    declaration_matches: bool = True
    tests_passed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "declaration_matches": self.declaration_matches,
            "dimensions": self.dimensions.to_dict(),
            "observed_diff": self.observed_diff.to_dict(),
            "tests_passed": self.tests_passed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceVerification:
        dim_data = data.get("dimensions", {})
        diff_data = data.get("observed_diff", {})
        return cls(
            contract_id=data.get("contract_id"),
            declaration_matches=bool(data.get("declaration_matches", True)),
            tests_passed=bool(data.get("tests_passed", True)),
            dimensions=VerificationDimensions(
                scope_verified=bool(dim_data.get("scope_verified", False)),
                state_verified=bool(dim_data.get("state_verified", False)),
                code_verified=bool(dim_data.get("code_verified", False)),
                behavioral_verified=bool(dim_data.get("behavioral_verified", False)),
                security_verified=bool(dim_data.get("security_verified", False)),
                outcome_verified=bool(dim_data.get("outcome_verified", False)),
            ),
            observed_diff=WorkspaceDiff(
                added=tuple(diff_data.get("added", ())),
                modified=tuple(diff_data.get("modified", ())),
                deleted=tuple(diff_data.get("deleted", ())),
            ),
        )


@dataclass(frozen=True)
class ExperienceRecord:
    """Canonical immutable artifact representing a verified agent execution."""

    experience_id: str
    task_id: str
    agent_id: str
    task_signature: str
    environment: dict[str, Any]
    verification: ExperienceVerification
    trust_level: TrustLevel
    learning_eligible: bool
    outcome: str
    recorded_at: str
    work_order_id: str | None = None
    initial_state: dict[str, Any] = field(default_factory=dict)
    actions: tuple[ExperienceAction, ...] = field(default_factory=tuple)
    observations: tuple[ExperienceObservation, ...] = field(default_factory=tuple)
    failures: tuple[ExperienceFailure, ...] = field(default_factory=tuple)
    corrections: tuple[ExperienceCorrection, ...] = field(default_factory=tuple)
    final_state: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    schema_version: int = 1

    @classmethod
    def mint_id(cls, task_signature: str, timestamp_str: str) -> str:
        """Mint a deterministic NodeID for an experience record using the registry scheme."""
        return node_id_for("experience", f"{task_signature}:{timestamp_str}")

    def validate_schema(self, schema_path: Path | None = None) -> None:
        """Validate this record against the JSON Schema."""
        if schema_path is None:
            schema_path = Path(__file__).resolve().parent.parent.parent / "schemas" / "experience.schema.json"
        if not schema_path.exists():
            raise FileNotFoundError(f"Experience schema not found at {schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft7Validator(schema)
        errors = list(validator.iter_errors(self.to_dict()))
        if errors:
            msg = "; ".join(e.message for e in errors)
            raise ValueError(f"Experience schema validation failed: {msg}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "agent_id": self.agent_id,
            "corrections": [c.to_dict() for c in self.corrections],
            "duration_ms": self.duration_ms,
            "environment": self.environment,
            "experience_id": self.experience_id,
            "failures": [f.to_dict() for f in self.failures],
            "final_state": self.final_state,
            "initial_state": self.initial_state,
            "learning_eligible": self.learning_eligible,
            "observations": [o.to_dict() for o in self.observations],
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_signature": self.task_signature,
            "trust_level": self.trust_level.value if hasattr(self.trust_level, "value") else str(self.trust_level),
            "verification": self.verification.to_dict(),
            "work_order_id": self.work_order_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperienceRecord:
        raw_trust = data.get("trust_level", "OBSERVABLE")
        trust_level = TrustLevel(raw_trust) if raw_trust in TrustLevel._value2member_map_ else TrustLevel.OBSERVABLE
        return cls(
            experience_id=str(data["experience_id"]),
            task_id=str(data["task_id"]),
            agent_id=str(data["agent_id"]),
            task_signature=str(data["task_signature"]),
            environment=dict(data.get("environment", {})),
            verification=ExperienceVerification.from_dict(data.get("verification", {})),
            trust_level=trust_level,
            learning_eligible=bool(data.get("learning_eligible", False)),
            outcome=str(data.get("outcome", "completed")),
            recorded_at=str(data.get("recorded_at", datetime.now(timezone.utc).isoformat())),
            work_order_id=data.get("work_order_id"),
            initial_state=dict(data.get("initial_state", {})),
            actions=tuple(ExperienceAction.from_dict(a) for a in data.get("actions", ())),
            observations=tuple(ExperienceObservation.from_dict(o) for o in data.get("observations", ())),
            failures=tuple(ExperienceFailure.from_dict(f) for f in data.get("failures", ())),
            corrections=tuple(ExperienceCorrection.from_dict(c) for c in data.get("corrections", ())),
            final_state=dict(data.get("final_state", {})),
            duration_ms=int(data.get("duration_ms", 0)),
            schema_version=int(data.get("schema_version", 1)),
        )
