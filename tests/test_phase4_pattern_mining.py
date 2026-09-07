"""Comprehensive test suite for Phase 4 (Pattern Mining).

Covers:
1. Task Normalization and intent extraction.
2. Hard Trust Gate: Only Learning-Eligible, completed episodes participate in pattern clustering.
3. Trajectory and action sequence similarity.
4. N >= 3 Evidence Gate for candidate distillation eligibility.
5. Skill candidate generation with strict provenance lineage.
6. Pattern Mining CLI commands (clusters, mine, distill).
"""

from __future__ import annotations

import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.experience.models import (
    ExperienceAction,
    ExperienceObservation,
    ExperienceRecord,
    ExperienceVerification,
)
from validators.experience.store import ExperienceStore
from validators.harness.snapshot import (
    TrustLevel,
    VerificationDimensions,
    WorkspaceDiff,
)
from validators.learning.cluster import ClusterEngine
from validators.learning.distiller import SkillDistiller
from validators.learning.miner import PatternMiner
from validators.learning.normalizer import TaskNormalizer
from validators.skill.models import RiskTier, SkillStatus
from validators.skill.store import SkillStore


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "mining-test-workspace"
    init(project, name="Pattern Mining Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _create_episode(
    store: ExperienceStore,
    signature: str,
    timestamp: str,
    *,
    learning_eligible: bool = True,
    outcome: str = "completed",
    tools_and_commands: list[tuple[str, str]] = (("bash", "alembic upgrade head"),),
    modified_files: list[str] = ("alembic/versions/001.py",),
) -> ExperienceRecord:
    exp_id = ExperienceRecord.mint_id(signature, timestamp)
    diff = WorkspaceDiff(modified=tuple(modified_files))
    dimensions = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )
    verification = ExperienceVerification(
        dimensions=dimensions if learning_eligible else VerificationDimensions(),
        observed_diff=diff,
    )
    actions = tuple(
        ExperienceAction(
            tool=tool,
            command_or_symbol=cmd,
            input_summary=cmd,
            timestamp=timestamp,
        )
        for tool, cmd in tools_and_commands
    )
    observations = tuple(
        ExperienceObservation(
            output_summary="Success",
            exit_code=0,
            is_error=False,
        )
        for _ in tools_and_commands
    )

    rec = ExperienceRecord(
        experience_id=exp_id,
        task_id=f"task-{signature}",
        agent_id="codex",
        task_signature=signature,
        environment={"runtime_version": "v3.1.0"},
        actions=actions,
        observations=observations,
        verification=verification,
        trust_level=TrustLevel.LEARNING_ELIGIBLE if learning_eligible else TrustLevel.OBSERVABLE,
        learning_eligible=learning_eligible,
        outcome=outcome,
        final_state={"summary": f"Completed {signature}"},
        recorded_at=timestamp,
    )
    store.save_record(rec)
    return rec


# ─── 1. Task Normalization ───────────────────────────────────────────────────

def test_task_normalizer_intent_extraction():
    # 1. Stripping noisy identifiers
    sig1 = "WO-042: apply database migration for user table 2026-09-01T12:00:00Z"
    intent1 = TaskNormalizer.normalize_signature(sig1)
    assert "database_migration" in intent1

    # 2. Extracting authentication domain
    sig2 = "rotate jwt auth secrets EXP-1234567890abcdef"
    intent2 = TaskNormalizer.normalize_signature(sig2)
    assert "authentication_flow" in intent2

    # 3. Cache management
    sig3 = "tune redis cache pool size"
    intent3 = TaskNormalizer.normalize_signature(sig3)
    assert "cache_management" in intent3


# ─── 2. Learning-Eligibility Gate Invariant ───────────────────────────────────

def test_pattern_mining_rejects_ineligible_episodes(temp_workspace):
    store = ExperienceStore(temp_workspace)

    # 3 unverified or failed episodes
    _create_episode(store, "migration:user_table", "2026-09-01T10:00:00Z", learning_eligible=False)
    _create_episode(store, "migration:user_table", "2026-09-01T11:00:00Z", outcome="failed")
    _create_episode(store, "migration:user_table", "2026-09-01T12:00:00Z", learning_eligible=False)

    miner = PatternMiner(temp_workspace)
    clusters = miner.mine_clusters(min_samples=1)
    assert len(clusters) == 0, "Ineligible episodes must never participate in clustering"


# ─── 3. Trajectory & Action Sequence Similarity ──────────────────────────────

