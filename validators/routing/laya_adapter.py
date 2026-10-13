"""Optional Laya Local Decision Engine Adapter.

Integrates NandhaKishorM/laya (https://github.com/NandhaKishorM/laya) as an optional,
strictly isolated local System-1 decision engine for structured classification
and skill-routing prioritization.

Architectural invariants:
- Zero hard dependency: Never crashes or fails startup if `laya` is not installed.
- Zero auto-downloads: Never initiates background checkpoint downloads or network calls.
- Deterministic fallback: Always provides instant deterministic heuristics when
  Laya is unavailable, disabled, unsupported, or uncertain.
- Safety & governance: Never authorizes tool calls, bypasses contracts, or overrides
  security/verification gates.
- Shadow mode: Allows evaluating Laya against production heuristics before activating.
- Disabled by default.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("stackmind.routing.laya")


class LayaMode(str, Enum):
    """Operational mode for the Laya local decision engine."""

    DISABLED = "disabled"  # Default: completely bypassed
    SHADOW = "shadow"      # Runs in background for comparison; production uses fallback
    ACTIVE = "active"      # Used for candidate prioritization when confidence exceeds threshold


class TaskCategory(str, Enum):
    """Standardized task categories for work order classification and skill routing."""

    TESTING = "testing"
    BACKEND = "backend"
    FRONTEND = "frontend"
    ARCHITECTURE = "architecture"
    GITOPS = "gitops"
    GENERAL = "general"


@dataclass(frozen=True)
class LayaDecisionResult:
    """Outcome of a structured category decision with confidence and provenance."""

    category: TaskCategory
    confidence: float
    source: str  # "laya", "fallback", "shadow", "fallback_low_confidence"
    latency_ms: float = 0.0
    shadow_comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "confidence": round(self.confidence, 3),
            "latency_ms": round(self.latency_ms, 2),
            "shadow_comparison": self.shadow_comparison,
            "source": self.source,
        }


class LayaDecisionAdapter:
    """Non-intrusive adapter for local Laya decision engine."""

    def __init__(
        self,
        mode: LayaMode | str = LayaMode.DISABLED,
        checkpoint_path: str | Path | None = None,
        min_confidence: float = 0.70,
    ) -> None:
        if isinstance(mode, LayaMode):
            self.mode = mode
        else:
            try:
                self.mode = LayaMode(str(mode).lower().strip())
            except ValueError:
                self.mode = LayaMode.DISABLED
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.min_confidence = min_confidence
        self._router_instance: Any | None = None
        self._checked_availability: bool = False
        self._is_available: bool = False

    def is_available(self) -> bool:
        """Check if Laya is installed and configured locally without downloading checkpoints."""
        if self._checked_availability:
            return self._is_available
        self._checked_availability = True

        if self.mode == LayaMode.DISABLED:
            self._is_available = False
            return False

        try:
            import laya  # type: ignore[import-untyped]

            # If checkpoint path was specified, it must exist locally
            if self.checkpoint_path is not None and not self.checkpoint_path.exists():
                self._is_available = False
                return False

            self._is_available = True
            return True
        except (ImportError, Exception):
            self._is_available = False
            return False

    def _get_router(self) -> Any | None:
        """Lazily initialize router instance if available without triggering downloads."""
        if not self.is_available():
            return None
        if self._router_instance is not None:
            return self._router_instance

        try:
            from laya import Router  # type: ignore[import-untyped]

            # Instantiate with explicit local checkpoint if provided
            if self.checkpoint_path:
                self._router_instance = Router(checkpoint=str(self.checkpoint_path))
            else:
                self._router_instance = Router()
            return self._router_instance
        except Exception as exc:
            logger.debug("Laya router initialization failed: %s", exc)
            self._is_available = False
            return None

    @classmethod
    def deterministic_classify(cls, title: str, body: str = "", work_order_id: str | None = None) -> TaskCategory:
        """Deterministic keyword and structure classification fallback."""
        text = f"{title} {body}".lower()

        # 1. Testing & repair
        if any(kw in text for kw in ("test", "pytest", "unit test", "integration test", "qa", "assert", "coverage")):
            return TaskCategory.TESTING

        # 2. Architecture & planning
        if any(kw in text for kw in ("plan.md", "architecture", "work order", "decompose", "milestone", "blueprint")):
            return TaskCategory.ARCHITECTURE

        # 3. GitOps & release
        if any(kw in text for kw in ("gitops", "release", "version", "changelog", "commit", "semantic version")):
            return TaskCategory.GITOPS

        # 4. Frontend
        if any(kw in text for kw in ("frontend", "ui", "html", "css", "styling", "markup", "view", "component", "dom")):
            return TaskCategory.FRONTEND

        # 5. Backend
        if any(kw in text for kw in ("backend", "api", "database", "endpoint", "route", "service", "model", "schema")):
            return TaskCategory.BACKEND

        return TaskCategory.GENERAL

    def classify_task(
        self,
        title: str,
        body: str = "",
        work_order_id: str | None = None,
    ) -> LayaDecisionResult:
        """Classify task domain with deterministic fallback and optional shadow comparison."""
        t_start = time.monotonic()
        fallback_cat = self.deterministic_classify(title, body, work_order_id)

        # If Laya is disabled or unavailable, return deterministic fallback immediately
        if self.mode == LayaMode.DISABLED or not self.is_available():
            latency = (time.monotonic() - t_start) * 1000
            return LayaDecisionResult(
                category=fallback_cat,
                confidence=0.85,
                source="fallback",
                latency_ms=latency,
            )

        router = self._get_router()
        if router is None:
            latency = (time.monotonic() - t_start) * 1000
            return LayaDecisionResult(
                category=fallback_cat,
                confidence=0.85,
                source="fallback",
                latency_ms=latency,
            )

        # Structured Laya decision schema
        document = {
            "title": title[:200],
            "body": body[:600],
            "work_order_id": work_order_id or "",
        }
        questions = {
            "category": {
                "type": "choice",
                "instructions": "Which development domain owns this engineering task?",
                "criteria": {
                    "testing": "running test suites, writing unit/integration tests, fixing test failures",
                    "backend": "server APIs, database schema, data models, backend services",
                    "frontend": "UI components, client styles, CSS, HTML markup, layout",
                    "architecture": "planning, work orders, system specifications, contracts",
                    "gitops": "git commits, versioning, release changelog, packaging",
                },
            }
        }

        try:
            res = router.predict(document, questions)
            latency = (time.monotonic() - t_start) * 1000
            answer = res.get("answers", {}).get("category", {})
            choice_str = str(answer.get("choice") or "").strip().lower()
            confidence = float(answer.get("score") or answer.get("confidence") or 0.8)

            # Map predicted choice to TaskCategory
            predicted_cat = TaskCategory.GENERAL
            for cat in TaskCategory:
                if cat.value == choice_str:
                    predicted_cat = cat
                    break

            shadow_comp = {
                "laya_prediction": predicted_cat.value,
                "deterministic_fallback": fallback_cat.value,
                "agreement": predicted_cat == fallback_cat,
                "confidence": confidence,
            }

            if self.mode == LayaMode.SHADOW:
                # Shadow mode returns deterministic fallback, recording comparison
                return LayaDecisionResult(
                    category=fallback_cat,
                    confidence=0.85,
                    source="shadow",
                    latency_ms=latency,
                    shadow_comparison=shadow_comp,
                )

            # Active mode: apply confidence threshold
            if confidence >= self.min_confidence and predicted_cat != TaskCategory.GENERAL:
                return LayaDecisionResult(
                    category=predicted_cat,
                    confidence=confidence,
                    source="laya",
                    latency_ms=latency,
                    shadow_comparison=shadow_comp,
                )

            return LayaDecisionResult(
                category=fallback_cat,
                confidence=confidence,
                source="fallback_low_confidence",
                latency_ms=latency,
                shadow_comparison=shadow_comp,
            )

        except Exception as exc:
            logger.debug("Laya decision evaluation failed: %s", exc)
            latency = (time.monotonic() - t_start) * 1000
            return LayaDecisionResult(
                category=fallback_cat,
                confidence=0.85,
                source="fallback_error",
                latency_ms=latency,
            )

    def route_skill_categories(self, query: str) -> list[str]:
        """Map a query into prioritized candidate skill categories."""
        decision = self.classify_task(query)
        cat = decision.category.value
        categories = [cat] if cat != "general" else []
        q_lower = query.lower()
        if "test" in q_lower or "qa" in q_lower or "coverage" in q_lower:
            if TaskCategory.TESTING.value not in categories:
                categories.append(TaskCategory.TESTING.value)
        if "backend" in q_lower or "api" in q_lower or "endpoint" in q_lower:
            if TaskCategory.BACKEND.value not in categories:
                categories.append(TaskCategory.BACKEND.value)
        if "frontend" in q_lower or "ui" in q_lower or "css" in q_lower:
            if TaskCategory.FRONTEND.value not in categories:
                categories.append(TaskCategory.FRONTEND.value)
        if "architecture" in q_lower or "plan" in q_lower or "spec" in q_lower:
            if TaskCategory.ARCHITECTURE.value not in categories:
                categories.append(TaskCategory.ARCHITECTURE.value)
        if "gitops" in q_lower or "release" in q_lower or "version" in q_lower:
            if TaskCategory.GITOPS.value not in categories:
                categories.append(TaskCategory.GITOPS.value)
        return categories or [TaskCategory.GENERAL.value]

    def shadow_evaluate(self, query: str, current_choice: str) -> dict[str, Any]:
        """Record shadow evaluation comparing Laya prediction against current baseline."""
        res = self.classify_task(query)
        return {
            "mode": "shadow",
            "query": query,
            "fallback_choice": res.category.value,
            "current_choice": current_choice,
            "match": res.category.value == current_choice.lower(),
            "confidence": res.confidence,
            "timestamp": time.time(),
        }


def classify_task_category(
    title: str,
    body: str = "",
    work_order_id: str | None = None,
    adapter: LayaDecisionAdapter | None = None,
) -> LayaDecisionResult:
    """Convenience helper to classify a task domain."""
    effective_adapter = adapter or LayaDecisionAdapter(mode=LayaMode.DISABLED)
    return effective_adapter.classify_task(title, body, work_order_id)
