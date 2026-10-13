"""Central Execution Profile and Policy Definitions for StackMind.

Defines the three canonical profiles:
- Low: Fast execution, zero optional Skill context, mandatory security and verification preserved.
- Medium: Balanced trade-off between execution speed, relevant Skills, token consumption, and code quality.
- High: Deeper relevant Skill guidance, progressive loading, and risk-appropriate extended verification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class ExecutionProfile(str, Enum):
    """Canonical user-facing execution profile."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def from_str(cls, val: Any) -> ExecutionProfile:
        """Parse string to ExecutionProfile, defaulting to MEDIUM when None/empty."""
        if isinstance(val, cls):
            return val
        if val is None or not str(val).strip():
            return cls.MEDIUM
        normalized = str(val).strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(
            f"Invalid execution profile '{val}'. Valid profiles are: 'low', 'medium', 'high'."
        )

    @property
    def display_name(self) -> str:
        return self.value.capitalize()


class VerificationDepth(str, Enum):
    """Depth of delivery verification gates."""

    STANDARD = "standard"
    EXTENDED = "extended"


@dataclass(frozen=True)
class ProfilePolicy:
    """Policy parameters governing Skill retrieval, budgeting, and verification depth."""

    profile: ExecutionProfile
    skills_enabled: bool
    skill_limit: int
    skill_token_budget: int
    min_relevance: float
    progressive_loading: bool
    full_detail_threshold: float
    verification_depth: VerificationDepth = VerificationDepth.STANDARD
    allow_deep_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "skills_enabled": self.skills_enabled,
            "skill_limit": self.skill_limit,
            "skill_token_budget": self.skill_token_budget,
            "min_relevance": self.min_relevance,
            "progressive_loading": self.progressive_loading,
            "full_detail_threshold": self.full_detail_threshold,
            "verification_depth": self.verification_depth.value,
            "allow_deep_review": self.allow_deep_review,
        }


# Canonical centralized policy profiles
PROFILE_POLICIES: dict[ExecutionProfile, ProfilePolicy] = {
    ExecutionProfile.LOW: ProfilePolicy(
        profile=ExecutionProfile.LOW,
        skills_enabled=False,
        skill_limit=0,
        skill_token_budget=0,
        min_relevance=1.0,
        progressive_loading=False,
        full_detail_threshold=1.0,
        verification_depth=VerificationDepth.STANDARD,
        allow_deep_review=False,
    ),
    ExecutionProfile.MEDIUM: ProfilePolicy(
        profile=ExecutionProfile.MEDIUM,
        skills_enabled=True,
        skill_limit=2,
        skill_token_budget=600,
        min_relevance=0.25,
        progressive_loading=True,
        full_detail_threshold=0.5,
        verification_depth=VerificationDepth.STANDARD,
        allow_deep_review=False,
    ),
    ExecutionProfile.HIGH: ProfilePolicy(
        profile=ExecutionProfile.HIGH,
        skills_enabled=True,
        skill_limit=5,
        skill_token_budget=1500,
        min_relevance=0.15,
        progressive_loading=True,
        full_detail_threshold=0.15,
        verification_depth=VerificationDepth.EXTENDED,
        allow_deep_review=True,
    ),
}


def get_profile_policy(profile: ExecutionProfile | str | None) -> ProfilePolicy:
    """Resolve the canonical ProfilePolicy for a given profile."""
    resolved = ExecutionProfile.from_str(profile)
    return PROFILE_POLICIES[resolved]
