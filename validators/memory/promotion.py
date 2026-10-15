"""Promotion governance for StackMind Durable Memory Records.

Implements centralized, policy-driven promotion eligibility for CANDIDATE -> ACTIVE transitions.
Reuses existing governance patterns (PromotionGovernor) while maintaining memory-specific semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from validators.memory.models import MemoryRecord, MemoryStatus, MemoryType, MemoryProvenance, MemoryEvidence
from validators.experience.models import TrustLevel
from validators.knowledge.contract import AgentContract
from validators.memory.contradiction import (
    ContradictionDetector,
    ContradictionFinding,
    ContradictionCategory,
    ContradictionSeverity,
    ContradictionStatus,
)

if TYPE_CHECKING:
    from validators.memory.store import MemoryStore
    from validators.memory.retriever import MemoryRetriever


class PromotionDecision(str, Enum):
    """Structured promotion decision outcomes."""
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    REQUIRES_REVIEW = "requires_review"
    ALREADY_ACTIVE = "already_active"
    INVALID_RECORD = "invalid_record"
    CONTRADICTION_DETECTED = "contradiction_detected"


@dataclass(frozen=True)
class PromotionEligibilityResult:
    """Result of promotion eligibility evaluation."""

    decision: PromotionDecision
    reason_code: str
    message: str
    requires_approval: bool = False
    approval_type: str | None = None
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, 'metadata', {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "requires_approval": self.requires_approval,
            "approval_type": self.approval_type,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class MemoryApprovalReceipt:
    """Audit receipt for memory promotion approval."""

    memory_id: str
    memory_type: str
    approver: str
    rationale: str
    approved_at: str
    approval_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "approved_at": self.approved_at,
            "approver": self.approver,
            "rationale": self.rationale,
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryApprovalReceipt:
        return cls(
            approval_id=data["approval_id"],
            approved_at=data["approved_at"],
            approver=data["approver"],
            rationale=data["rationale"],
            memory_id=data["memory_id"],
            memory_type=data["memory_type"],
        )


class MemoryPromotionError(Exception):
    """Base exception for memory promotion policy violations."""
    pass


class ApprovalRequiredError(MemoryPromotionError):
    """Raised when a memory promotion requires explicit approval."""
    pass


class MemoryPromotionPolicy:
    """Centralized promotion eligibility policy for memory records.

    Evaluates memory records against integrity, lifecycle, provenance, evidence,
    trust, scope, and approval requirements before allowing CANDIDATE -> ACTIVE transition.
    """

    # Type-specific policy configuration
    TYPE_POLICIES: dict[MemoryType, dict[str, Any]] = {
        MemoryType.FACT: {
            "min_provenance_confidence": 0.7,
            "min_verification_bonus": 0.3,  # Requires at least one verified dimension
            "requires_evidence": True,
            "approval_required": False,
            "accepted_evidence_types": ["verification_dimension"],
        },
        MemoryType.DECISION: {
            "min_provenance_confidence": 0.8,
            "min_verification_bonus": 0.0,  # Decisions come from final_state, not verification
            "requires_evidence": True,
            "approval_required": False,
            "accepted_evidence_types": ["final_state"],
        },
        MemoryType.LESSON: {
            "min_provenance_confidence": 0.6,
            "min_verification_bonus": 0.1,
            "requires_evidence": True,
            "approval_required": False,
            "accepted_evidence_types": ["correction", "failure"],
        },
        MemoryType.PREFERENCE: {
            "min_provenance_confidence": 0.5,
            "min_verification_bonus": 0.0,
            "requires_evidence": False,
            "approval_required": False,
            "accepted_evidence_types": [],
        },
        MemoryType.PATTERN: {
            "min_provenance_confidence": 0.7,
            "min_verification_bonus": 0.2,
            "requires_evidence": True,
            "approval_required": False,
            "accepted_evidence_types": ["verification_dimension", "correction"],
        },
    }

    def __init__(
        self,
        project_path: Path | str,
        contract: AgentContract | None = None,
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.contract = contract
        self._memory_store = memory_store
        self.approvals_dir = self.project_path / ".sync" / "memory" / "approvals"

    @property
    def memory_store(self) -> "MemoryStore":
        """Lazy initialization of MemoryStore to avoid circular imports."""
        if self._memory_store is None:
            from validators.memory.store import MemoryStore
            self._memory_store = MemoryStore(self.project_path)
        return self._memory_store

    def ensure_directories(self) -> None:
        self.approvals_dir.mkdir(parents=True, exist_ok=True)

    def _approval_file(self, memory_id: str) -> Path:
        return self.approvals_dir / f"{memory_id}.approval.json"

    def get_approval(self, memory_id: str) -> MemoryApprovalReceipt | None:
        """Fetch approval receipt for a specific memory if present."""
        path = self._approval_file(memory_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return MemoryApprovalReceipt.from_dict(data)
        except Exception:
            return None

    def grant_approval(
        self,
        memory: MemoryRecord,
        approver: str,
        rationale: str,
    ) -> MemoryApprovalReceipt:
        """Grant and record an immutable approval receipt for a memory promotion."""
        self.ensure_directories()
        now_iso = datetime.now(timezone.utc).isoformat()

        raw_id = f"MEM-APPROVAL:{memory.memory_id}:{approver}:{now_iso}".encode("utf-8")
        approval_id = f"MAPP-{hashlib.sha256(raw_id).hexdigest()[:16]}"

        receipt = MemoryApprovalReceipt(
            memory_id=memory.memory_id,
            memory_type=memory.memory_type.value,
            approver=approver,
            rationale=rationale,
            approved_at=now_iso,
            approval_id=approval_id,
        )

        target_file = self._approval_file(memory.memory_id)
        tmp_file = target_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(receipt.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp_file, target_file)

        return receipt

    def _is_expired(self, record: MemoryRecord) -> bool:
        """Check if a memory record has expired based on expires_at timestamp."""
        if record.expires_at is None:
            return False
        try:
            expires = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return now >= expires
        except Exception:
            return False

    def _check_contract_scope(self, record: MemoryRecord) -> bool:
        """Verify the memory's project_id is within the contract's allowed scope."""
        if not self.contract:
            return True
        if not self.contract.allow_rules and not self.contract.deny_rules:
            return True

        project = record.project_id

        # Deny rules take precedence
        for deny_rule in self.contract.deny_rules:
            pattern = deny_rule.get("module") or deny_rule.get("project") or deny_rule.get("target")
            if pattern and (pattern in {"*", "**"} or pattern == project):
                return False

        # Allow rules - at least one must match
        if self.contract.allow_rules:
            allowed = False
            for allow_rule in self.contract.allow_rules:
                pattern = allow_rule.get("module") or allow_rule.get("project") or allow_rule.get("target")
                if pattern and (pattern in {"*", "**"} or pattern == project):
                    allowed = True
                    break
            if not allowed:
                return False

        return True

    def _has_sufficient_verification(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Check if the record's provenance has sufficient verification evidence."""
        policy = self.TYPE_POLICIES.get(record.memory_type, {})
        min_bonus = policy.get("min_verification_bonus", 0.0)
        accepted_types = policy.get("accepted_evidence_types", ["verification_dimension"])

        # Evidence from accepted types
        accepted_evidence = [e for e in record.evidence if e.evidence_type in accepted_types]
        if not accepted_evidence and min_bonus > 0:
            return False, [f"no_{accepted_types[0]}_evidence"]

        verified_dims = set()
        for ev in accepted_evidence:
            for dim, passed in ev.dimension_scores.items():
                if passed:
                    verified_dims.add(dim)

        if len(verified_dims) < 1 and min_bonus > 0:
            return False, ["insufficient_verified_dimensions"]

        return True, []

    def _check_evidence_requirements(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Verify required evidence exists and meets policy."""
        policy = self.TYPE_POLICIES.get(record.memory_type, {})
        requires_evidence = policy.get("requires_evidence", True)

        if not requires_evidence:
            return True, []

        if not record.evidence:
            return False, ["no_evidence"]

        # At least one evidence item with non-empty excerpt
        has_substantive_evidence = any(e.excerpt and len(e.excerpt.strip()) > 0 for e in record.evidence)
        if not has_substantive_evidence:
            return False, ["no_substantive_evidence"]

        return True, []

    def _check_provenance_confidence(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Check if provenance confidence meets minimum for memory type."""
        policy = self.TYPE_POLICIES.get(record.memory_type, {})
        min_confidence = policy.get("min_provenance_confidence", 0.5)

        if record.provenance.confidence < min_confidence:
            return False, [f"provenance_confidence_below_min_{min_confidence}"]

        return True, []

    def _check_source_trust(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Evaluate source trust level for the memory."""
        # Source trust is encoded in provenance confidence and evidence
        # For memories extracted from experiences, trust comes from the source experience
        if not record.provenance.source_experience_ids:
            return False, ["no_source_experience_ids"]

        # Check extraction method - compiler extractions are more trusted than manual
        if record.provenance.extraction_method not in {"compiler", "llm_distillation"}:
            # Manual extractions require higher confidence
            if record.provenance.confidence < 0.8:
                return False, ["untrusted_extraction_method"]

        return True, []

    def _check_duplicate_active(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Check if an equivalent active memory already exists."""
        # Use the same deduplication logic as compiler
        from validators.memory.store import normalize_content, make_uniqueness_key

        normalized = normalize_content(record.normalized_content)
        uniqueness_key = make_uniqueness_key(normalized, record.project_id)

        existing_records = self.memory_store.list_records(
            project_id=record.project_id,
            status=MemoryStatus.ACTIVE,
        )

        for existing in existing_records:
            existing_normalized = normalize_content(existing.normalized_content)
            existing_key = make_uniqueness_key(existing_normalized, existing.project_id)
            if existing_key == uniqueness_key and existing.memory_id != record.memory_id:
                return False, ["duplicate_active_memory", existing.memory_id]

        return True, []

    def _check_contradiction(self, record: MemoryRecord) -> tuple[bool, list[str]]:
        """Check for known contradictions with active memories using ContradictionDetector.

        Conservative approach: uses deterministic checks for incompatible values,
        conflicting decisions, invalid supersession chains, etc.
        """
        detector = ContradictionDetector(
            self.project_path,
            contract=self.contract,
            memory_store=self.memory_store,
        )

        findings = detector.detect_contradictions(record)

        # Filter for blocking findings
        blocking_findings = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING and f.status == ContradictionStatus.UNRESOLVED]

        if blocking_findings:
            # Return the first blocking finding
            finding = blocking_findings[0]
            return False, [f"contradiction_{finding.category.value}", finding.memory_id_b if finding.memory_id_a == record.memory_id else finding.memory_id_a]

        return True, []

    def evaluate_promotion(
        self,
        record: MemoryRecord,
        *,
        actor: str = "claude",
        is_human: bool = False,
        dry_run: bool = False,
    ) -> PromotionEligibilityResult:
        """Evaluate if a memory record is eligible for promotion to ACTIVE.

        Returns structured result with decision, reason codes, and metadata.
        """
        # 1. Record integrity
        if not record:
            return PromotionEligibilityResult(
                decision=PromotionDecision.INVALID_RECORD,
                reason_code="record_not_found",
                message="Memory record not found or invalid",
            )

        # 2. Schema validation
        try:
            schema_path = self.project_path / "schemas" / "memory.schema.json"
            if schema_path.exists():
                record.validate_schema(schema_path)
        except Exception as e:
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code="schema_validation_failed",
                message=f"Schema validation failed: {e}",
            )

        # 3. Lifecycle eligibility
        if record.status != MemoryStatus.CANDIDATE:
            if record.status == MemoryStatus.ACTIVE:
                return PromotionEligibilityResult(
                    decision=PromotionDecision.ALREADY_ACTIVE,
                    reason_code="already_active",
                    message=f"Memory {record.memory_id} is already ACTIVE",
                )
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code="invalid_lifecycle_state",
                message=f"Cannot promote memory in status: {record.status.value}. Only CANDIDATE memories can be promoted.",
            )

        # 4. Expiration check (re-checked at promotion time)
        if self._is_expired(record):
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code="expired",
                message="Memory has expired and cannot be promoted",
            )

        # 5. Contract scope enforcement
        if not self._check_contract_scope(record):
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code="contract_scope_denied",
                message="Memory project_id not authorized by contract scope",
            )

        # 6. Provenance confidence
        ok, reasons = self._check_provenance_confidence(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code=reasons[0],
                message=f"Provenance confidence insufficient for {record.memory_type.value}: {record.provenance.confidence:.2f}",
            )

        # 7. Source trust
        ok, reasons = self._check_source_trust(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED,
                reason_code=reasons[0],
                message="Source trust requirements not met",
            )

        # 8. Evidence requirements
        ok, reasons = self._check_evidence_requirements(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.REJECTED if "no_evidence" in reasons else PromotionDecision.REQUIRES_REVIEW,
                reason_code=reasons[0],
                message=f"Evidence requirement not met: {reasons[0]}",
            )

        # 9. Verification evidence
        ok, reasons = self._check_evidence_requirements(record)
        # Actually check verification dimensions specifically
        ok, reasons = self._has_sufficient_verification(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.REQUIRES_REVIEW,
                reason_code=reasons[0],
                message=f"Insufficient verification evidence: {reasons[0]}",
            )

        # 10. Duplicate active memory check
        ok, reasons = self._check_duplicate_active(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.ALREADY_ACTIVE,
                reason_code=reasons[0],
                message=f"Equivalent active memory already exists: {reasons[1] if len(reasons) > 1 else 'unknown'}",
                metadata={"duplicate_memory_id": reasons[1] if len(reasons) > 1 else None},
            )

        # 11. Contradiction check
        ok, reasons = self._check_contradiction(record)
        if not ok:
            return PromotionEligibilityResult(
                decision=PromotionDecision.CONTRADICTION_DETECTED,
                reason_code=reasons[0],
                message=f"Potential contradiction with existing active memory: {reasons[1] if len(reasons) > 1 else 'unknown'}",
                metadata={"contradiction_memory_id": reasons[1] if len(reasons) > 1 else None},
            )

        # 12. Approval check (type-specific)
        policy = self.TYPE_POLICIES.get(record.memory_type, {})
        if policy.get("approval_required", False):
            approval = self.get_approval(record.memory_id)
            if not approval and not is_human:
                return PromotionEligibilityResult(
                    decision=PromotionDecision.REQUIRES_REVIEW,
                    reason_code="approval_required",
                    message=f"Memory type {record.memory_type.value} requires approval before promotion",
                    requires_approval=True,
                    approval_type="human_or_lead",
                )

        # All checks passed
        return PromotionEligibilityResult(
            decision=PromotionDecision.ELIGIBLE,
            reason_code="eligible",
            message="Memory meets all promotion criteria",
            metadata={
                "provenance_confidence": record.provenance.confidence,
                "evidence_count": len(record.evidence),
                "memory_type": record.memory_type.value,
            },
        )

    def enforce_promotion(
        self,
        record: MemoryRecord,
        *,
        actor: str = "claude",
        is_human: bool = False,
        dry_run: bool = False,
    ) -> tuple[MemoryRecord | None, PromotionEligibilityResult]:
        """Enforce promotion policy; returns promoted record and result, or (None, result) if rejected.

        Raises ApprovalRequiredError if approval is required but not provided.
        """
        result = self.evaluate_promotion(record, actor=actor, is_human=is_human, dry_run=dry_run)

        if result.decision == PromotionDecision.REQUIRES_REVIEW and result.requires_approval:
            if not is_human:
                raise ApprovalRequiredError(result.message)

        if result.decision not in {PromotionDecision.ELIGIBLE, PromotionDecision.ALREADY_ACTIVE}:
            return None, result

        if result.decision == PromotionDecision.ALREADY_ACTIVE:
            # Return the existing active record
            if "duplicate_memory_id" in result.metadata:
                existing = self.memory_store.load_record(result.metadata["duplicate_memory_id"])
                return existing, result
            return record, result

        if dry_run:
            return None, result

        # Perform atomic promotion
        promoted = MemoryRecord(
            memory_id=record.memory_id,
            memory_type=record.memory_type,
            content=record.content,
            normalized_content=record.normalized_content,
            project_id=record.project_id,
            provenance=record.provenance,
            evidence=record.evidence,
            status=MemoryStatus.ACTIVE,
            created_at=record.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_validated_at=datetime.now(timezone.utc).isoformat(),
            expires_at=record.expires_at,
            superseded_by=record.superseded_by,
            supersedes=record.supersedes,
            tags=record.tags,
            schema_version=record.schema_version,
        )

        self.memory_store.save_record(promoted)
        return promoted, result


