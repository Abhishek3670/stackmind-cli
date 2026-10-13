"""Requirement tracking and decompos validation harness module."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .requirements import Requirement, RequirementsLedger


@dataclass
class RequirementOwnership:
    """Tracks requirement to work order mappings."""
    requirement_id: str
    work_order_id: str
    milestone_id: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "work_order_id": self.work_order_id,
            "milestone_id": self.milestone_id,
            "created_at": self.created_at,
        }


@dataclass
class RequirementDecompositionValidator:
    """Validates work order references to requirements."""
    ledger: RequirementsLedger
    known_milestones: set[str] = field(default_factory=set)

    def validate_wo_requires_requirement(
        self,
        wo_id: str,
        requirement_id: str | None,
    ) -> tuple[bool, list[str]]:
        """
        Validate a work order's requirement reference.
        Returns (valid, list of validation errors).
        """
        errors: list[str] = []

        if not requirement_id:
            return True, []

        # Check requirement exists
        req = self.ledger.get_requirement(requirement_id)
        if not req:
            errors.append(f"Work order {wo_id} references unknown requirement {requirement_id}")
            return False, errors

        # Check milestone reference if present
        if hasattr(req, 'milestone_id') and req.milestone_id:
            if req.milestone_id not in self.known_milestones:
                errors.append(
                    f"Requirement {requirement_id} references unknown milestone {req.milestone_id}"
                )

        return len(errors) == 0, errors

    def validate_requirement_decomposition(
        self,
        requirement_id: str,
        sub_requirement_ids: list[str],
    ) -> tuple[bool, list[str]]:
        """
        Validate that sub-requirements belong to the parent requirement.
        Returns (valid, list of validation errors).
        """
        errors: list[str] = []

        parent_req = self.ledger.get_requirement(requirement_id)
        if not parent_req:
            errors.append(f"Parent requirement {requirement_id} not found")
            return False, errors

        for sub_id in sub_requirement_ids:
            sub_req = self.ledger.get_requirement(sub_id)
            if not sub_req:
                errors.append(f"Sub-requirement {sub_id} not found")
                continue

            # Check sub-requirement is properly linked
            if not sub_req.work_order_ids and not hasattr(sub_req, 'blocker_ids'):
                errors.append(
                    f"Sub-requirement {sub_id} has no work order or blocker assigned"
                )

        return len(errors) == 0, errors


def compile_requirements_from_milestones(
    ledger_path: Path,
    milestones: dict[str, dict[str, Any]],
) -> RequirementsLedger:
    """
    Compile requirements from PLAN milestone data.

    Args:
        ledger_path: Path to requirements ledger file
        milestones: Dictionary mapping milestone IDs to milestone data

    Returns:
        RequirementsLedger with compiled requirements
    """
    import re

    ledger: RequirementsLedger | None = None

    # Try to load existing ledger
    try:
        ledger = RequirementsLedger.load(ledger_path)
    except Exception:
        ledger = RequirementsLedger()

    # Process each milestone
    for milestone_id, m_data in milestones.items():
        text = m_data.get("text", m_data.get("title", ""))
        acceptance = m_data.get("acceptance_criteria", [])

        req = Requirement(
            id=f"R{len(ledger.requirements) + 1:03d}",
            text=text,
            type="FUNCTIONAL",
            priority=m_data.get("priority", "MEDIUM"),
            acceptance_criteria=acceptance,
            source_location=f"PLAN.md: {milestone_id}",
            work_order_ids=[],
        )

        # Add milestone_id if present in m_data
        if "milestone_id" in m_data:
            req.milestone_id = m_data["milestone_id"]

        ledger.add_requirement(req)

    return ledger


def validate_requirement_coverage(
    ledger: RequirementsLedger,
    work_order_ids: list[str],
) -> tuple[bool, list[str]]:
    """
    Validate that all requirements have owners or blockers.

    Args:
        ledger: Requirements ledger to validate
        work_order_ids: List of known work order IDs

    Returns:
        (passed, list of blocking issues)
    """
    issues: list[str] = []

    for req in ledger.requirements:
        # Check for requirements without owners or blockers
        if not req.work_order_ids:
            if not hasattr(req, 'blocker_ids') or not req.blocker_ids:
                issues.append(
                    f"Requirement {req.id} ({req.text[:50]}...) has no owner or blocker"
                )
            else:
                # Check blocker is valid
                blocker = req.blocker_ids[0] if req.blocker_ids else None
                if blocker and blocker not in work_order_ids:
                    issues.append(
                        f"Requirement {req.id} blocker {blocker} not in known work orders"
                    )

        # Check requirement status consistency
        if req.status == "PASS" and not req.evidence:
            issues.append(
                f"Requirement {req.id} is PASS but has no verification evidence"
            )

    return len(issues) == 0, issues
