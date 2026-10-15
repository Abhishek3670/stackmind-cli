"""Memory Retrieval engine integrating durable memories into agent context.

Extends the existing Knowledge API retrieval path with memory-specific
search, filtering, and context assembly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.kernel.profile import ExecutionProfile, ProfilePolicy, get_profile_policy
from validators.knowledge.contract import AgentContract
from validators.memory.models import MemoryRecord, MemoryStatus, MemoryType
from validators.memory.store import MemoryStore, MemorySearchResult
from validators.memory.decay import DecayAdjuster, DecayPolicy, DecayDiagnostics, get_decay_policy


def _estimate_memory_tokens(text: str) -> int:
    """Conservative token estimation (1 token ~= 4 characters)."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class MemoryRetrievalResult:
    """A matched memory with relevance score, loading mode, and prompt guidance."""

    memory: MemoryRecord
    relevance_score: float
    matched_terms: tuple[str, ...]
    formatted_guidance: str
    loading_mode: str = "full"  # "full" or "summary"
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.memory.provenance.confidence,
            "estimated_tokens": self.estimated_tokens,
            "formatted_guidance": self.formatted_guidance,
            "loading_mode": self.loading_mode,
            "matched_terms": list(self.matched_terms),
            "relevance_score": round(self.relevance_score, 3),
            "memory_id": self.memory.memory_id,
            "memory_type": self.memory.memory_type.value,
            "status": self.memory.status.value,
        }


@dataclass(frozen=True)
class MemoryRetrievalDiagnostics:
    """Audit diagnostics distinguishing considered, selected, loaded, and injected memories."""

    profile: str
    memories_enabled: bool
    considered_count: int
    selected_count: int
    loaded_count: int
    injected_count: int
    considered_ids: tuple[str, ...] = ()
    selected_ids: tuple[str, ...] = ()
    loaded_ids: tuple[str, ...] = ()
    injected_ids: tuple[str, ...] = ()
    estimated_memory_tokens: int = 0
    memory_token_budget: int = 0
    budget_exceeded: bool = False
    exclusions: tuple[dict[str, str], ...] = ()
    decay_diagnostics: DecayDiagnostics | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "budget_exceeded": self.budget_exceeded,
            "considered_count": self.considered_count,
            "considered_ids": list(self.considered_ids),
            "estimated_memory_tokens": self.estimated_memory_tokens,
            "exclusions": list(self.exclusions),
            "injected_count": self.injected_count,
            "injected_ids": list(self.injected_ids),
            "loaded_count": self.loaded_count,
            "loaded_ids": list(self.loaded_ids),
            "memory_token_budget": self.memory_token_budget,
            "memories_enabled": self.memories_enabled,
            "profile": self.profile,
            "selected_count": self.selected_count,
            "selected_ids": list(self.selected_ids),
        }
        if self.decay_diagnostics is not None:
            result["decay_diagnostics"] = self.decay_diagnostics.to_dict()
        return result


