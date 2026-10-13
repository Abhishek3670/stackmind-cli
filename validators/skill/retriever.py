"""Skill Retrieval engine integrating active procedural skills into agent context.

Implements Phase 7 (Retrieval Integration) and Skills Execution Profiles (Low, Medium, High).
Surfaces verified active skills matching task queries and preconditions,
while strictly enforcing Agent Contract scope boundaries, progressive loading,
and profile-governed token budgets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from validators.kernel.profile import ExecutionProfile, ProfilePolicy, get_profile_policy
from validators.knowledge.contract import AgentContract
from validators.skill.models import RiskTier, SkillRecord, SkillStatus
from validators.skill.store import SkillStore


def _estimate_skill_tokens(text: str) -> int:
    """Conservative token estimation (1 token ~= 4 characters)."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class SkillRetrievalResult:
    """A matched active skill with relevance score, loading mode, and prompt guidance."""

    skill: SkillRecord
    relevance_score: float
    matched_terms: tuple[str, ...]
    formatted_guidance: str
    loading_mode: str = "full"  # "full" or "summary"
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.skill.metrics.confidence_score,
            "estimated_tokens": self.estimated_tokens,
            "formatted_guidance": self.formatted_guidance,
            "loading_mode": self.loading_mode,
            "matched_terms": list(self.matched_terms),
            "relevance_score": round(self.relevance_score, 3),
            "risk_tier": self.skill.risk_tier.value,
            "skill_id": self.skill.skill_id,
            "skill_name": self.skill.name,
            "version": self.skill.version,
        }


