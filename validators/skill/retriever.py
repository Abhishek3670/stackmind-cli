"""Skill Retrieval engine integrating active procedural skills into agent context.

Implements Phase 7 (Retrieval Integration).
Surfaces verified active skills matching task queries and preconditions,
while strictly enforcing Agent Contract scope boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from validators.knowledge.contract import AgentContract
from validators.skill.models import RiskTier, SkillRecord, SkillStatus
from validators.skill.store import SkillStore


@dataclass(frozen=True)
class SkillRetrievalResult:
    """A matched active skill with relevance score and prompt-ready procedural guidance."""

    skill: SkillRecord
    relevance_score: float
    matched_terms: tuple[str, ...]
    formatted_guidance: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.skill.metrics.confidence_score,
            "formatted_guidance": self.formatted_guidance,
            "matched_terms": list(self.matched_terms),
            "relevance_score": round(self.relevance_score, 3),
            "risk_tier": self.skill.risk_tier.value,
            "skill_id": self.skill.skill_id,
            "skill_name": self.skill.name,
            "version": self.skill.version,
        }


class SkillRetriever:
    """Discovers and formats active procedural skills for prompt injection."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.skill_store = SkillStore(self.project_path)

    @classmethod
    def _is_module_in_contract(cls, module_pattern: str, contract: AgentContract) -> bool:
        """Verify whether a target module pattern is allowed by an agent contract."""
        from validators.knowledge.contract import module_matches, path_to_module

        clean_module = path_to_module(module_pattern)

        # 1. Deny rules take precedence
        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module")
            if pattern and (
                module_matches(clean_module, pattern)
                or module_matches(module_pattern, pattern)
                or pattern in clean_module
            ):
                return False

        # 2. Allow rules
        for allow_rule in contract.allow_rules:
            pattern = allow_rule.get("module")
            if pattern and (
                pattern in {"*", "**"}
                or module_matches(clean_module, pattern)
                or module_matches(module_pattern, pattern)
                or clean_module.startswith(pattern.rstrip(".*"))
            ):
                return True

        return False

    def retrieve_skills(
        self,
        query: str,
        *,
        contract: AgentContract | str | Path | None = None,
        limit: int = 3,
        min_relevance: float = 0.2,
    ) -> list[SkillRetrievalResult]:
        """Retrieve and rank active skills matching the query and contract boundary."""
        # 1. Resolve contract if provided
        active_contract: AgentContract | None = None
        if contract is not None:
            if isinstance(contract, AgentContract):
                active_contract = contract
            else:
                active_contract = AgentContract.load(contract, self.project_path)

        # 2. Only active skills participate in runtime retrieval
        active_skills = self.skill_store.list_skills(status=SkillStatus.ACTIVE)
        if not active_skills:
            return []

        # Tokenize query
        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return []

        scored_skills: list[SkillRetrievalResult] = []

        for skill in active_skills:
            # 3. Hard Scope Boundary Gate: Skill target_modules must fall within contract allow scope
            if active_contract is not None:
                scope_allowed = True
                for target_mod in skill.applicability.target_modules:
                    if not self._is_module_in_contract(target_mod, active_contract):
                        scope_allowed = False
                        break
                if not scope_allowed:
                    # Filter out out-of-scope skill
                    continue

            # 4. Compute relevance score
            matched_terms: list[str] = []
            skill_text = f"{skill.name} {skill.description} {' '.join(skill.applicability.preconditions)} {' '.join(s.action for s in skill.steps)}".lower()

            for tok in query_tokens:
                if len(tok) > 2 and tok in skill_text:
                    matched_terms.append(tok)

            if not matched_terms:
                continue

            # Relevance ratio
            base_score = len(matched_terms) / len(query_tokens)
            # Boost score with skill's verified confidence
            final_score = round(base_score * skill.metrics.confidence_score, 3)

            if final_score >= min_relevance:
                formatted = self.format_single_skill(skill)
                scored_skills.append(
                    SkillRetrievalResult(
                        skill=skill,
                        relevance_score=final_score,
                        matched_terms=tuple(matched_terms),
                        formatted_guidance=formatted,
                    )
                )

        # Sort by relevance score descending
        scored_skills.sort(key=lambda r: (r.relevance_score, r.skill.metrics.confidence_score), reverse=True)
        return scored_skills[:limit]

    @classmethod
    def format_single_skill(cls, skill: SkillRecord) -> str:
        """Render a single verified skill into an actionable Markdown block."""
        lines = [
            f"#### Skill: `{skill.name}` (v{skill.version}, {skill.skill_id})",
            f"*Risk:* `{skill.risk_tier.value.upper()}` | *Confidence:* `{skill.metrics.confidence_score:.2f}`",
            f"*Description:* {skill.description}",
        ]

        if skill.applicability.preconditions:
            lines.append("*Preconditions:*")
            for pre in skill.applicability.preconditions:
                lines.append(f"  - {pre}")

        lines.append("*Execution Procedure:*")
        for step in skill.steps:
            cmd_str = f" (`{step.command_template}`)" if step.command_template else ""
            lines.append(f"  {step.step_index}. **[{step.tool}]** {step.action}{cmd_str}")

        if skill.fallback_procedure:
            lines.append(f"*Fallback:* {skill.fallback_procedure}")

        return "\n".join(lines)

    @classmethod
    def format_prompt_section(cls, results: Sequence[SkillRetrievalResult]) -> str:
        """Render a collection of retrieved skills into a prompt-ready guidance section."""
        if not results:
            return ""

        header = [
            "### Procedural Guidance (Verified Active Skills)",
            "The following verified procedural skills match your task preconditions and active scope boundary:\n",
        ]
        body = "\n\n".join(r.formatted_guidance for r in results)
        return "\n".join(header) + body