class MemoryRetriever:
    """Discovers and formats active durable memories for prompt injection."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.memory_store = MemoryStore(self.project_path)

    @classmethod
    def _is_memory_authorized(
        cls,
        memory: MemoryRecord,
        contract: AgentContract,
    ) -> bool:
        """Verify whether a memory is authorized for access under the agent contract.

        Memories are scoped by project_id. The contract's allow/deny rules are checked
        against the memory's project_id. This provides a memory-specific authorization
        check that respects contract boundaries without inappropriately applying
        source-code module-path rules.
        """
        if not contract.allow_rules and not contract.deny_rules:
            # No explicit rules means no restriction (open by default)
            return True

        memory_project = memory.project_id

        # Deny rules take precedence
        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module") or deny_rule.get("project") or deny_rule.get("target")
            if pattern:
                # Check project_id match
                if pattern in {"*", "**"} or pattern == memory_project:
                    return False

        # Allow rules - at least one must match
        if contract.allow_rules:
            allowed = False
            for allow_rule in contract.allow_rules:
                pattern = allow_rule.get("module") or allow_rule.get("project") or allow_rule.get("target")
                if pattern:
                    if pattern in {"*", "**"} or pattern == memory_project:
                        allowed = True
                        break
            if not allowed:
                return False

        return True

    @classmethod
    def _is_expired(cls, memory: MemoryRecord) -> bool:
        """Check if a memory has expired based on expires_at timestamp."""
        if memory.expires_at is None:
            return False
        try:
            expires = datetime.fromisoformat(memory.expires_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return now >= expires
        except Exception:
            # On parse error, treat as not expired (fail open for validity)
            return False

    @classmethod
    def _is_module_in_contract(cls, module_pattern: str, contract: AgentContract) -> bool:
        """Verify whether a target module pattern is allowed by an agent contract."""
        from validators.knowledge.contract import module_matches, path_to_module

        if module_pattern in {"*", "**", "", None}:
            return bool(contract.allow_rules)

        clean_module = path_to_module(module_pattern)

        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module")
            if pattern and (
                module_matches(clean_module, pattern)
                or module_matches(module_pattern, pattern)
                or pattern in clean_module
            ):
                return False

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

    def retrieve_memories_with_diagnostics(
        self,
        query: str,
        *,
        contract: AgentContract | str | Path | None = None,
        profile: ExecutionProfile | str | None = None,
        policy: ProfilePolicy | None = None,
        limit: int | None = None,
        token_budget: int | None = None,
        min_relevance: float | None = None,
        explicit_memories: Sequence[str] | None = None,
        project_id: str | None = None,
    ) -> tuple[list[MemoryRetrievalResult], MemoryRetrievalDiagnostics]:
        """Retrieve and progressively rank active memories returning full lifecycle diagnostics."""
        active_policy = policy or get_profile_policy(profile)

        # For memories, we use a separate budget but respect the profile's skills_enabled as a proxy
        # In the future, this could be a separate memory policy
        memories_enabled = active_policy.skills_enabled
        effective_limit = limit if limit is not None else active_policy.skill_limit
        effective_budget = token_budget if token_budget is not None else max(300, active_policy.skill_token_budget // 2)
        effective_min_relevance = min_relevance if min_relevance is not None else active_policy.min_relevance

        # Low profile policy: completely disable optional memory retrieval
        if not memories_enabled or effective_limit <= 0 or effective_budget <= 0:
            diag = MemoryRetrievalDiagnostics(
                profile=active_policy.profile.value,
                memories_enabled=False,
                considered_count=0,
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=(),
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_memory_tokens=0,
                memory_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=(),
            )
            return [], diag

        # Resolve contract if provided
        active_contract: AgentContract | None = None
        if contract is not None:
            if isinstance(contract, AgentContract):
                active_contract = contract
            else:
                active_contract = AgentContract.load(contract, self.project_path)

        # Collect explicit memories chosen by Architect
        chosen_memories: set[str] = set()
        if explicit_memories:
            for m in explicit_memories:
                chosen_memories.add(str(m).strip().lower())

        # Candidate Identification: only ACTIVE memories are eligible by default
        active_memories = self.memory_store.list_records(
            project_id=project_id,
            status=MemoryStatus.ACTIVE,
        )
        considered_ids = tuple(m.memory_id for m in active_memories)
        if not active_memories:
            diag = MemoryRetrievalDiagnostics(
                profile=active_policy.profile.value,
                memories_enabled=True,
                considered_count=0,
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=(),
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_memory_tokens=0,
                memory_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=(),
            )
            return [], diag

        # Tokenize query for relevance scoring
        query_tokens = set(re.findall(r"\w+", query.lower()))
        exclusions: list[dict[str, str]] = []

        if not query_tokens and not chosen_memories:
            diag = MemoryRetrievalDiagnostics(
                profile=active_policy.profile.value,
                memories_enabled=True,
                considered_count=len(active_memories),
                selected_count=0,
                loaded_count=0,
                injected_count=0,
                considered_ids=considered_ids,
                selected_ids=(),
                loaded_ids=(),
                injected_ids=(),
                estimated_memory_tokens=0,
                memory_token_budget=effective_budget,
                budget_exceeded=False,
                exclusions=tuple(
                    {"memory_id": mid, "reason": "empty_query_tokens"} for mid in considered_ids
                ),
            )
            return [], diag

        # Filter, scope check, and score candidates
        candidates: list[tuple[MemoryRecord, float, list[str]]] = []
        seen_memory_ids: set[str] = set()

        for memory in active_memories:
            if memory.memory_id in seen_memory_ids:
                exclusions.append({"memory_id": memory.memory_id, "reason": "duplicate_memory_id"})
                continue

            is_explicit = memory.memory_id.lower() in chosen_memories

            # Authorization check: both ambient and explicit memories must pass contract authorization
            if active_contract is not None:
                if not self._is_memory_authorized(memory, active_contract):
                    exclusions.append({
                        "memory_id": memory.memory_id,
                        "reason": "contract_scope_denied",
                    })
                    continue

            if is_explicit:
                # Explicit memories must still be lifecycle-valid (not deleted/expired)
                if memory.status in {MemoryStatus.DELETED, MemoryStatus.EXPIRED}:
                    exclusions.append({
                        "memory_id": memory.memory_id,
                        "reason": f"explicit_memory_lifecycle_invalid ({memory.status.value})",
                    })
                    continue
                # Also check expiration timestamp
                if self._is_expired(memory):
                    exclusions.append({
                        "memory_id": memory.memory_id,
                        "reason": "explicit_memory_expired",
                    })
                    continue
                matched_terms = ["architect_assigned", memory.memory_id]
                final_score = 1.0
                seen_memory_ids.add(memory.memory_id)
                candidates.append((memory, final_score, matched_terms))
                continue

            # Lifecycle check: skip deleted/expired/superseded memories for ambient retrieval
            if memory.status not in {MemoryStatus.ACTIVE}:
                exclusions.append({
                    "memory_id": memory.memory_id,
                    "reason": f"lifecycle_excluded ({memory.status.value})",
                })
                continue

            # Expiration check: skip expired memories even if status is ACTIVE
            if self._is_expired(memory):
                exclusions.append({
                    "memory_id": memory.memory_id,
                    "reason": "expired",
                })
                continue

            # Compute relevance score for ambient memories
            matched_terms: list[str] = []
            memory_text = (
                f"{memory.content} {memory.normalized_content} "
                f"{' '.join(memory.tags)}"
            ).lower()

            for tok in query_tokens:
                if len(tok) > 2 and tok in memory_text:
                    matched_terms.append(tok)

            if not matched_terms:
                exclusions.append({"memory_id": memory.memory_id, "reason": "no_keyword_match"})
                continue

            # Boost by provenance confidence
            base_score = len(matched_terms) / len(query_tokens) if query_tokens else 0.5
            provenance_boost = memory.provenance.confidence
            final_score = round(base_score * provenance_boost, 3)

            if final_score < effective_min_relevance:
                exclusions.append({
                    "memory_id": memory.memory_id,
                    "reason": f"below_min_relevance ({final_score:.3f} < {effective_min_relevance:.3f})",
                })
                continue

            seen_memory_ids.add(memory.memory_id)
            candidates.append((memory, final_score, matched_terms))

        # Sort candidates by relevance descending, provenance confidence descending
        candidates.sort(key=lambda c: (c[1], c[0].provenance.confidence), reverse=True)

        # Step 5: Apply memory decay as a retrieval-time signal (Stage 7)
        # Decay is a multiplicative factor on baseline relevance scores
        decay_policy = get_decay_policy(active_policy.profile)
        decay_adjuster = DecayAdjuster(self.project_path, decay_policy)

        # Check if shadow mode is enabled via environment/config
        # For now, we can enable via an explicit parameter or config
        import os
        if os.environ.get("STACKMIND_MEMORY_DECAY_SHADOW", "").lower() in ("1", "true", "yes"):
            decay_adjuster.enable_shadow_mode()

        # Extract memories and base scores for decay application
        candidate_memories = [c[0] for c in candidates]
        candidate_base_scores = [c[1] for c in candidates]

        decayed_scores, decay_diag = decay_adjuster.apply_decay(
            candidate_memories, candidate_base_scores, active_policy.profile
        )

        # Log decay diagnostics to audit trail
        decay_adjuster.log_audit(decay_diag, query)

        # Rebuild candidates with decayed scores
        decayed_candidates = []
        for i, (memory, _, matched_terms) in enumerate(candidates):
            decayed_candidates.append((memory, decayed_scores[i], matched_terms))

        # Re-sort by decayed relevance (decay may change ordering)
        decayed_candidates.sort(key=lambda c: (c[1], c[0].provenance.confidence), reverse=True)

        # Selection: Cap at effective_limit
        selected_candidates = decayed_candidates[:effective_limit]
        selected_ids = tuple(c[0].memory_id for c in selected_candidates)

        for c in decayed_candidates[effective_limit:]:
            exclusions.append({
                "memory_id": c[0].memory_id,
                "reason": f"exceeded_profile_limit ({effective_limit})",
            })

        # Progressive Loading & Token Budget Enforcement
        loaded_ids: list[str] = []
        injected_ids: list[str] = []
        injected_results: list[MemoryRetrievalResult] = []
        accumulated_tokens = 0
        budget_exceeded = False

        for memory, score, matched_terms in selected_candidates:
            # Determine loading mode (progressive summary vs full content)
            if not active_policy.progressive_loading:
                load_mode = "full"
                formatted = self.format_single_memory(memory)
            else:
                if (
                    active_policy.profile == ExecutionProfile.HIGH
                    or score >= active_policy.full_detail_threshold
                ):
                    load_mode = "full"
                    formatted = self.format_single_memory(memory)
                else:
                    load_mode = "summary"
                    formatted = self.format_compact_summary(memory)

            est_tokens = _estimate_memory_tokens(formatted)
            loaded_ids.append(memory.memory_id)

            # Check if this loaded memory fits into the remaining memory token budget
            if accumulated_tokens + est_tokens <= effective_budget:
                accumulated_tokens += est_tokens
                injected_ids.append(memory.memory_id)
                injected_results.append(
                    MemoryRetrievalResult(
                        memory=memory,
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
                    "memory_id": memory.memory_id,
                    "reason": f"memory_token_budget_exceeded ({est_tokens} tokens exceeds remaining {effective_budget - accumulated_tokens})",
                })

        diag = MemoryRetrievalDiagnostics(
            profile=active_policy.profile.value,
            memories_enabled=True,
            considered_count=len(active_memories),
            selected_count=len(selected_candidates),
            loaded_count=len(loaded_ids),
            injected_count=len(injected_ids),
            considered_ids=considered_ids,
            selected_ids=selected_ids,
            loaded_ids=tuple(loaded_ids),
            injected_ids=tuple(injected_ids),
            estimated_memory_tokens=accumulated_tokens,
            memory_token_budget=effective_budget,
            budget_exceeded=budget_exceeded,
            exclusions=tuple(exclusions),
            decay_diagnostics=decay_diag,
        )
        return injected_results, diag

    def retrieve_memories(
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
    ) -> list[MemoryRetrievalResult] | tuple[list[MemoryRetrievalResult], MemoryRetrievalDiagnostics]:
        """Retrieve memories preserving backwards compatibility."""
        results, diagnostics = self.retrieve_memories_with_diagnostics(
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
    def format_single_memory(cls, memory: MemoryRecord) -> str:
        """Render a single memory into an actionable Markdown guidance block."""
        lines = [
            f"#### Memory: `{memory.memory_id}` ({memory.memory_type.value})",
            f"*Project:* `{memory.project_id}` | *Confidence:* `{memory.provenance.confidence:.2f}` | *Status:* `{memory.status.value}`",
            f"*Content:* {memory.content}",
        ]

        if memory.evidence:
            lines.append("*Supporting Evidence:*")
            for ev in memory.evidence:
                lines.append(f"  - [{ev.evidence_type}] {ev.excerpt[:200]}")

        if memory.tags:
            lines.append(f"*Tags:* {', '.join(memory.tags)}")

        provenance = memory.provenance
        lines.append(f"*Source:* Experience(s) {', '.join(provenance.source_experience_ids)} (extracted by {provenance.extractor_agent})")

        return "\n".join(lines)

    @classmethod
    def format_compact_summary(cls, memory: MemoryRecord) -> str:
        """Render a concise memory summary suitable for progressive loading."""
        lines = [
            f"#### Memory Summary: `{memory.memory_id}` ({memory.memory_type.value})",
            f"*Project:* `{memory.project_id}` | *Confidence:* `{memory.provenance.confidence:.2f}` | *Status:* `{memory.status.value}`",
            f"*Content:* {memory.content[:300]}{'...' if len(memory.content) > 300 else ''}",
        ]

        if memory.tags:
            lines.append(f"*Tags:* {', '.join(memory.tags)}")

        return "\n".join(lines)

    @classmethod
    def format_prompt_section(cls, results: Sequence[MemoryRetrievalResult]) -> str:
        """Render a collection of retrieved memories into a prompt-ready guidance section."""
        if not results:
            return ""

        header = [
            "### Durable Memories (Verified Knowledge)",
            "The following verified memories match your task context:\n",
        ]
        body = "\n\n".join(r.formatted_guidance for r in results)
        return "\n".join(header) + body


class MemoryRetrievalBridge:
    """Bridge between MemoryRetriever and KnowledgeAPI for unified context assembly."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.memory_retriever = MemoryRetriever(self.project_path)

    def augment_context_bundle(
        self,
        query: str,
        *,
        contract: AgentContract | str | Path | None = None,
        profile: ExecutionProfile | str | None = None,
        policy: ProfilePolicy | None = None,
        token_budget: int = 1200,
        include_memories: bool = True,
        explicit_memories: Sequence[str] | None = None,
    ) -> tuple[str, MemoryRetrievalDiagnostics | None]:
        """Retrieve memories and format them for injection into a context bundle."""
        if not include_memories:
            return "", None

        active_policy = policy or get_profile_policy(profile)
        if not active_policy.skills_enabled:
            return "", None

        memory_budget = max(200, token_budget // 4)  # Reserve 25% for memories
        results, diag = self.memory_retriever.retrieve_memories_with_diagnostics(
            query,
            contract=contract,
            policy=active_policy,
            token_budget=memory_budget,
            explicit_memories=explicit_memories,
        )

        if not results:
            return "", diag

        formatted = MemoryRetriever.format_prompt_section(results)
        return formatted, diag