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
from typing import Any, Mapping, Sequence

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

    def decide_skills_for_task(
        self,
        title: str,
        description: str = "",
        role: str = "backend",
        deliverable_path: str | None = None,
        available_skills: Sequence[Any] | None = None,
        explicit_skills: Sequence[str] | None = None,
        project_path: Path | str | None = None,
    ) -> list[str]:
        """Autonomously decide which procedural skills should be assigned to an engineering task.
        
        Args:
            title: Work order or milestone title.
            description: Work order description or context.
            role: Assigned agent role (backend, frontend, qa, architect, etc.).
            deliverable_path: Target deliverable path (e.g. 'src/app.py', 'index.html').
            available_skills: Optional sequence of SkillRecord instances. If None, loaded from store.
            explicit_skills: Optional sequence of explicit skill names declared by Architect/Operator.
            project_path: Optional project workspace root.

        Returns:
            List of selected skill names.
        """
        import re
        selected: list[str] = []
        seen: set[str] = set()

        if available_skills is None:
            try:
                from validators.skill.store import SkillStore
                from validators.skill.models import SkillStatus
                store = SkillStore(project_path)
                available_skills = store.list_skills(status=SkillStatus.ACTIVE, include_global=True)
            except Exception:
                available_skills = []

        skill_map = {getattr(s, "name", "").lower(): s for s in available_skills}

        # 1. Honor explicit skills requested by Architect or Work Order
        if explicit_skills:
            for sk in explicit_skills:
                clean = str(sk).strip().lower()
                if not clean:
                    continue
                if clean in skill_map:
                    if clean not in seen:
                        selected.append(clean)
                        seen.add(clean)
                else:
                    norm = clean.replace("-", "_")
                    alt = clean.replace("_", "-")
                    if norm in skill_map and norm not in seen:
                        selected.append(norm)
                        seen.add(norm)
                    elif alt in skill_map and alt not in seen:
                        selected.append(alt)
                        seen.add(alt)
                    elif clean not in seen:
                        selected.append(clean)
                        seen.add(clean)

        # 2. If explicit skills were designated by Architect/Operator, honor them as authoritative
        if selected:
            return selected

        # 3. Classify task domain using Laya
        decision = self.classify_task(title=title, body=description)
        task_cat = decision.category
        combined_text = f"{title} {description} {role} {deliverable_path or ''}".lower()
        role_norm = role.lower()
        deliv_norm = (deliverable_path or "").lower()

        # 4. Score and rank candidate skills
        for s in available_skills:
            s_name = getattr(s, "name", "").lower()
            if not s_name or s_name in seen:
                continue

            should_select = False

            # --- Rule A: ASD-STE100 (Simplified Technical English for instructions/coordination) ---
            if s_name in ("asd-ste100", "asd_ste100"):
                is_clarity_needed = any(
                    kw in combined_text
                    for kw in (
                        "ste100", "simplified", "english", "instruction", "instructions",
                        "clarity", "clear", "ambiguity", "ambiguous", "prompt", "prompts",
                        "spec", "specification", "document", "documentation", "coordination",
                        "handoff", "review", "communicate", "writing", "plain language",
                    )
                )
                is_coordination_role = role_norm in ("qa", "architect", "reviewer", "lead")
                is_spec_or_doc = any(
                    deliv_norm.endswith(ext)
                    for ext in (".md", ".txt", ".rst", ".yaml", ".yml", ".json")
                ) and any(kw in deliv_norm for kw in ("spec", "plan", "doc", "contract", "prompt"))
                if is_clarity_needed or (is_coordination_role and task_cat in (TaskCategory.ARCHITECTURE, TaskCategory.TESTING)) or is_spec_or_doc:
                    should_select = True

            # --- Rule B: Ponytail (Lazy / Minimal Code / Standard Library First / YAGNI) ---
            elif s_name == "ponytail":
                is_code_file = any(
                    deliv_norm.endswith(ext)
                    for ext in (".py", ".ts", ".js", ".html", ".css", ".go", ".rs", ".sql")
                ) or deliv_norm.startswith("src/")
                is_impl_task = any(
                    kw in combined_text
                    for kw in ("implement", "feature", "logic", "module", "service", "build", "refactor", "create", "api", "endpoint", "component")
                )
                if (is_code_file or is_impl_task) and (
                    task_cat in (TaskCategory.BACKEND, TaskCategory.FRONTEND, TaskCategory.GENERAL)
                    or role_norm in ("backend", "frontend", "codex", "gemini")
                ):
                    should_select = True

            # --- Rule C: Testing & QA Skills ---
            elif any(t_kw in s_name for t_kw in ("test", "pytest", "qa", "fix", "verify")):
                is_test_task = (
                    task_cat == TaskCategory.TESTING
                    or "tests/" in deliv_norm
                    or deliv_norm.startswith("test_")
                    or any(kw in combined_text for kw in ("test", "pytest", "unit test", "integration", "verify", "qa", "assert", "coverage"))
                )
                if is_test_task or role_norm in ("qa", "gemma", "tester"):
                    should_select = True

            # --- Rule D: GitOps & Release Skills ---
            elif any(g_kw in s_name for g_kw in ("gitops", "release", "version", "changelog", "commit")):
                if task_cat == TaskCategory.GITOPS or any(kw in combined_text for kw in ("release", "changelog", "bump", "version", "tag", "commit")):
                    should_select = True

            # --- Rule E: Direct Tool / Skill Name Mention in Task ---
            if not should_select:
                if s_name in combined_text or s_name.replace("-", "_") in combined_text or s_name.replace("_", "-") in combined_text:
                    should_select = True
                else:
                    # Check significant parts of compound skill name (e.g., 'docker' in 'docker-deployer', 'prisma' in 'prisma-migrate')
                    generic_parts = {"tool", "tools", "skill", "skills", "helper", "runner", "plugin", "script", "task", "agent", "flow", "test", "check"}
                    parts = [p for p in re.split(r"[-_]", s_name) if len(p) >= 4 and p not in generic_parts]
                    if any(part in combined_text for part in parts):
                        should_select = True

            # --- Rule F: Target Module Matching ---
            if not should_select and hasattr(s, "applicability") and s.applicability:
                for mod in getattr(s.applicability, "target_modules", ()):
                    if mod == "*":
                        continue
                    clean_mod = mod.rstrip("*").rstrip("/")
                    if clean_mod and (clean_mod in deliv_norm or clean_mod in combined_text):
                        should_select = True
                        break

            # --- Rule G: Semantic & Stemmed Keyword Overlap ---
            if not should_select and hasattr(s, "description") and s.description:
                def _stem(word: str) -> str:
                    w = word.lower().strip()
                    for suffix in ("ing", "ers", "er", "ies", "ied", "ed", "es", "s", "ment", "tion", "ator", "ize", "ise"):
                        if len(w) > len(suffix) + 3 and w.endswith(suffix):
                            return w[:-len(suffix)]
                    return w

                desc_text = f"{s.name} {s.description}".lower()
                s_tokens = set(re.findall(r"\w+", desc_text))
                stop_words = {"the", "and", "for", "with", "that", "this", "from", "into", "skill", "procedure", "will", "when", "using", "your"}
                meaningful = {t for t in s_tokens if len(t) > 3 and t not in stop_words}
                task_tokens = set(re.findall(r"\w+", combined_text))

                if len(meaningful.intersection(task_tokens)) >= 2:
                    should_select = True
                else:
                    stem_s = {_stem(t) for t in meaningful}
                    stem_task = {_stem(t) for t in task_tokens if len(t) > 3 and t not in stop_words}
                    if len(stem_s.intersection(stem_task)) >= 2:
                        should_select = True

            # --- Rule H: Domain Category Cross-Check ---
            if not should_select and hasattr(s, "description") and s.description:
                desc_lower = s.description.lower()
                domain_cues: dict[TaskCategory, tuple[str, ...]] = {
                    TaskCategory.FRONTEND: ("ui", "css", "html", "styling", "frontend", "layout", "view", "component", "dom"),
                    TaskCategory.BACKEND: ("backend", "api", "database", "sql", "orm", "endpoint", "route", "service", "migration"),
                    TaskCategory.TESTING: ("test", "testing", "pytest", "qa", "verify", "assert", "coverage", "mock", "e2e"),
                    TaskCategory.GITOPS: ("git", "docker", "k8s", "kubernetes", "container", "deploy", "release", "ci", "cd"),
                    TaskCategory.ARCHITECTURE: ("architecture", "spec", "specification", "rfc", "contract", "plan", "milestone"),
                }
                cues = domain_cues.get(task_cat, ())
                matched_cues = [c for c in cues if c in desc_lower]
                if len(matched_cues) >= 2 and any(c in combined_text for c in matched_cues):
                    should_select = True

            if should_select:
                selected.append(s.name)
                seen.add(s_name)

        return selected

    def filter_skills_for_architect(
        self,
        available_skills: Sequence[Any],
        task_title: str = "",
        task_description: str = "",
        max_skills: int = 6,
    ) -> list[Any]:
        """Pre-filter and prioritize relevant procedural skills for the Architect's briefing.

        Prevents context dilution and token budget exhaustion by selecting the top-scoring
        skills aligned with the user goal/domain instead of dumping the entire library.
        """
        import re

        if not available_skills:
            return []
        if len(available_skills) <= max_skills:
            return list(available_skills)

        combined_text = f"{task_title} {task_description}".lower().strip()
        task_cat = self.classify_task(title=task_title, body=task_description).category if combined_text else TaskCategory.GENERAL

        domain_cues: dict[TaskCategory, tuple[str, ...]] = {
            TaskCategory.FRONTEND: ("ui", "css", "html", "style", "styling", "frontend", "layout", "view", "component", "dom", "browser", "animation"),
            TaskCategory.BACKEND: ("backend", "api", "database", "sql", "orm", "endpoint", "route", "service", "migration", "server"),
            TaskCategory.TESTING: ("test", "testing", "pytest", "qa", "verify", "assert", "coverage", "mock", "e2e"),
            TaskCategory.GITOPS: ("git", "docker", "k8s", "kubernetes", "container", "deploy", "release", "ci", "cd"),
            TaskCategory.ARCHITECTURE: ("architecture", "spec", "specification", "rfc", "contract", "plan", "milestone"),
        }

        stop_words = {"the", "and", "for", "with", "that", "this", "from", "into", "skill", "procedure", "will", "when", "using", "your", "build", "make"}
        task_tokens = {t for t in re.findall(r"\w+", combined_text) if len(t) > 3 and t not in stop_words}

        scored: list[tuple[float, int, Any]] = []
        for idx, s in enumerate(available_skills):
            s_name = getattr(s, "name", "").lower()
            desc = getattr(s, "description", "").lower()
            score = 0.0

            # Direct name match in task text
            if s_name and (s_name in combined_text or s_name.replace("-", "_") in combined_text or s_name.replace("_", "-") in combined_text):
                score += 100.0

            # Keyword token overlap with description
            desc_tokens = {t for t in re.findall(r"\w+", desc) if len(t) > 3 and t not in stop_words}
            overlap = len(desc_tokens.intersection(task_tokens))
            score += overlap * 10.0

            # Domain category alignment
            if task_cat in domain_cues:
                cues = domain_cues[task_cat]
                matched = sum(1 for c in cues if c in desc or c in s_name)
                score += matched * 8.0

            # Foundational skills boost when relevant
            if s_name == "ponytail":
                if not combined_text or any(kw in combined_text for kw in ("implement", "code", "build", "create", "write", "feature", "app")):
                    score += 15.0
            elif s_name in ("asd-ste100", "asd_ste100"):
                if not combined_text or any(kw in combined_text for kw in ("spec", "prompt", "instruct", "document", "clarity", "rule", "clean")):
                    score += 15.0

            # Metric bonus
            metrics = getattr(s, "metrics", None)
            if metrics and hasattr(metrics, "confidence_score"):
                score += float(getattr(metrics, "confidence_score", 0.0) or 0.0) * 2.0

            # Stable tie-breaking with negative index
            scored.append((score, -idx, s))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item[2] for item in scored[:max_skills]]


def classify_task_category(
    title: str,
    body: str = "",
    work_order_id: str | None = None,
    adapter: LayaDecisionAdapter | None = None,
) -> LayaDecisionResult:
    """Convenience helper to classify a task domain."""
    effective_adapter = adapter or LayaDecisionAdapter(mode=LayaMode.DISABLED)
    return effective_adapter.classify_task(title, body, work_order_id)