@dataclass(frozen=True)
class SkillRetrievalDiagnostics:
    """Audit diagnostics distinguishing considered, selected, loaded, and injected skills."""

    profile: str
    skills_enabled: bool
    considered_count: int
    selected_count: int
    loaded_count: int
    injected_count: int
    considered_ids: tuple[str, ...] = ()
    selected_ids: tuple[str, ...] = ()
    loaded_ids: tuple[str, ...] = ()
    injected_ids: tuple[str, ...] = ()
    estimated_skill_tokens: int = 0
    skill_token_budget: int = 0
    budget_exceeded: bool = False
    exclusions: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_exceeded": self.budget_exceeded,
            "considered_count": self.considered_count,
            "considered_ids": list(self.considered_ids),
            "estimated_skill_tokens": self.estimated_skill_tokens,
            "exclusions": list(self.exclusions),
            "injected_count": self.injected_count,
            "injected_ids": list(self.injected_ids),
            "loaded_count": self.loaded_count,
            "loaded_ids": list(self.loaded_ids),
            "profile": self.profile,
            "selected_count": self.selected_count,
            "selected_ids": list(self.selected_ids),
            "skill_token_budget": self.skill_token_budget,
            "skills_enabled": self.skills_enabled,
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

        # Universal wildcards or empty patterns signify general procedural knowledge
        # (e.g. coding conventions, TDD, minimal design, refactoring) that applies across
        # whatever files or code paths the agent's contract authorizes.
        if module_pattern in {"*", "**", "", None}:
            return bool(contract.allow_rules)

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

    def retrieve_skills_with_diagnostics(
        self,
        query: str,
        *,
        contract: AgentContract | str | Path | None = None,
        profile: ExecutionProfile | str | None = None,
        policy: ProfilePolicy | None = None,
        limit: int | None = None,
        token_budget: int | None = None,
        min_relevance: float | None = None,
        explicit_skills: Sequence[str] | None = None,
    ) -> tuple[list[SkillRetrievalResult], SkillRetrievalDiagnostics]:
        """Retrieve and progressively rank active skills returning full lifecycle diagnostics."""
        active_policy = policy or get_profile_policy(profile)
        effective_limit = limit if limit is not None else active_policy.skill_limit
        effective_budget = (
            token_budget if token_budget is not None else active_policy.skill_token_budget
        )
        effective_min_relevance = (
            min_relevance if min_relevance is not None else active_policy.min_relevance
        )

        # 1. Low profile policy: completely disable optional skill retrieval
        if not active_policy.skills_enabled or effective_limit <= 0 or effective_budget <= 0:
            diag = SkillRetrievalDiagnostics(
                profile=active_policy.profile.value,
                skills_enabled=False,
                considered_count=0,
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=(),
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_skill_tokens=0,
                skill_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=(),
            )
            return [], diag

        # 2. Resolve contract if provided
        active_contract: AgentContract | None = None
        if contract is not None:
            if isinstance(contract, AgentContract):
                active_contract = contract
            else:
                active_contract = AgentContract.load(contract, self.project_path)

        # Collect explicit skills chosen by Architect (from parameter or contract)
        chosen_skills: set[str] = set()
        if explicit_skills:
            for s in explicit_skills:
                chosen_skills.add(str(s).strip().lower())
        if active_contract and hasattr(active_contract, "skills") and active_contract.skills:
            for s in active_contract.skills:
                chosen_skills.add(str(s).strip().lower())

        # 3. Candidate Identification: only ACTIVE verified skills are eligible
        active_skills = self.skill_store.list_skills(status=SkillStatus.ACTIVE)
        considered_ids = tuple(s.skill_id for s in active_skills)
        if not active_skills:
            diag = SkillRetrievalDiagnostics(
                profile=active_policy.profile.value,
                skills_enabled=True,
                considered_count=0,
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=(),
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_skill_tokens=0,
                skill_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=(),
            )
            return [], diag

        # Tokenize query for relevance scoring
        query_tokens = set(re.findall(r"\w+", query.lower()))
        exclusions: list[dict[str, str]] = []

        if not query_tokens and not chosen_skills:
            diag = SkillRetrievalDiagnostics(
                profile=active_policy.profile.value,
                skills_enabled=True,
                considered_count=len(active_skills),
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=considered_ids,
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_skill_tokens=0,
                skill_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=tuple(
                    {"skill_id": sid, "reason": "empty_query_tokens"} for sid in considered_ids
                ),
            )
            return [], diag

        # 4. Filter, scope check, and score candidates
        candidates: list[tuple[SkillRecord, float, list[str]]] = []
        seen_skill_names: set[str] = set()

        for skill in active_skills:
            # Deduplicate by canonical skill name
            if skill.name in seen_skill_names:
                exclusions.append({"skill_id": skill.skill_id, "reason": "duplicate_skill_name"})
                continue

            # Check if this skill was explicitly selected by the Architect
            is_explicit = (
                skill.name.lower() in chosen_skills
                or skill.skill_id.lower() in chosen_skills
                or skill.name.lower().replace("-", "_") in chosen_skills
                or skill.name.lower().replace("_", "-") in chosen_skills
                or any(cs in skill.name.lower() or skill.name.lower() in cs for cs in chosen_skills if len(cs) > 2)
            )

            # Hard Scope Boundary Gate: Skill target_modules must fall within contract allow scope
            # Explicitly assigned skills by the Architect are pre-authorized for the task
            if active_contract is not None and not is_explicit:
                scope_allowed = True
                for target_mod in skill.applicability.target_modules:
                    if not self._is_module_in_contract(target_mod, active_contract):
                        scope_allowed = False
                        break
                if not scope_allowed:
                    exclusions.append({"skill_id": skill.skill_id, "reason": "contract_scope_denied"})
                    continue

            if is_explicit:
                # Architect-chosen skill receives top relevance and guaranteed selection
                matched_terms = ["architect_assigned", skill.name]
                final_score = 1.0
                seen_skill_names.add(skill.name)
                candidates.append((skill, final_score, matched_terms))
                continue

            # Compute relevance score for ambient skills
            matched_terms: list[str] = []
            skill_text = (
                f"{skill.name} {skill.description} "
                f"{' '.join(skill.applicability.preconditions)} "
                f"{' '.join(s.action for s in skill.steps)}"
            ).lower()

            for tok in query_tokens:
                if len(tok) > 2 and tok in skill_text:
                    matched_terms.append(tok)

            if not matched_terms:
                exclusions.append({"skill_id": skill.skill_id, "reason": "no_keyword_match"})
                continue

            base_score = len(matched_terms) / len(query_tokens) if query_tokens else 0.5
            final_score = round(base_score * skill.metrics.confidence_score, 3)

            if final_score < effective_min_relevance:
                exclusions.append({
                    "skill_id": skill.skill_id,
                    "reason": f"below_min_relevance ({final_score:.3f} < {effective_min_relevance:.3f})",
                })
                continue

            seen_skill_names.add(skill.name)
            candidates.append((skill, final_score, matched_terms))

        # Sort candidates by relevance descending, confidence descending
        candidates.sort(key=lambda c: (c[1], c[0].metrics.confidence_score), reverse=True)

        # 5. Selection: Cap at effective_limit
        selected_candidates = candidates[:effective_limit]
        selected_ids = tuple(c[0].skill_id for c in selected_candidates)

        for c in candidates[effective_limit:]:
            exclusions.append({
                "skill_id": c[0].skill_id,
                "reason": f"exceeded_profile_limit ({effective_limit})",
            })

        # 6. Progressive Loading & Token Budget Enforcement
        loaded_ids: list[str] = []
        injected_ids: list[str] = []
        injected_results: list[SkillRetrievalResult] = []
        accumulated_tokens = 0
        budget_exceeded = False

        for skill, score, matched_terms in selected_candidates:
            # Determine loading mode (progressive summary vs full procedure)
            if not active_policy.progressive_loading:
                load_mode = "full"
                formatted = self.format_single_skill(skill)
            else:
                # Progressive loading: load full runbook only when relevance is high
                if (
                    active_policy.profile == ExecutionProfile.HIGH
                    or score >= active_policy.full_detail_threshold
                ):
                    load_mode = "full"
                    formatted = self.format_single_skill(skill)
                else:
                    load_mode = "summary"
                    formatted = self.format_compact_summary(skill)

            est_tokens = _estimate_skill_tokens(formatted)
            loaded_ids.append(skill.skill_id)

            # Check if this loaded skill fits into the remaining skill token budget
            if accumulated_tokens + est_tokens <= effective_budget:
                accumulated_tokens += est_tokens
                injected_ids.append(skill.skill_id)
                injected_results.append(
                    SkillRetrievalResult(
                        skill=skill,
                        relevance_score=score,
                        matched_terms=tuple(matched_terms),
                        formatted_guidance=formatted,
                        loading_mode=load_mode,
                        estimated_tokens=est_tokens,
                    )
                )
            else:
                budget_exceeded = True
                exclusions.append({
                    "skill_id": skill.skill_id,
                    "reason": f"skill_token_budget_exceeded ({est_tokens} tokens exceeds remaining {effective_budget - accumulated_tokens})",
                })

        diag = SkillRetrievalDiagnostics(
            profile=active_policy.profile.value,
            skills_enabled=True,
            considered_count=len(active_skills),
            selected_count=len(selected_candidates),
            loaded_count=len(loaded_ids),
            injected_count=len(injected_ids),
            considered_ids=considered_ids,
            selected_ids=selected_ids,
            loaded_ids=tuple(loaded_ids),
            injected_ids=tuple(injected_ids),
            estimated_skill_tokens=accumulated_tokens,
            skill_token_budget=effective_budget,
            budget_exceeded=budget_exceeded,
            exclusions=tuple(exclusions),
        )
        return injected_results, diag

    def retrieve_skills(
        self,
        query: str,
        *,
        contract: AgentContract | str | Path | None = None,
        profile: ExecutionProfile | str | None = None,
        policy: ProfilePolicy | None = None,
        limit: int | None = None,
        token_budget: int | None = None,
        min_relevance: float | None = None,
        return_diagnostics: bool = False,
    ) -> list[SkillRetrievalResult] | tuple[list[SkillRetrievalResult], SkillRetrievalDiagnostics]:
        """Retrieve skills preserving backwards compatibility."""
        results, diagnostics = self.retrieve_skills_with_diagnostics(
            query,
            contract=contract,
            profile=profile,
            policy=policy,
            limit=limit,
            token_budget=token_budget,
            min_relevance=min_relevance,
        )
        if return_diagnostics:
            return results, diagnostics
        return results

    @classmethod
    def format_single_skill(cls, skill: SkillRecord) -> str:
        """Render a single verified skill into an actionable Markdown runbook block."""
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
    def format_compact_summary(cls, skill: SkillRecord) -> str:
        """Render a concise skill summary suitable for progressive loading."""
        lines = [
            f"#### Skill Summary: `{skill.name}` (v{skill.version}, {skill.skill_id})",
            f"*Risk:* `{skill.risk_tier.value.upper()}` | *Confidence:* `{skill.metrics.confidence_score:.2f}`",
            f"*Description:* {skill.description}",
        ]

        if skill.applicability.preconditions:
            lines.append(f"*Preconditions:* {', '.join(skill.applicability.preconditions)}")

        tools_used = [step.tool for step in skill.steps if step.tool]
        if tools_used:
            dedup_tools = list(dict.fromkeys(tools_used))
            lines.append(f"*Tools:* {', '.join(dedup_tools)}")

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
