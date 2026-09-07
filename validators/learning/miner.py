"""High-level Pattern Mining coordinator for StackMind.

Implements Phase 4 (Pattern Mining) end-to-end pipeline.
Discovers clusters of verified execution experience and synthesizes candidate skills.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from validators.experience.store import ExperienceStore
from validators.learning.cluster import ClusterEngine, PatternCluster
from validators.learning.distiller import SkillDistiller
from validators.skill.models import SkillRecord
from validators.skill.store import SkillStore


class PatternMiner:
    """Orchestrates pattern mining across verified experience records."""

    def __init__(self, project_path: Path | str) -> None:
        self.project_path = Path(project_path).resolve()
        self.exp_store = ExperienceStore(self.project_path)
        self.skill_store = SkillStore(self.project_path)

    def mine_clusters(
        self,
        *,
        min_samples: int = 3,
        min_similarity: float = 0.5,
    ) -> list[PatternCluster]:
        """Mine recurring patterns from all learning-eligible experience records."""
        records = self.exp_store.list_records(only_learning_eligible=True)
        return ClusterEngine.cluster_records(
            records,
            min_samples=min_samples,
            min_similarity=min_similarity,
        )

    def distill_candidates(
        self,
        *,
        min_samples: int = 3,
        min_similarity: float = 0.5,
        save: bool = True,
        author_agent: str = "claude",
    ) -> list[SkillRecord]:
        """Mine clusters and distill all eligible clusters into Candidate SkillRecords."""
        clusters = self.mine_clusters(min_samples=min_samples, min_similarity=min_similarity)
        candidates: list[SkillRecord] = []

        for cluster in clusters:
            if cluster.is_distillation_eligible:
                # Check if a skill already exists with this name
                existing = self.skill_store.get_skill(cluster.intent_slug)
                if existing is not None:
                    continue

                candidate = SkillDistiller.distill_candidate_skill(
                    cluster,
                    author_agent=author_agent,
                )
                if save:
                    self.skill_store.save_version(candidate)
                candidates.append(candidate)

        return candidates
