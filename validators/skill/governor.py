"""Risk-tiered promotion governor for procedural skills.

Implements Phase 6 (Risk-Tiered Promotion).
Enforces that promotion autonomy scales inversely with consequence:
- LOW: Autonomous auto-promotion upon passing 3-stage verification pipeline.
- MEDIUM: Architect (Claude/CEO) review or pre-authorization.
- HIGH / CRITICAL: Mandatory human / CEO approval receipt required.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validators.skill.models import RiskTier, SkillRecord, SkillStatus


class RiskPromotionGateError(Exception):
    """Raised when a skill promotion violates risk-tiered governance policies."""
    pass


class HumanApprovalRequiredError(RiskPromotionGateError):
    """Raised when HIGH or CRITICAL risk skill requires explicit human/CEO signoff."""
    pass


@dataclass(frozen=True)
class ApprovalReceipt:
    """Audit receipt recording human or lead architect approval for a high-risk skill."""

    skill_id: str
    skill_name: str
    version: int
    risk_tier: RiskTier
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
            "risk_tier": self.risk_tier.value,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalReceipt:
        return cls(
            approval_id=data["approval_id"],
            approved_at=data["approved_at"],
            approver=data["approver"],
            rationale=data["rationale"],
            risk_tier=RiskTier(data["risk_tier"]),
            skill_id=data["skill_id"],
            skill_name=data["skill_name"],
            version=data["version"],
        )


class PromotionGovernor:
    """Governs risk-tiered promotion autonomy and human-in-the-loop review approvals."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.approvals_dir = self.project_path / ".sync" / "skills" / "approvals"

    def ensure_directories(self) -> None:
        self.approvals_dir.mkdir(parents=True, exist_ok=True)

    def _approval_file(self, skill_name: str, version: int) -> Path:
        return self.approvals_dir / f"{skill_name}_v{version}.approval.json"

    def get_approval(self, skill_name: str, version: int) -> ApprovalReceipt | None:
        """Fetch approval receipt for a specific skill version if present."""
        path = self._approval_file(skill_name, version)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ApprovalReceipt.from_dict(data)
        except Exception:
            return None

    def grant_approval(
        self,
        skill: SkillRecord,
        approver: str,
        rationale: str,
    ) -> ApprovalReceipt:
        """Grant and record an immutable approval receipt for a skill promotion."""
        self.ensure_directories()
        now_iso = datetime.now(timezone.utc).isoformat()
        import hashlib

        raw_id = f"APPROVAL:{skill.skill_id}:v{skill.version}:{approver}:{now_iso}".encode("utf-8")
        approval_id = f"APPR-{hashlib.sha256(raw_id).hexdigest()[:16]}"

        receipt = ApprovalReceipt(
            skill_id=skill.skill_id,
            skill_name=skill.name,
            version=skill.version,
            risk_tier=skill.risk_tier,
            approver=approver,
            rationale=rationale,
            approved_at=now_iso,
            approval_id=approval_id,
        )

        target_file = self._approval_file(skill.name, skill.version)
        tmp_file = target_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(receipt.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp_file, target_file)

        return receipt

    def check_promotion_eligibility(
        self,
        skill: SkillRecord,
        *,
        actor: str = "claude",
        is_human: bool = False,
        allow_medium_auto: bool = False,
    ) -> tuple[bool, str]:
        """Evaluate if skill can be promoted based on risk tier and approvals."""
        tier = skill.risk_tier

        # LOW risk tier: Auto-promotable once verified
        if tier == RiskTier.LOW:
            return True, "LOW risk tier: Eligible for autonomous promotion upon passing verification."

        # MEDIUM risk tier: Lead agent (Claude/CEO), human, or pre-authorized
        if tier == RiskTier.MEDIUM:
            if is_human or actor.lower() in {"ceo", "claude", "human"} or allow_medium_auto:
                return True, f"MEDIUM risk tier: Authorized by lead agent '{actor}' or human."
            # Check if explicit approval receipt exists
            approval = self.get_approval(skill.name, skill.version)
            if approval:
                return True, f"MEDIUM risk tier: Approved via receipt {approval.approval_id} by {approval.approver}."
            return (
                False,
                f"MEDIUM risk tier: Promotion by worker agent '{actor}' requires Claude/CEO authorization or approval receipt.",
            )

        # HIGH & CRITICAL risk tiers: Mandatory approval receipt or direct Human/CEO signoff
        if tier in {RiskTier.HIGH, RiskTier.CRITICAL}:
            if is_human or actor.lower() in {"ceo", "human"}:
                return True, f"{tier.value.upper()} risk tier: Direct Human/CEO signoff provided."

            approval = self.get_approval(skill.name, skill.version)
            if approval:
                return (
                    True,
                    f"{tier.value.upper()} risk tier: Authorized via approval receipt {approval.approval_id} by {approval.approver}.",
                )

            return (
                False,
                f"{tier.value.upper()} risk tier: Mandatory Human or CEO review required before promotion.",
            )

        return False, f"Unknown risk tier '{tier}'"

    def enforce_promotion_governance(
        self,
        skill: SkillRecord,
        *,
        actor: str = "claude",
        is_human: bool = False,
        allow_medium_auto: bool = False,
    ) -> None:
        """Enforce risk promotion gate; raises exception if unauthorized."""
        eligible, reason = self.check_promotion_eligibility(
            skill,
            actor=actor,
            is_human=is_human,
            allow_medium_auto=allow_medium_auto,
        )
        if not eligible:
            if skill.risk_tier in {RiskTier.HIGH, RiskTier.CRITICAL}:
                raise HumanApprovalRequiredError(reason)
            raise RiskPromotionGateError(reason)
