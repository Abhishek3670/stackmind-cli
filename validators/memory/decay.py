"""Memory Decay: configurable, measurable decay as a retrieval/prioritization signal only.

Implements safe memory decay with:
- Multiple decay functions (exponential, linear, stepwise)
- Configurable half-life per memory type and execution profile
- Shadow-mode evaluation for A/B testing decay effects
- Full audit diagnostics and no mutation of memory records
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from validators.kernel.profile import ExecutionProfile, ProfilePolicy, get_profile_policy
from validators.memory.models import MemoryRecord, MemoryType


class DecayFunction(str, Enum):
    """Available decay functions for memory relevance attenuation."""

    EXPONENTIAL = "exponential"  # exp(-lambda * age) - smooth continuous decay
    LINEAR = "linear"  # 1 - age / half_life - linear to zero at half_life
    STEPWISE = "stepwise"  # Discrete steps at half-life intervals
    NONE = "none"  # No decay applied


@dataclass(frozen=True)
class DecayPolicy:
    """Configuration for memory decay behavior.

    Decay is applied as a multiplicative factor to baseline relevance scores.
    The decay factor ranges from 1.0 (fresh) to decay_floor (minimum).
    """

    function: DecayFunction = DecayFunction.EXPONENTIAL
    half_life_days: float = 30.0  # Days until relevance halves (for exponential)
    decay_floor: float = 0.2  # Minimum decay multiplier (0.0-1.0)
    enabled: bool = True

    # Per-memory-type half-life overrides (days)
    half_life_by_type: dict[MemoryType, float] = field(default_factory=dict)

    # Per-profile half-life multipliers
    profile_multipliers: dict[ExecutionProfile, float] = field(default_factory=dict)

    @classmethod
    def default(cls) -> DecayPolicy:
        """Default decay policy with sensible per-type and per-profile settings."""
        return cls(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.2,
            enabled=True,
            half_life_by_type={
                MemoryType.FACT: 60.0,  # Facts decay slower
                MemoryType.DECISION: 90.0,  # Decisions very stable
                MemoryType.LESSON: 21.0,  # Lessons decay faster
                MemoryType.PREFERENCE: 120.0,  # Preferences very stable
                MemoryType.PATTERN: 45.0,  # Patterns moderate
            },
            profile_multipliers={
                ExecutionProfile.LOW: 0.0,  # No decay effect in low profile (memories disabled)
                ExecutionProfile.MEDIUM: 1.0,  # Standard decay
                ExecutionProfile.HIGH: 0.5,  # Reduced decay effect in high profile
            },
        )

    @classmethod
    def no_decay(cls) -> DecayPolicy:
        """Policy with decay completely disabled."""
        return cls(
            function=DecayFunction.NONE,
            half_life_days=0.0,
            decay_floor=1.0,
            enabled=False,
        )

    def get_effective_half_life(self, memory_type: MemoryType, profile: ExecutionProfile) -> float:
        """Get effective half-life for a memory type and profile combination."""
        base = self.half_life_by_type.get(memory_type, self.half_life_days)
        multiplier = self.profile_multipliers.get(profile, 1.0)
        return base * multiplier

    def to_dict(self) -> dict[str, Any]:
        return {
            "function": self.function.value,
            "half_life_days": self.half_life_days,
            "decay_floor": self.decay_floor,
            "enabled": self.enabled,
            "half_life_by_type": {k.value if hasattr(k, 'value') else k: v for k, v in self.half_life_by_type.items()},
            "profile_multipliers": {k.value if hasattr(k, 'value') else k: v for k, v in self.profile_multipliers.items()},
        }


@dataclass(frozen=True)
class DecayApplication:
    """Result of applying decay to a single memory."""

    memory_id: str
    memory_type: MemoryType
    age_days: float
    base_relevance: float
    decay_factor: float
    decayed_relevance: float
    half_life_used: float
    function_used: DecayFunction

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.value,
            "age_days": round(self.age_days, 2),
            "base_relevance": round(self.base_relevance, 4),
            "decay_factor": round(self.decay_factor, 4),
            "decayed_relevance": round(self.decayed_relevance, 4),
            "half_life_used": round(self.half_life_used, 2),
            "function_used": self.function_used.value,
        }


@dataclass(frozen=True)
class DecayDiagnostics:
    """Audit diagnostics for decay application across a retrieval set."""

    policy: dict[str, Any]
    profile: str
    considered_count: int
    decay_applied_count: int
    decay_skipped_count: int
    applications: tuple[DecayApplication, ...] = ()
    shadow_mode: bool = False
    shadow_comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "policy": self.policy,
            "profile": self.profile,
            "considered_count": self.considered_count,
            "decay_applied_count": self.decay_applied_count,
            "decay_skipped_count": self.decay_skipped_count,
            "applications": [a.to_dict() for a in self.applications],
            "shadow_mode": self.shadow_mode,
        }
        if self.shadow_comparison:
            result["shadow_comparison"] = self.shadow_comparison
        return result


class DecayAdjuster:
    """Applies configured memory decay as a multiplicative relevance factor.

    Decay is a retrieval-time signal only - it never mutates stored memory records.
    All decay applications are fully auditable via DecayDiagnostics.
    """

    def __init__(self, project_path: Path | str, policy: DecayPolicy | None = None) -> None:
        self.project_path = Path(project_path).resolve()
        self.policy = policy or DecayPolicy.default()
        self._shadow_mode = False
        self._audit_dir = self.project_path / ".sync" / "memory" / "audit"
        self._audit_dir.mkdir(parents=True, exist_ok=True)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    def enable_shadow_mode(self) -> None:
        """Enable shadow-mode: compute decay but don't apply, log comparison."""
        self._shadow_mode = True

    def disable_shadow_mode(self) -> None:
        """Disable shadow-mode: apply decay normally."""
        self._shadow_mode = False

    def _parse_datetime(self, dt_str: str | None) -> datetime | None:
        """Parse ISO datetime string to timezone-aware datetime."""
        if not dt_str:
            return None
        try:
            # Handle both with and without timezone
            if dt_str.endswith("Z"):
                dt_str = dt_str.replace("Z", "+00:00")
            return datetime.fromisoformat(dt_str)
        except Exception:
            return None

    def _calculate_age_days(self, memory: MemoryRecord) -> float:
        """Calculate age of memory in days from created_at."""
        created = self._parse_datetime(memory.created_at)
        if not created:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - created
        return delta.total_seconds() / 86400.0

    def _compute_decay_factor(
        self,
        age_days: float,
        half_life: float,
        function: DecayFunction,
        decay_floor: float,
    ) -> float:
        """Compute decay multiplier based on age, half-life, and function."""
        if half_life <= 0 or function == DecayFunction.NONE:
            return 1.0

        if function == DecayFunction.EXPONENTIAL:
            # Exponential decay: exp(-ln(2) * age / half_life)
            # At age = half_life, factor = 0.5
            lambda_val = math.log(2) / half_life
            factor = math.exp(-lambda_val * age_days)
        elif function == DecayFunction.LINEAR:
            # Linear decay: 1 - age / half_life, clamped at decay_floor
            factor = max(0.0, 1.0 - age_days / half_life)
        elif function == DecayFunction.STEPWISE:
            # Stepwise: 0.5^n where n = floor(age / half_life)
            steps = int(age_days / half_life)
            factor = 0.5 ** steps
        else:
            factor = 1.0

        return max(decay_floor, min(1.0, factor))

    def apply_decay(
        self,
        memories: Sequence[MemoryRecord],
        base_relevances: Sequence[float],
        profile: ExecutionProfile,
    ) -> tuple[list[float], DecayDiagnostics]:
        """Apply decay to base relevance scores, returning decayed scores and diagnostics.

        Args:
            memories: Memory records to apply decay to
            base_relevances: Baseline relevance scores (same order as memories)
            profile: Current execution profile for policy lookup

        Returns:
            Tuple of (decayed_relevances_list, DecayDiagnostics)
        """
        if not self.policy.enabled:
            # Decay disabled - return base scores unchanged
            diag = DecayDiagnostics(
                policy=self.policy.to_dict(),
                profile=profile.value,
                considered_count=len(memories),
                decay_applied_count=0,
                decay_skipped_count=len(memories),
                applications=(),
                shadow_mode=self._shadow_mode,
            )
            return list(base_relevances), diag

        if len(memories) != len(base_relevances):
            raise ValueError("memories and base_relevances must have same length")

        decayed_relevances: list[float] = []
        applications: list[DecayApplication] = []
        applied = 0
        skipped = 0

        for memory, base_score in zip(memories, base_relevances):
            half_life = self.policy.get_effective_half_life(memory.memory_type, profile)
            age_days = self._calculate_age_days(memory)
            decay_factor = self._compute_decay_factor(
                age_days, half_life, self.policy.function, self.policy.decay_floor
            )
            decayed_score = round(base_score * decay_factor, 4)

            app = DecayApplication(
                memory_id=memory.memory_id,
                memory_type=memory.memory_type,
                age_days=age_days,
                base_relevance=base_score,
                decay_factor=decay_factor,
                decayed_relevance=decayed_score,
                half_life_used=half_life,
                function_used=self.policy.function,
            )
            applications.append(app)
            decayed_relevances.append(decayed_score)

            if decay_factor < 1.0 - 1e-6:
                applied += 1
            else:
                skipped += 1

        diag = DecayDiagnostics(
            policy=self.policy.to_dict(),
            profile=profile.value,
            considered_count=len(memories),
            decay_applied_count=applied,
            decay_skipped_count=skipped,
            applications=tuple(applications),
            shadow_mode=self._shadow_mode,
        )

        if self._shadow_mode:
            # Shadow mode: log comparison but return original scores
            diag = DecayDiagnostics(
                policy=diag.policy,
                profile=diag.profile,
                considered_count=diag.considered_count,
                decay_applied_count=diag.decay_applied_count,
                decay_skipped_count=diag.decay_skipped_count,
                applications=diag.applications,
                shadow_mode=True,
                shadow_comparison={
                    "original_scores": [round(s, 4) for s in base_relevances],
                    "decayed_scores": decayed_relevances,
                    "max_impact": round(max(base_relevances[i] - decayed_relevances[i] for i in range(len(base_relevances))), 4)
                    if base_relevances
                    else 0.0,
                    "avg_impact": round(
                        sum(base_relevances[i] - decayed_relevances[i] for i in range(len(base_relevances))) / len(base_relevances), 4
                    )
                    if base_relevances
                    else 0.0,
                },
            )
            # Return ORIGINAL scores in shadow mode
            return list(base_relevances), diag

        return decayed_relevances, diag

    def evaluate_shadow(
        self,
        memories: Sequence[MemoryRecord],
        base_relevances: Sequence[float],
        profile: ExecutionProfile,
    ) -> DecayDiagnostics:
        """Run shadow evaluation without applying decay.

        Returns diagnostics with shadow_comparison showing what would happen.
        """
        was_shadow = self._shadow_mode
        self._shadow_mode = True
        try:
            _, diag = self.apply_decay(memories, base_relevances, profile)
            return diag
        finally:
            self._shadow_mode = was_shadow

    def log_audit(self, diagnostics: DecayDiagnostics, query: str = "") -> None:
        """Append decay diagnostics to daily audit log."""
        audit_file = self._audit_dir / f"decay_{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query[:200] if query else "",
            "diagnostics": diagnostics.to_dict(),
        }
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def get_decay_policy(profile: ExecutionProfile | str | None = None) -> DecayPolicy:
    """Get effective decay policy for a profile (can be extended for config file support)."""
    resolved = ExecutionProfile.from_str(profile) if profile else ExecutionProfile.MEDIUM
    policy = DecayPolicy.default()

    # Future: load from .sync/memory/config/decay.json if exists
    config_path = Path.cwd() / ".sync" / "memory" / "config" / "decay.json"
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                config = json.load(f)
            # Merge config overrides
            if "function" in config:
                policy = DecayPolicy(
                    function=DecayFunction(config["function"]),
                    half_life_days=config.get("half_life_days", policy.half_life_days),
                    decay_floor=config.get("decay_floor", policy.decay_floor),
                    enabled=config.get("enabled", policy.enabled),
                    half_life_by_type={
                        MemoryType(k): v for k, v in config.get("half_life_by_type", {}).items()
                    },
                    profile_multipliers={
                        ExecutionProfile(k): v for k, v in config.get("profile_multipliers", {}).items()
                    },
                )
        except Exception:
            pass  # Fall back to defaults on config error

    return policy