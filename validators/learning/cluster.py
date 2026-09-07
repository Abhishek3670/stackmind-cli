"""Trajectory similarity and pattern clustering engine for experience records.

Implements Phase 4 (Pattern Mining) clustering pipeline.
Groups learning-eligible episodes by normalized intent and aligned tool-action sequences.
"""

from __future__ import annotations

import difflib
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

from validators.experience.models import ExperienceRecord
from validators.learning.normalizer import TaskNormalizer


@dataclass(frozen=True)
class PatternCluster:
    """A cluster of semantically and sequentially similar experience executions."""

    cluster_id: str
    intent_slug: str
    sample_count: int
    experience_ids: tuple[str, ...]
    common_actions: tuple[dict[str, str], ...]
    similarity_score: float
    is_distillation_eligible: bool
    source_records: tuple[ExperienceRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "common_actions": list(self.common_actions),
            "experience_ids": list(self.experience_ids),
            "intent_slug": self.intent_slug,
            "is_distillation_eligible": self.is_distillation_eligible,
            "sample_count": self.sample_count,
            "similarity_score": round(self.similarity_score, 3),
        }


class ClusterEngine:
    """Clusters verified experience records into repeatable procedural patterns."""

    @classmethod
    def compute_action_similarity(cls, seq1: Sequence[str], seq2: Sequence[str]) -> float:
        """Compute sequence similarity ratio between two action tool sequences."""
        if not seq1 and not seq2:
            return 1.0
        if not seq1 or not seq2:
            return 0.0
        matcher = difflib.SequenceMatcher(None, seq1, seq2)
        return matcher.ratio()

    @classmethod
    def _extract_action_signature(cls, rec: ExperienceRecord) -> list[str]:
        return [f"{a.tool}:{a.command_or_symbol.split()[0] if a.command_or_symbol else ''}" for a in rec.actions]

    @classmethod
    def cluster_records(
        cls,
        records: Sequence[ExperienceRecord],
        *,
        min_samples: int = 3,
        min_similarity: float = 0.5,
    ) -> list[PatternCluster]:
        """Cluster verified experience records into distillation-eligible patterns."""
        # Hard gate: Only mine from verified LEARNING_ELIGIBLE successful episodes
        eligible_records = [r for r in records if r.learning_eligible and r.outcome == "completed"]

        if not eligible_records:
            return []

        # 1. Group by normalized intent
        intent_groups: dict[str, list[ExperienceRecord]] = defaultdict(list)
        for rec in eligible_records:
            intent = TaskNormalizer.normalize_signature(rec.task_signature)
            intent_groups[intent].append(rec)

        clusters: list[PatternCluster] = []

        # 2. Sub-cluster by trajectory / action sequence similarity
        for intent, group in intent_groups.items():
            sub_clusters: list[list[ExperienceRecord]] = []

            for rec in group:
                rec_seq = cls._extract_action_signature(rec)
                placed = False
                for cluster_members in sub_clusters:
                    rep_seq = cls._extract_action_signature(cluster_members[0])
                    sim = cls.compute_action_similarity(rec_seq, rep_seq)
                    if sim >= min_similarity:
                        cluster_members.append(rec)
                        placed = True
                        break
                if not placed:
                    sub_clusters.append([rec])

            # 3. Build PatternCluster artifacts
            for member_records in sub_clusters:
                sample_count = len(member_records)
                exp_ids = tuple(r.experience_id for r in member_records)

                # Compute pairwise average similarity
                if sample_count > 1:
                    sim_sum = 0.0
                    comparisons = 0
                    for i in range(sample_count):
                        for j in range(i + 1, sample_count):
                            s1 = cls._extract_action_signature(member_records[i])
                            s2 = cls._extract_action_signature(member_records[j])
                            sim_sum += cls.compute_action_similarity(s1, s2)
                            comparisons += 1
                    avg_sim = sim_sum / comparisons if comparisons > 0 else 1.0
                else:
                    avg_sim = 1.0

                # Extract consensus common actions
                common_actions = cls._extract_consensus_actions(member_records)

                # Deterministic cluster ID
                raw_cluster_id = f"{intent}:{':'.join(sorted(exp_ids))}".encode("utf-8")
                cluster_id = f"CLUSTER-{hashlib.sha256(raw_cluster_id).hexdigest()[:16]}"

                is_eligible = sample_count >= min_samples and avg_sim >= min_similarity

                clusters.append(
                    PatternCluster(
                        cluster_id=cluster_id,
                        intent_slug=intent,
                        sample_count=sample_count,
                        experience_ids=exp_ids,
                        common_actions=common_actions,
                        similarity_score=avg_sim,
                        is_distillation_eligible=is_eligible,
                        source_records=tuple(member_records),
                    )
                )

        # Sort clusters by sample count descending, then similarity descending
        clusters.sort(key=lambda c: (c.is_distillation_eligible, c.sample_count, c.similarity_score), reverse=True)
        return clusters

    @classmethod
    def _extract_consensus_actions(cls, records: Sequence[ExperienceRecord]) -> tuple[dict[str, str], ...]:
        """Extract the representative consensus sequence of actions from a cluster."""
        if not records:
            return ()

        # Use the first record's actions as a template
        rep = records[0]
        consensus = []
        for idx, act in enumerate(rep.actions, start=1):
            consensus.append(
                {
                    "action": f"Step {idx}: {act.tool} operation",
                    "command_template": act.command_or_symbol,
                    "rationale": act.input_summary,
                    "tool": act.tool,
                }
            )
        return tuple(consensus)
