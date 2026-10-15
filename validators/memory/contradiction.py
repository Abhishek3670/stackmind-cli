"""Contradiction detection for StackMind Durable Memory Records.

Implements conservative, staged contradiction detection with deterministic checks
and optional semantic analysis. Findings are persisted and traceable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from validators.memory.models import MemoryRecord, MemoryStatus, MemoryType, MemoryProvenance, MemoryEvidence
from validators.knowledge.contract import AgentContract

if TYPE_CHECKING:
    from validators.memory.store import MemoryStore


class ContradictionCategory(str, Enum):
    """Categories of contradiction findings."""
    # Deterministic/structural
    INCOMPATIBLE_VALUES = "incompatible_values"           # Same subject, incompatible explicit values
    CONFLICTING_DECISIONS = "conflicting_decisions"       # Conflicting decisions for same scope
    INCOMPATIBLE_STATES = "incompatible_states"           # Incompatible explicit states for same work item
    INCOMPATIBLE_CONSTRAINTS = "incompatible_constraints" # Incompatible constraints on same target
    TEMPORAL_CONFLICT = "temporal_conflict"               # Explicit temporal/version metadata conflict
    SUPERSESSION_CHAIN_INVALID = "supersession_chain_invalid" # Invalid supersession chain

    # Conservative semantic (potential, not confirmed)
    POTENTIAL_SEMANTIC_CONFLICT = "potential_semantic_conflict"  # Potential conflict, needs review

    # Non-conflict outcomes
    LEGITIMATE_SUPERSESSION = "legitimate_supersession"   # Valid supersession/update relationship
    SCOPE_MISMATCH = "scope_mismatch"                     # Different projects/modules/entities
    NO_CONFLICT_DETECTED = "no_conflict_detected"         # No contradiction found


class ContradictionSeverity(str, Enum):
    """Severity levels for contradiction findings."""
    BLOCKING = "blocking"       # Must be resolved before promotion
    WARNING = "warning"         # Should be reviewed, may block per policy
    INFO = "info"               # Informational, no action required


class ContradictionStatus(str, Enum):
    """Resolution status of a contradiction finding."""
    UNRESOLVED = "unresolved"
    UNDER_REVIEW = "under_review"
    RESOLVED_NOT_CONTRADICTION = "resolved_not_contradiction"
    RESOLVED_SUPERSEDED = "resolved_superseded"
    RESOLVED_BOTH_VALID = "resolved_both_valid"
    RESOLVED_REJECTED = "resolved_rejected"


@dataclass(frozen=True)
class ContradictionFinding:
    """A persisted contradiction finding between memory records."""

    finding_id: str
    category: ContradictionCategory
    severity: ContradictionSeverity
    status: ContradictionStatus

    # Involved records
    memory_id_a: str
    memory_id_b: str
    project_id: str

    # Detection details
    detection_method: str  # "deterministic", "semantic", "supersession_chain"
    confidence: float | None = None  # For semantic: 0.0-1.0, for deterministic: None
    explanation: str = ""

    # Evidence references
    evidence_refs_a: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs_b: tuple[str, ...] = field(default_factory=tuple)

    # Metadata
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str | None = None
    resolution_rationale: str | None = None
    resolution_action: str | None = None  # e.g., "supersede", "keep_both", "reject_candidate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "memory_id_a": self.memory_id_a,
            "memory_id_b": self.memory_id_b,
            "project_id": self.project_id,
            "detection_method": self.detection_method,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "evidence_refs_a": list(self.evidence_refs_a),
            "evidence_refs_b": list(self.evidence_refs_b),
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "resolution_rationale": self.resolution_rationale,
            "resolution_action": self.resolution_action,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContradictionFinding:
        return cls(
            finding_id=str(data["finding_id"]),
            category=ContradictionCategory(data["category"]),
            severity=ContradictionSeverity(data["severity"]),
            status=ContradictionStatus(data["status"]),
            memory_id_a=str(data["memory_id_a"]),
            memory_id_b=str(data["memory_id_b"]),
            project_id=str(data["project_id"]),
            detection_method=str(data["detection_method"]),
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            explanation=str(data.get("explanation", "")),
            evidence_refs_a=tuple(data.get("evidence_refs_a", ())),
            evidence_refs_b=tuple(data.get("evidence_refs_b", ())),
            detected_at=str(data.get("detected_at", datetime.now(timezone.utc).isoformat())),
            resolved_at=data.get("resolved_at"),
            resolution_rationale=data.get("resolution_rationale"),
            resolution_action=data.get("resolution_action"),
        )


class ContradictionStore:
    """Filesystem storage for contradiction findings."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.findings_dir = self.project_path / ".sync" / "memory" / "contradictions"

    def ensure_directories(self) -> None:
        self.findings_dir.mkdir(parents=True, exist_ok=True)

    def _finding_file(self, finding_id: str) -> Path:
        return self.findings_dir / f"{finding_id}.json"

    def save_finding(self, finding: ContradictionFinding) -> Path:
        self.ensure_directories()
        target_file = self._finding_file(finding.finding_id)
        tmp_file = target_file.with_suffix(".tmp")

        payload = json.dumps(finding.to_dict(), indent=2, sort_keys=True)
        tmp_file.write_text(payload, encoding="utf-8")
        os.replace(tmp_file, target_file)
        return target_file

    def load_finding(self, finding_id: str) -> ContradictionFinding | None:
        target_file = self._finding_file(finding_id)
        if not target_file.exists():
            return None
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            return ContradictionFinding.from_dict(data)
        except Exception:
            return None

    def list_findings(
        self,
        *,
        project_id: str | None = None,
        status: ContradictionStatus | None = None,
        category: ContradictionCategory | None = None,
        memory_id: str | None = None,
        limit: int | None = None,
    ) -> list[ContradictionFinding]:
        if not self.findings_dir.exists():
            return []

        findings: list[ContradictionFinding] = []
        for path in self.findings_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                finding = ContradictionFinding.from_dict(data)

                if project_id and finding.project_id != project_id:
                    continue
                if status and finding.status != status:
                    continue
                if category and finding.category != category:
                    continue
                if memory_id and finding.memory_id_a != memory_id and finding.memory_id_b != memory_id:
                    continue

                findings.append(finding)
            except Exception:
                continue

        findings.sort(key=lambda f: f.detected_at, reverse=True)
        if limit:
            return findings[:limit]
        return findings

    def find_by_memory_pair(self, memory_id_a: str, memory_id_b: str) -> ContradictionFinding | None:
        """Find existing finding between two memories (order-independent)."""
        for finding in self.list_findings():
            if (finding.memory_id_a == memory_id_a and finding.memory_id_b == memory_id_b) or \
               (finding.memory_id_a == memory_id_b and finding.memory_id_b == memory_id_a):
                return finding
        return None


