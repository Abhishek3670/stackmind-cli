"""Requirements Ledger - persistent traceability from user goals to verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml


class RequirementsError(ValueError):
    """Raised when requirements ledger operations fail."""
    pass


# Valid requirement statuses
REQUIREMENT_STATUS = (
    "PENDING",
    "IN_PROGRESS",
    "PASS",
    "FAIL",
    "BLOCKED",
    "ARCHIVED",
)


@dataclass
class Requirement:
    """A single requirement entry in the ledger."""
    id: str  # Stable ID like "R001"
    text: str  # Original requirement text
    type: str  # e.g., "FUNCTIONAL", "NON_FUNCTIONAL", "CONSTRAINT", "INTEGRATION"
    priority: str  # e.g., "HIGH", "MEDIUM", "LOW"
    acceptance_criteria: list[str] = field(default_factory=list)
    source_location: str | None = None  # Where in user goal/plan this came from
    work_order_ids: list[str] = field(default_factory=list)  # Owning WOs
    status: str = "PENDING"  # Current verification status
    verification_method: str | None = None  # How to verify (e.g., "TEST", "MANUAL", "STATIC_ANALYSIS")
    evidence: dict[str, Any] = field(default_factory=dict)  # Verification evidence
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "type": self.type,
            "priority": self.priority,
            "acceptance_criteria": self.acceptance_criteria,
            "source_location": self.source_location,
            "work_order_ids": self.work_order_ids,
            "status": self.status,
            "verification_method": self.verification_method,
            "evidence": self.evidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Requirement:
        return cls(
            id=data["id"],
            text=data["text"],
            type=data.get("type", "FUNCTIONAL"),
            priority=data.get("priority", "MEDIUM"),
            acceptance_criteria=data.get("acceptance_criteria", []),
            source_location=data.get("source_location"),
            work_order_ids=data.get("work_order_ids", []),
            status=data.get("status", "PENDING"),
            verification_method=data.get("verification_method"),
            evidence=data.get("evidence", {}),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class RequirementsLedger:
    """Persistent requirements ledger for a project."""
    schema_version: int = 1
    requirements: list[Requirement] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requirements": [r.to_dict() for r in self.requirements],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequirementsLedger:
        return cls(
            schema_version=data.get("schema_version", 1),
            requirements=[Requirement.from_dict(r) for r in data.get("requirements", [])],
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )

    @classmethod
    def load(cls, path: Path) -> "RequirementsLedger":
        """Load ledger from file. Raises RequirementsError if missing/malformed."""
        if not path.exists():
            raise RequirementsError(f"Requirements ledger not found: {path}")

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise RequirementsError("Requirements ledger must be a YAML mapping")
            return cls.from_dict(data)
        except yaml.YAMLError as e:
            raise RequirementsError(f"Malformed requirements ledger: {e}")

    def save(self, path: Path) -> None:
        """Save ledger to file."""
        self.updated_at = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)

    def get_requirement(self, req_id: str) -> Requirement | None:
        """Get a requirement by ID."""
        for r in self.requirements:
            if r.id == req_id:
                return r
        return None

    def add_requirement(self, req: Requirement) -> None:
        """Add a new requirement."""
        # Check for duplicate ID
        for r in self.requirements:
            if r.id == req.id:
                raise RequirementsError(f"Requirement ID {req.id} already exists")
        self.requirements.append(req)
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def update_requirement(self, req_id: str, updates: dict[str, Any]) -> Requirement:
        """Update an existing requirement. Returns the updated requirement."""
        req = self.get_requirement(req_id)
        if not req:
            raise RequirementsError(f"Requirement {req_id} not found")

        # Validate status if being updated
        if "status" in updates and updates["status"] not in REQUIREMENT_STATUS:
            raise RequirementsError(f"Invalid status: {updates['status']}")

        for key, value in updates.items():
            if hasattr(req, key):
                setattr(req, key, value)
        req.updated_at = datetime.now(timezone.utc).isoformat()
        return req

    def get_requirements_by_wo(self, wo_id: str) -> list[Requirement]:
        """Get all requirements owned by a work order."""
        return [r for r in self.requirements if wo_id in r.work_order_ids]

    def get_requirements_with_missing_wo(self) -> list[Requirement]:
        """Get requirements with no owning work order."""
        return [r for r in self.requirements if not r.work_order_ids]

    def get_requirements_by_status(self, status: str) -> list[Requirement]:
        """Get requirements with a specific status."""
        return [r for r in self.requirements if r.status == status]


def compile_requirements_from_plan(ledger_path: Path, plan_path: Path) -> RequirementsLedger:
    """
    Compile requirements from PLAN.md content.

    Extracts functional and non-functional requirements from plan milestones
    and their acceptance criteria.
    """
    if not plan_path.exists():
        raise RequirementsError(f"PLAN.md not found: {plan_path}")

    content = plan_path.read_text(encoding="utf-8")
    ledger: RequirementsLedger | None = None

    # Try to load existing ledger
    try:
        ledger = RequirementsLedger.load(ledger_path)
    except RequirementsError:
        # Create new ledger if none exists
        ledger = RequirementsLedger()

    # Simple heuristic: extract requirements from milestones
    # In a full implementation, this would use more sophisticated parsing
    import re
    requirement_id = len(ledger.requirements) + 1 if ledger.requirements else 1

    # Look for milestone titles and any "acceptance" or "requirements" text
    milestone_pattern = r"- \[ \] (Milestone \d+):\s*(.+?)(?:\n|$)"
    milestones = re.findall(milestone_pattern, content)

    for m_num, m_title in milestones:
        # Create requirement for each milestone
        req = Requirement(
            id=f"R{requirement_id:03d}",
            text=m_title.strip(),
            type="FUNCTIONAL",
            priority="MEDIUM",
            acceptance_criteria=[],  # Would need more sophisticated parsing
            source_location=f"PLAN.md: {m_num}",
            work_order_ids=[],  # Assigned later when WOs are created
        )
        ledger.add_requirement(req)
        requirement_id += 1

    return ledger


def validate_requirements_coverage(ledger: RequirementsLedger, work_order_ids: list[str]) -> tuple[bool, list[str]]:
    """
    Validate that all requirements have coverage.

    Returns (passed, list of blocking issues).
    """
    issues: list[str] = []

    # Check for requirements without owning work orders
    missing_wo = ledger.get_requirements_with_missing_wo()
    if missing_wo:
        for r in missing_wo:
            issues.append(f"Requirement {r.id} ({r.text[:50]}...) has no owning work order")

    # Check for requirements that are not PASS
    non_pass = [r for r in ledger.requirements if r.status not in ("PASS", "ARCHIVED")]
    for r in non_pass:
        if not r.work_order_ids:
            # Already reported above
            continue
        if r.status == "PENDING":
            issues.append(f"Requirement {r.id} is PENDING (work order {r.work_order_ids[0]} not complete)")
        elif r.status == "IN_PROGRESS":
            issues.append(f"Requirement {r.id} is IN_PROGRESS")
        elif r.status == "FAIL":
            issues.append(f"Requirement {r.id} has FAIL status")
        elif r.status == "BLOCKED":
            issues.append(f"Requirement {r.id} is BLOCKED")

    # Check for PASS without evidence
    for r in ledger.get_requirements_by_status("PASS"):
        if not r.evidence:
            issues.append(f"Requirement {r.id} is PASS but has no verification evidence")

    return len(issues) == 0, issues


__all__ = [
    "Requirement",
    "RequirementsLedger",
    "RequirementsError",
    "compile_requirements_from_plan",
    "validate_requirements_coverage",
    "REQUIREMENT_STATUS",
]