def test_action_sequence_clustering(temp_workspace):
    store = ExperienceStore(temp_workspace)

    # Migration group (3 episodes with alembic)
    _create_episode(
        store,
        "migration:users",
        "2026-09-01T10:00:00Z",
        tools_and_commands=[("bash", "alembic current"), ("bash", "alembic upgrade head")],
    )
    _create_episode(
        store,
        "migration:orders",
        "2026-09-01T11:00:00Z",
        tools_and_commands=[("bash", "alembic current"), ("bash", "alembic upgrade head")],
    )
    _create_episode(
        store,
        "migration:payments",
        "2026-09-01T12:00:00Z",
        tools_and_commands=[("bash", "alembic current"), ("bash", "alembic upgrade head")],
    )

    # Auth group (disjoint trajectory)
    _create_episode(
        store,
        "auth:rotate_key",
        "2026-09-01T13:00:00Z",
        tools_and_commands=[("bash", "openssl genrsa"), ("bash", "kubectl create secret")],
    )

    miner = PatternMiner(temp_workspace)
    clusters = miner.mine_clusters(min_samples=1)

    assert len(clusters) == 2
    migration_cluster = next(c for c in clusters if "database_migration" in c.intent_slug)
    auth_cluster = next(c for c in clusters if "authentication_flow" in c.intent_slug)

    assert migration_cluster.sample_count == 3
    assert migration_cluster.similarity_score == 1.0
    assert auth_cluster.sample_count == 1


# ─── 4. N >= 3 Evidence Gate for Distillation ─────────────────────────────────

def test_n_greater_equal_3_distillation_gate(temp_workspace):
    store = ExperienceStore(temp_workspace)

    # Cluster with N = 2 (not eligible for distillation)
    _create_episode(store, "redis:tune_pool", "2026-09-01T10:00:00Z")
    _create_episode(store, "redis:tune_pool", "2026-09-01T11:00:00Z")

    miner = PatternMiner(temp_workspace)
    clusters = miner.mine_clusters(min_samples=3)
    assert len(clusters) == 1
    assert not clusters[0].is_distillation_eligible
    assert clusters[0].sample_count == 2

    # Attempting to distill N < 3 raises ValueError
    with pytest.raises(ValueError, match="not eligible for distillation"):
        SkillDistiller.distill_candidate_skill(clusters[0])

    # Add 3rd episode -> now eligible
    _create_episode(store, "redis:tune_pool", "2026-09-01T12:00:00Z")
    clusters_updated = miner.mine_clusters(min_samples=3)
    assert len(clusters_updated) == 1
    assert clusters_updated[0].is_distillation_eligible
    assert clusters_updated[0].sample_count == 3


# ─── 5. Skill Candidate Distillation with Provenance ──────────────────────────

def test_skill_candidate_distillation_provenance(temp_workspace):
    store = ExperienceStore(temp_workspace)
    episodes = [
        _create_episode(
            store,
            f"migration:test_{i}",
            f"2026-09-01T1{i}:00:00Z",
            tools_and_commands=[("bash", "alembic current"), ("bash", "alembic upgrade head")],
            modified_files=[f"alembic/versions/00{i}.py"],
        )
        for i in range(1, 4)
    ]

    miner = PatternMiner(temp_workspace)
    candidates = miner.distill_candidates(min_samples=3, save=True)

    assert len(candidates) == 1
    cand = candidates[0]

    assert cand.status == SkillStatus.CANDIDATE
    assert cand.risk_tier == RiskTier.MEDIUM  # Derived from alembic migration commands
    assert len(cand.steps) == 2
    assert set(cand.provenance.source_experience_ids) == {e.experience_id for e in episodes}

    # Saved in SkillStore
    skill_store = SkillStore(temp_workspace)
    loaded = skill_store.get_skill(cand.name)
    assert loaded is not None
    assert loaded.skill_id == cand.skill_id


# ─── 6. Pattern Mining CLI Commands ──────────────────────────────────────────

def test_pattern_mining_cli(temp_workspace):
    store = ExperienceStore(temp_workspace)
    for i in range(1, 4):
        _create_episode(
            store,
            f"cache:pool_tune_{i}",
            f"2026-09-01T1{i}:00:00Z",
            tools_and_commands=[("bash", "redis-cli config set maxclients 10000")],
        )

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Clusters command
    res_clusters = runner.invoke(cli, ["learn", "clusters", "-p", str(temp_workspace), "-n", "3"])
    assert res_clusters.exit_code == 0
    assert "Discovered Pattern Clusters" in res_clusters.output
    assert "cache_management" in res_clusters.output

    # 2. Mine command
    res_mine = runner.invoke(cli, ["learn", "mine", "-p", str(temp_workspace), "-n", "3"])
    assert res_mine.exit_code == 0
    assert "Distilled 1 candidate skill" in res_mine.output