class ContradictionDetector:
    """Conservative contradiction detection with staged approach."""

    def __init__(
        self,
        project_path: Path | str,
        contract: AgentContract | None = None,
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.contract = contract
        self._memory_store = memory_store
        self.findings_store = ContradictionStore(self.project_path)

    @property
    def memory_store(self) -> "MemoryStore":
        if self._memory_store is None:
            from validators.memory.store import MemoryStore
            self._memory_store = MemoryStore(self.project_path)
        return self._memory_store

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

    def _is_eligible_for_check(self, record: MemoryRecord) -> bool:
        """Check if record is eligible for contradiction detection."""
        # Only active, non-expired, non-deleted, non-superseded records
        if record.status not in {MemoryStatus.ACTIVE}:
            return False
        if record.expires_at is not None:
            try:
                expires = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= expires:
                    return False
            except Exception:
                pass
        return True

    def _extract_subject_key(self, record: MemoryRecord) -> tuple[str, str] | None:
        """Extract a subject key for comparison.

        Returns (subject_type, subject_value) or None if not extractable.
        Uses memory type and content patterns to identify comparable entities.
        """
        normalized = record.normalized_content.lower()
        mem_type = record.memory_type

        # Work order pattern - match WO-XXX in various contexts
        wo_match = re.search(r'(?:work order|wo-|for\s+)(wo-\s*[A-Z0-9\-]+)', normalized)
        if wo_match:
            wo = wo_match.group(1).replace(" ", "").upper()
            return ("work_order", wo)

        # Direct WO- pattern without prefix
        wo_direct = re.search(r'\b(wo-[A-Z0-9\-]+)\b', normalized)
        if wo_direct:
            return ("work_order", wo_direct.group(1).upper())

        # File path pattern
        file_match = re.search(r'(?:file|path|module|changes to)\s*([a-zA-Z0-9_\-./\\]+)', normalized)
        if file_match:
            return ("file", file_match.group(1).strip())

        # Test/module pattern
        test_match = re.search(r'(?:test|module|package)\s*([a-zA-Z0-9_\-]+)', normalized)
        if test_match:
            return ("module", test_match.group(1).strip())

        # For decisions, use work_order_id from project_id or content
        if mem_type == MemoryType.DECISION:
            if record.project_id and record.project_id.startswith("WO-"):
                return ("work_order", record.project_id.upper())
            # Also check content for WO-
            wo_in_content = re.search(r'\b(wo-[A-Z0-9\-]+)\b', normalized)
            if wo_in_content:
                return ("work_order", wo_in_content.group(1).upper())

        # For facts, try to extract the subject
        if mem_type == MemoryType.FACT:
            # Look for "X passed", "X verified", "changes to X"
            patterns = [
                r'(?:tests?|build|checks?)\s+(?:passed|failed|verified|pass|fail)\s+(?:for|on|in)\s+([a-zA-Z0-9_\-]+)',
                r'verified\s+(?:code\s+)?changes\s+(?:to\s+)?([a-zA-Z0-9_\-./]+)',
            ]
            for pat in patterns:
                m = re.search(pat, normalized)
                if m:
                    return ("fact_subject", m.group(1).strip())

        # Fallback: use first significant words as key
        words = normalized.split()
        significant = [w for w in words if len(w) > 3 and w not in {'work', 'order', 'tests', 'passed', 'verified', 'changes', 'approved', 'implement', 'module', 'user', 'auth'}]
        if significant:
            return ("content_hash", hashlib.md5(" ".join(significant[:5]).encode()).hexdigest()[:12])

        return None

    def _extract_decision_context(self, record: MemoryRecord) -> dict[str, str] | None:
        """Extract decision context for comparison."""
        if record.memory_type != MemoryType.DECISION:
            return None

        normalized = record.normalized_content.lower()
        context = {}

        # Extract work order - match WO-XXX in various contexts
        wo_match = re.search(r'(?:work order|wo-|for\s+)(wo-\s*[A-Z0-9\-]+)', normalized)
        if wo_match:
            wo = wo_match.group(1).replace(" ", "").upper()
            context["work_order"] = wo
        else:
            # Direct WO- pattern without prefix
            wo_direct = re.search(r'\b(wo-[A-Z0-9\-]+)\b', normalized)
            if wo_direct:
                context["work_order"] = wo_direct.group(1).upper()

        # Also check project_id
        if not context.get("work_order") and record.project_id and record.project_id.startswith("WO-"):
            context["work_order"] = record.project_id.upper()

        # Extract decision type
        if "approv" in normalized:
            context["decision_type"] = "approval"
        elif "reject" in normalized:
            context["decision_type"] = "rejection"
        elif "defer" in normalized:
            context["decision_type"] = "deferral"

        # Extract key entities
        entities = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)*\b', record.content)
        if entities:
            context["entities"] = ",".join(sorted(set(entities)))

        return context if context else None

    def _check_incompatible_values(self, record_a: MemoryRecord, record_b: MemoryRecord) -> ContradictionFinding | None:
        """Check for incompatible explicit values for the same subject."""
        # Only compare same type and project
        if record_a.memory_type != record_b.memory_type:
            return None
        if record_a.project_id != record_b.project_id:
            return None

        # Only check FACT type for now (explicit values)
        if record_a.memory_type != MemoryType.FACT:
            return None

        subject_a = self._extract_subject_key(record_a)
        subject_b = self._extract_subject_key(record_b)

        if not subject_a or not subject_b or subject_a != subject_b:
            return None

        # Check for explicit incompatible outcomes
        content_a = record_a.normalized_content.lower()
        content_b = record_b.normalized_content.lower()

        # Pass/Fail opposites
        a_pass = "passed" in content_a or "pass" in content_a
        a_fail = "failed" in content_a or "fail" in content_a
        b_pass = "passed" in content_b or "pass" in content_b
        b_fail = "failed" in content_b or "fail" in content_b

        if (a_pass and b_fail) or (a_fail and b_pass):
            return ContradictionFinding(
                finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                category=ContradictionCategory.INCOMPATIBLE_VALUES,
                severity=ContradictionSeverity.BLOCKING,
                status=ContradictionStatus.UNRESOLVED,
                memory_id_a=record_a.memory_id,
                memory_id_b=record_b.memory_id,
                project_id=record_a.project_id,
                detection_method="deterministic",
                confidence=None,
                explanation=f"Conflicting outcomes for {subject_a[1]}: one indicates pass, other indicates fail",
                evidence_refs_a=tuple(e.experience_id for e in record_a.evidence),
                evidence_refs_b=tuple(e.experience_id for e in record_b.evidence),
            )

        # Verified/Not verified opposites
        a_verified = "verified" in content_a and "not" not in content_a
        a_not_verified = "not verified" in content_a or "unverified" in content_a
        b_verified = "verified" in content_b and "not" not in content_b
        b_not_verified = "not verified" in content_b or "unverified" in content_b

        if (a_verified and b_not_verified) or (a_not_verified and b_verified):
            return ContradictionFinding(
                finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                category=ContradictionCategory.INCOMPATIBLE_VALUES,
                severity=ContradictionSeverity.BLOCKING,
                status=ContradictionStatus.UNRESOLVED,
                memory_id_a=record_a.memory_id,
                memory_id_b=record_b.memory_id,
                project_id=record_a.project_id,
                detection_method="deterministic",
                confidence=None,
                explanation=f"Conflicting verification status for {subject_a[1]}",
                evidence_refs_a=tuple(e.experience_id for e in record_a.evidence),
                evidence_refs_b=tuple(e.experience_id for e in record_b.evidence),
            )

        return None

    def _check_conflicting_decisions(self, record_a: MemoryRecord, record_b: MemoryRecord) -> ContradictionFinding | None:
        """Check for conflicting decisions for the same scope."""
        if record_a.memory_type != MemoryType.DECISION or record_b.memory_type != MemoryType.DECISION:
            return None
        if record_a.project_id != record_b.project_id:
            return None

        ctx_a = self._extract_decision_context(record_a)
        ctx_b = self._extract_decision_context(record_b)

        if not ctx_a or not ctx_b:
            return None

        # Same work order but potentially different outcomes
        if ctx_a.get("work_order") == ctx_b.get("work_order") and ctx_a.get("work_order") is not None:

            # Check for approval vs rejection
            content_a = record_a.normalized_content.lower()
            content_b = record_b.normalized_content.lower()

            a_approves = "approv" in content_a
            a_rejects = "reject" in content_a
            b_approves = "approv" in content_b
            b_rejects = "reject" in content_b

            if (a_approves and b_rejects) or (a_rejects and b_approves):
                return ContradictionFinding(
                    finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                    category=ContradictionCategory.CONFLICTING_DECISIONS,
                    severity=ContradictionSeverity.BLOCKING,
                    status=ContradictionStatus.UNRESOLVED,
                    memory_id_a=record_a.memory_id,
                    memory_id_b=record_b.memory_id,
                    project_id=record_a.project_id,
                    detection_method="deterministic",
                    confidence=None,
                    explanation=f"Conflicting decisions for work order {ctx_a.get('work_order')}: approval vs rejection",
                    evidence_refs_a=tuple(e.experience_id for e in record_a.evidence),
                    evidence_refs_b=tuple(e.experience_id for e in record_b.evidence),
                )

        return None

    def _check_supersession_chain(self, record_a: MemoryRecord, record_b: MemoryRecord) -> ContradictionFinding | None:
        """Check for invalid or inconsistent supersession chains."""
        # Check if both claim to supersede each other
        if record_a.memory_id in record_b.supersedes and record_b.memory_id in record_a.supersedes:
            return ContradictionFinding(
                finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                category=ContradictionCategory.SUPERSESSION_CHAIN_INVALID,
                severity=ContradictionSeverity.BLOCKING,
                status=ContradictionStatus.UNRESOLVED,
                memory_id_a=record_a.memory_id,
                memory_id_b=record_b.memory_id,
                project_id=record_a.project_id,
                detection_method="supersession_chain",
                confidence=None,
                explanation="Mutual supersession: each record claims to supersede the other",
                evidence_refs_a=tuple(),
                evidence_refs_b=tuple(),
            )

        # Check if A supersedes B but B is still ACTIVE and not superseded by A
        if record_a.memory_id in record_b.supersedes:
            if record_a.status == MemoryStatus.ACTIVE and record_b.status == MemoryStatus.ACTIVE:
                if record_b.superseded_by != record_a.memory_id:
                    return ContradictionFinding(
                        finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                        category=ContradictionCategory.SUPERSESSION_CHAIN_INVALID,
                        severity=ContradictionSeverity.WARNING,
                        status=ContradictionStatus.UNRESOLVED,
                        memory_id_a=record_a.memory_id,
                        memory_id_b=record_b.memory_id,
                        project_id=record_a.project_id,
                        detection_method="supersession_chain",
                        confidence=None,
                        explanation=f"Record {record_a.memory_id} supersedes {record_b.memory_id} but superseded_by link is missing",
                        evidence_refs_a=tuple(),
                        evidence_refs_b=tuple(),
                    )

        return None

    def _check_legitimate_supersession(self, record_a: MemoryRecord, record_b: MemoryRecord) -> ContradictionFinding | None:
        """Check if one record legitimately supersedes the other (not a conflict)."""
        # Case 1: A supersedes B with clear temporal ordering (both existing records)
        if record_a.memory_id in record_b.supersedes:
            if record_a.status == MemoryStatus.ACTIVE and record_b.status == MemoryStatus.SUPERSEDED:
                if record_b.superseded_by == record_a.memory_id:
                    return ContradictionFinding(
                        finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                        category=ContradictionCategory.LEGITIMATE_SUPERSESSION,
                        severity=ContradictionSeverity.INFO,
                        status=ContradictionStatus.RESOLVED_SUPERSEDED,
                        memory_id_a=record_a.memory_id,
                        memory_id_b=record_b.memory_id,
                        project_id=record_a.project_id,
                        detection_method="deterministic",
                        confidence=None,
                        explanation=f"Legitimate supersession: {record_a.memory_id} supersedes {record_b.memory_id}",
                        evidence_refs_a=tuple(),
                        evidence_refs_b=tuple(),
                        resolved_at=datetime.now(timezone.utc).isoformat(),
                        resolution_rationale="Valid supersession chain with reciprocal links",
                        resolution_action="supersede",
                    )

        if record_b.memory_id in record_a.supersedes:
            if record_b.status == MemoryStatus.ACTIVE and record_a.status == MemoryStatus.SUPERSEDED:
                if record_a.superseded_by == record_b.memory_id:
                    return ContradictionFinding(
                        finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                        category=ContradictionCategory.LEGITIMATE_SUPERSESSION,
                        severity=ContradictionSeverity.INFO,
                        status=ContradictionStatus.RESOLVED_SUPERSEDED,
                        memory_id_a=record_a.memory_id,
                        memory_id_b=record_b.memory_id,
                        project_id=record_a.project_id,
                        detection_method="deterministic",
                        confidence=None,
                        explanation=f"Legitimate supersession: {record_b.memory_id} supersedes {record_a.memory_id}",
                        evidence_refs_a=tuple(),
                        evidence_refs_b=tuple(),
                        resolved_at=datetime.now(timezone.utc).isoformat(),
                        resolution_rationale="Valid supersession chain with reciprocal links",
                        resolution_action="supersede",
                    )

        # Case 2: Candidate supersedes a SUPERSEDED memory, and there's already
        # an ACTIVE memory that also supersedes that same SUPERSEDED memory.
        # This means the candidate is repeating an already-validated supersession.
        # We check both directions (a is candidate, b is active OR a is active, b is candidate)
        if record_a.status == MemoryStatus.CANDIDATE and record_b.status == MemoryStatus.ACTIVE:
            # Check if candidate's supersedes target is also superseded by active
            for sup_id in record_a.supersedes:
                if sup_id in record_b.supersedes:
                    # Both supersede the same memory
                    # Check if that target is actually SUPERSEDED
                    # (we'd need to load it, but for now assume if both point to it, it's valid)
                    return ContradictionFinding(
                        finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                        category=ContradictionCategory.LEGITIMATE_SUPERSESSION,
                        severity=ContradictionSeverity.INFO,
                        status=ContradictionStatus.RESOLVED_SUPERSEDED,
                        memory_id_a=record_a.memory_id,
                        memory_id_b=record_b.memory_id,
                        project_id=record_a.project_id,
                        detection_method="deterministic",
                        confidence=None,
                        explanation=f"Legitimate supersession: candidate {record_a.memory_id} and active {record_b.memory_id} both supersede same target",
                        evidence_refs_a=tuple(),
                        evidence_refs_b=tuple(),
                        resolved_at=datetime.now(timezone.utc).isoformat(),
                        resolution_rationale="Both memories supersede the same target - validated supersession chain",
                        resolution_action="supersede",
                    )

        if record_b.status == MemoryStatus.CANDIDATE and record_a.status == MemoryStatus.ACTIVE:
            # Check if candidate's supersedes target is also superseded by active
            for sup_id in record_b.supersedes:
                if sup_id in record_a.supersedes:
                    return ContradictionFinding(
                        finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                        category=ContradictionCategory.LEGITIMATE_SUPERSESSION,
                        severity=ContradictionSeverity.INFO,
                        status=ContradictionStatus.RESOLVED_SUPERSEDED,
                        memory_id_a=record_a.memory_id,
                        memory_id_b=record_b.memory_id,
                        project_id=record_a.project_id,
                        detection_method="deterministic",
                        confidence=None,
                        explanation=f"Legitimate supersession: candidate {record_b.memory_id} and active {record_a.memory_id} both supersede same target",
                        evidence_refs_a=tuple(),
                        evidence_refs_b=tuple(),
                        resolved_at=datetime.now(timezone.utc).isoformat(),
                        resolution_rationale="Both memories supersede the same target - validated supersession chain",
                        resolution_action="supersede",
                    )

        return None

    def _check_scope_mismatch(self, record_a: MemoryRecord, record_b: MemoryRecord) -> ContradictionFinding | None:
        """Check if records are in different scopes and shouldn't be compared."""
        if record_a.project_id != record_b.project_id:
            return ContradictionFinding(
                finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                category=ContradictionCategory.SCOPE_MISMATCH,
                severity=ContradictionSeverity.INFO,
                status=ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
                memory_id_a=record_a.memory_id,
                memory_id_b=record_b.memory_id,
                project_id=record_a.project_id,
                detection_method="deterministic",
                confidence=None,
                explanation=f"Different projects: {record_a.project_id} vs {record_b.project_id}",
                evidence_refs_a=tuple(),
                evidence_refs_b=tuple(),
                resolved_at=datetime.now(timezone.utc).isoformat(),
                resolution_rationale="Cross-project comparison not applicable",
                resolution_action="scope_isolation",
            )

        # Check for different entities/modules within same project
        subject_a = self._extract_subject_key(record_a)
        subject_b = self._extract_subject_key(record_b)

        if subject_a and subject_b and subject_a[0] == subject_b[0] and subject_a[1] != subject_b[1]:
            return ContradictionFinding(
                finding_id=self._make_finding_id(record_a.memory_id, record_b.memory_id),
                category=ContradictionCategory.SCOPE_MISMATCH,
                severity=ContradictionSeverity.INFO,
                status=ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
                memory_id_a=record_a.memory_id,
                memory_id_b=record_b.memory_id,
                project_id=record_a.project_id,
                detection_method="deterministic",
                confidence=None,
                explanation=f"Different {subject_a[0]}s: {subject_a[1]} vs {subject_b[1]}",
                evidence_refs_a=tuple(),
                evidence_refs_b=tuple(),
                resolved_at=datetime.now(timezone.utc).isoformat(),
                resolution_rationale="Different entities within same project",
                resolution_action="scope_isolation",
            )

        return None

    def _make_finding_id(self, memory_id_a: str, memory_id_b: str) -> str:
        """Create a stable finding ID from the two memory IDs."""
        # Order-independent ID
        ids = sorted([memory_id_a, memory_id_b])
        combined = f"{ids[0]}:{ids[1]}"
        return f"CONTR-{hashlib.sha256(combined.encode()).hexdigest()[:16]}"

    def detect_contradictions(
        self,
        candidate: MemoryRecord,
        *,
        existing_records: list[MemoryRecord] | None = None,
    ) -> list[ContradictionFinding]:
        """Detect contradictions between candidate and existing eligible memories."""
        if not self._check_contract_scope(candidate):
            return []

        # Get existing records if not provided
        if existing_records is None:
            existing_records = self.memory_store.list_records(
                project_id=candidate.project_id,
                status=MemoryStatus.ACTIVE,
                include_expired=False,
                include_deleted=False,
            )

        # Filter eligible records (only ACTIVE, non-expired)
        eligible_records = [r for r in existing_records if self._is_eligible_for_check(r)]

        findings: list[ContradictionFinding] = []

        for existing in eligible_records:
            # Skip if same memory
            if existing.memory_id == candidate.memory_id:
                continue

            # Check for existing finding
            existing_finding = self.findings_store.find_by_memory_pair(candidate.memory_id, existing.memory_id)
            if existing_finding:
                # Reuse existing finding if still relevant
                if existing_finding.status in {ContradictionStatus.UNRESOLVED, ContradictionStatus.UNDER_REVIEW}:
                    findings.append(existing_finding)
                continue

            # Run deterministic checks in order
            checks = [
                self._check_legitimate_supersession,  # Check this first
                self._check_scope_mismatch,
                self._check_supersession_chain,
                self._check_incompatible_values,
                self._check_conflicting_decisions,
            ]

            for check in checks:
                finding = check(candidate, existing)
                if finding:
                    findings.append(finding)
                    break  # First match wins for this pair

        # Persist new findings
        for finding in findings:
            if finding.status == ContradictionStatus.UNRESOLVED or \
               finding.category in {ContradictionCategory.LEGITIMATE_SUPERSESSION, ContradictionCategory.SCOPE_MISMATCH}:
                self.findings_store.save_finding(finding)

        return findings

    def get_unresolved_blocking(self, project_id: str | None = None) -> list[ContradictionFinding]:
        """Get all unresolved blocking findings."""
        findings = self.findings_store.list_findings(
            project_id=project_id,
            status=ContradictionStatus.UNRESOLVED,
        )
        return [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]