class MemoryPromotionService:
    """High-level service for promoting memories with audit logging.

    This is the authoritative entry point for all memory promotions.
    """

    def __init__(
        self,
        project_path: Path | str,
        contract: AgentContract | None = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.contract = contract
        self._memory_store: "MemoryStore | None" = None
        self.policy = MemoryPromotionPolicy(self.project_path, contract, memory_store=None)

    @property
    def memory_store(self) -> "MemoryStore":
        """Lazy initialization of MemoryStore to avoid circular imports."""
        if self._memory_store is None:
            from validators.memory.store import MemoryStore
            self._memory_store = MemoryStore(self.project_path)
        return self._memory_store

    def promote(
        self,
        memory_id: str,
        *,
        reason: str = "Validated by user",
        actor: str = "claude",
        is_human: bool = False,
        dry_run: bool = False,
    ) -> tuple[MemoryRecord | None, PromotionEligibilityResult]:
        """Promote a memory record to ACTIVE status through the governance gate."""
        record = self.memory_store.load_record(memory_id)
        if not record:
            result = PromotionEligibilityResult(
                decision=PromotionDecision.INVALID_RECORD,
                reason_code="record_not_found",
                message=f"Memory '{memory_id}' not found",
            )
            return None, result

        promoted, result = self.policy.enforce_promotion(
            record,
            actor=actor,
            is_human=is_human,
            dry_run=dry_run,
        )

        # Audit logging (non-blocking)
        self._audit_promotion(memory_id, record.status.value, promoted.status.value if promoted else record.status.value, result, actor, reason)

        return promoted, result

    def preview_promotion(self, memory_id: str) -> PromotionEligibilityResult:
        """Preview promotion eligibility without mutating state."""
        record = self.memory_store.load_record(memory_id)
        if not record:
            return PromotionEligibilityResult(
                decision=PromotionDecision.INVALID_RECORD,
                reason_code="record_not_found",
                message=f"Memory '{memory_id}' not found",
            )
        return self.policy.evaluate_promotion(record, dry_run=True)

    def _audit_promotion(
        self,
        memory_id: str,
        old_status: str,
        new_status: str,
        result: PromotionEligibilityResult,
        actor: str,
        reason: str,
    ) -> None:
        """Record promotion decision to audit log."""
        try:
            audit_dir = self.project_path / ".sync" / "memory" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)

            audit_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "memory_id": memory_id,
                "old_status": old_status,
                "new_status": new_status,
                "decision": result.decision.value,
                "reason_code": result.reason_code,
                "message": result.message,
                "actor": actor,
                "reason": reason,
                "policy_version": "1.0",
            }

            audit_file = audit_dir / f"promotion_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_entry) + "\n")
        except Exception:
            # Audit failures must not block promotion
            pass