class ContradictionResolutionService:
    """Service for resolving contradiction findings."""

    def __init__(
        self,
        project_path: Path | str,
        contract: AgentContract | None = None,
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.contract = contract
        self._memory_store = memory_store
        self.findings_store = ContradictionStore(self.project_path)

    @property
    def memory_store(self) -> "MemoryStore":
        if self._memory_store is None:
            from validators.memory.store import MemoryStore
            self._memory_store = MemoryStore(self.project_path)
        return self._memory_store

    def _check_contract_scope(self, record: MemoryRecord) -> bool:
        if not self.contract:
            return True
        if not self.contract.allow_rules and not self.contract.deny_rules:
            return True
        project = record.project_id
        for deny_rule in self.contract.deny_rules:
            pattern = deny_rule.get("module") or deny_rule.get("project") or deny_rule.get("target")
            if pattern and (pattern in {"*", "**"} or pattern == project):
                return False
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

    def resolve_finding(
        self,
        finding_id: str,
        resolution: ContradictionStatus,
        rationale: str,
        actor: str,
        resolution_action: str | None = None,
    ) -> ContradictionFinding | None:
        """Resolve a contradiction finding."""
        finding = self.findings_store.load_finding(finding_id)
        if not finding:
            return None

        if finding.status in {ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
                              ContradictionStatus.RESOLVED_SUPERSEDED,
                              ContradictionStatus.RESOLVED_BOTH_VALID,
                              ContradictionStatus.RESOLVED_REJECTED}:
            # Already resolved - idempotent
            return finding

        # Validate authorization
        record_a = self.memory_store.load_record(finding.memory_id_a)
        record_b = self.memory_store.load_record(finding.memory_id_b)

        if record_a and not self._check_contract_scope(record_a):
            raise ValueError(f"Cannot resolve: {finding.memory_id_a} outside contract scope")
        if record_b and not self._check_contract_scope(record_b):
            raise ValueError(f"Cannot resolve: {finding.memory_id_b} outside contract scope")

        # Validate resolution is allowed
        valid_resolutions = {
            ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
            ContradictionStatus.RESOLVED_SUPERSEDED,
            ContradictionStatus.RESOLVED_BOTH_VALID,
            ContradictionStatus.RESOLVED_REJECTED,
        }
        if resolution not in valid_resolutions:
            raise ValueError(f"Invalid resolution status: {resolution.value}")

        # Update finding
        resolved_finding = ContradictionFinding(
            finding_id=finding.finding_id,
            category=finding.category,
            severity=finding.severity,
            status=resolution,
            memory_id_a=finding.memory_id_a,
            memory_id_b=finding.memory_id_b,
            project_id=finding.project_id,
            detection_method=finding.detection_method,
            confidence=finding.confidence,
            explanation=finding.explanation,
            evidence_refs_a=finding.evidence_refs_a,
            evidence_refs_b=finding.evidence_refs_b,
            detected_at=finding.detected_at,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            resolution_rationale=rationale,
            resolution_action=resolution_action or resolution.value,
        )

        self.findings_store.save_finding(resolved_finding)

        # Audit log
        self._audit_resolution(resolved_finding, actor)

        return resolved_finding

    def supersede_and_resolve(
        self,
        finding_id: str,
        superseding_memory: MemoryRecord,
        rationale: str,
        actor: str,
    ) -> tuple[MemoryRecord | None, ContradictionFinding | None]:
        """Resolve a contradiction by superseding one memory with another.

        Returns the superseded (old) memory and the resolved finding.
        """
        finding = self.findings_store.load_finding(finding_id)
        if not finding:
            return None, None

        # Determine which memory to supersede
        # Typically supersede the older one
        record_a = self.memory_store.load_record(finding.memory_id_a)
        record_b = self.memory_store.load_record(finding.memory_id_b)

        if not record_a or not record_b:
            return None, None

        # Superseding must be authorized
        if not self._check_contract_scope(superseding_memory):
            raise ValueError("Superseding memory outside contract scope")

        # Use compiler to supersede
        from validators.memory.store import MemoryCompiler
        compiler = MemoryCompiler(self.project_path)

        # Determine which to supersede (usually the one with earlier created_at)
        if record_a.created_at < record_b.created_at:
            old_memory = record_a
        else:
            old_memory = record_b

        old_memory_id = old_memory.memory_id

        try:
            # supersede_memory returns the new memory, but we want the old one
            _ = compiler.supersede_memory(old_memory_id, superseding_memory)
            if _:
                # Load the superseded old memory to return it
                superseded_old = self.memory_store.load_record(old_memory_id)
                # Resolve finding
                resolved = self.resolve_finding(
                    finding_id,
                    ContradictionStatus.RESOLVED_SUPERSEDED,
                    rationale,
                    actor,
                    "supersede",
                )
                return superseded_old, resolved
        except Exception as e:
            raise ValueError(f"Supersession failed: {e}")

        return None, None

    def _audit_resolution(self, finding: ContradictionFinding, actor: str) -> None:
        try:
            audit_dir = self.project_path / ".sync" / "memory" / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)

            audit_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "contradiction_resolved",
                "finding_id": finding.finding_id,
                "category": finding.category.value,
                "severity": finding.severity.value,
                "old_status": "unresolved",
                "new_status": finding.status.value,
                "memory_id_a": finding.memory_id_a,
                "memory_id_b": finding.memory_id_b,
                "actor": actor,
                "rationale": finding.resolution_rationale,
                "action": finding.resolution_action,
            }

            audit_file = audit_dir / f"contradiction_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit_entry) + "\n")
        except Exception:
            pass