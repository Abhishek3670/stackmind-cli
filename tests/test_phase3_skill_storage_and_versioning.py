"""Comprehensive test suite for Phase 3 (Skill Storage & Versioning).

Covers:
1. Deterministic SKILL- node ID minting.
2. Draft-7 JSON schema validation for SkillRecord.
3. Versioned manifest storage under .sync/skills/manifests/<name>/v<N>.json.
4. Promotion to ACTIVE and active pointer management under .sync/skills/active/<name>.json.
5. Rollback functionality creating a new active version preserving provenance lineage.
6. Deprecation and archiving lifecycle state transitions.
7. Skill CLI commands (create, list, show, promote, rollback, deprecate, stats).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.store import SkillStore


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "skill-test-workspace"
    init(project, name="Skill Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _sample_skill(
    name: str = "database_migration",
    version: int = 1,
    status: SkillStatus = SkillStatus.CANDIDATE,
    risk_tier: RiskTier = RiskTier.MEDIUM,
) -> SkillRecord:
    skill_id = SkillRecord.mint_id(name, version)
    return SkillRecord(
        skill_id=skill_id,
        name=name,
        version=version,
        status=status,
        risk_tier=risk_tier,
        description="Standard Alembic database schema migration procedure",
        applicability=SkillApplicability(
            preconditions=("Postgres service reachable", "Valid DATABASE_URL"),
            target_modules=("db/*", "alembic/*"),
            known_exclusions=("sqlite_in_memory",),
        ),
        steps=(
            SkillStep(
                step_index=1,
                action="Check database migration status",
                tool="bash",
                command_template="alembic current",
                expected_outcome="Prints current revision",
            ),
            SkillStep(
                step_index=2,
                action="Apply forward migrations",
                tool="bash",
                command_template="alembic upgrade head",
                expected_outcome="Schema upgraded to target revision",
            ),
        ),
        provenance=SkillProvenance(
            source_experience_ids=("EXP-1122334455667788",),
            created_at="2026-09-01T12:00:00+00:00",
            author_agent="claude",
        ),
        metrics=SkillMetrics(
            success_count=5,
            failure_count=0,
            success_rate=1.0,
            confidence_score=0.85,
        ),
    )


# ─── 1. Skill ID Minting & Schema Validation ─────────────────────────────────

def test_skill_id_minting_and_schema(temp_workspace):
    skill = _sample_skill()
    assert skill.skill_id.startswith("SKILL-")
    assert len(skill.skill_id) == 22  # SKILL- + 16 hex chars

    # Validate against JSON schema
    errors = skill.validate_schema(temp_workspace)
    assert errors == [], f"Schema validation errors: {errors}"


# ─── 2. Versioned Manifest Storage & Retrieval ───────────────────────────────

def test_skill_store_save_and_get(temp_workspace):
    store = SkillStore(temp_workspace)
    v1 = _sample_skill("auth_flow", 1, SkillStatus.CANDIDATE)
    store.save_version(v1)

    # v1 should exist in manifests, but not in active/
    assert (temp_workspace / ".sync" / "skills" / "manifests" / "auth_flow" / "v1.json").exists()
    assert not (temp_workspace / ".sync" / "skills" / "active" / "auth_flow.json").exists()

    loaded = store.get_skill("auth_flow", 1)
    assert loaded is not None
    assert loaded.skill_id == v1.skill_id
    assert loaded.status == SkillStatus.CANDIDATE


# ─── 3. Promotion to Active Pointer ──────────────────────────────────────────

def test_skill_promotion_updates_active_pointer(temp_workspace):
    store = SkillStore(temp_workspace)
    v1 = _sample_skill("payment_router", 1, SkillStatus.CANDIDATE)
    store.save_version(v1)

    promoted = store.promote_version("payment_router", 1, reason="All canary tests passed")
    assert promoted.status == SkillStatus.ACTIVE
    assert promoted.provenance.promotion_reason == "All canary tests passed"

    # Active pointer should now exist
    active_path = temp_workspace / ".sync" / "skills" / "active" / "payment_router.json"
    assert active_path.exists()

    active_loaded = store.get_skill("payment_router")
    assert active_loaded is not None
    assert active_loaded.status == SkillStatus.ACTIVE
    assert active_loaded.version == 1


# ─── 4. Version Progression & Rollback ───────────────────────────────────────

def test_skill_version_progression_and_rollback(temp_workspace):
    store = SkillStore(temp_workspace)

    # 1. Promote v1
    v1 = _sample_skill("redis_cache", 1, SkillStatus.CANDIDATE)
    store.save_version(v1)
    store.promote_version("redis_cache", 1)

    # 2. Create and promote v2
    v2 = SkillRecord(
        skill_id=SkillRecord.mint_id("redis_cache", 2),
        name="redis_cache",
        version=2,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Updated redis caching strategy with cluster mode",
        applicability=v1.applicability,
        steps=(
            SkillStep(step_index=1, action="Verify cluster connection", tool="bash", command_template="redis-cli cluster info"),
        ),
        provenance=SkillProvenance(
            source_experience_ids=("EXP-1122334455667788", "EXP-9988776655443322"),
            created_at="2026-09-01T13:00:00+00:00",
            author_agent="claude",
            previous_version_id=v1.skill_id,
        ),
    )
    store.save_version(v2)
    store.promote_version("redis_cache", 2)

    active_v2 = store.get_skill("redis_cache")
    assert active_v2.version == 2
    assert active_v2.steps[0].action == "Verify cluster connection"

    # 3. Rollback to v1
    rolled_back_v3 = store.rollback_version("redis_cache", target_version=1, reason="Cluster mode regression detected")
    assert rolled_back_v3.version == 3
    assert rolled_back_v3.status == SkillStatus.ACTIVE
    assert rolled_back_v3.provenance.previous_version_id == v2.skill_id
    assert "Rollback to v1" in rolled_back_v3.provenance.promotion_reason
    assert rolled_back_v3.steps[0].action == "Check database migration status"

    # Active pointer now points to v3
    active_now = store.get_skill("redis_cache")
    assert active_now.version == 3


# ─── 5. Deprecation & Lifecycle Filtering ────────────────────────────────────

def test_skill_deprecation_and_listing(temp_workspace):
    store = SkillStore(temp_workspace)
    s1 = _sample_skill("skill_one", 1, SkillStatus.ACTIVE)
    s2 = _sample_skill("skill_two", 1, SkillStatus.CANDIDATE)
    store.save_version(s1)
    store.save_version(s2)

    active_list = store.list_skills(active_only=True)
    assert len(active_list) == 1
    assert active_list[0].name == "skill_one"

    # Deprecate skill_one
    dep = store.deprecate_skill("skill_one", reason="Replaced by new architecture")
    assert dep.status == SkillStatus.DEPRECATED
    assert not (temp_workspace / ".sync" / "skills" / "active" / "skill_one.json").exists()

    stats = store.stats()
    assert stats["deprecated"] == 1
    assert stats["active_skills"] == 0
    assert stats["candidates"] == 1


# ─── 6. Skill CLI Commands ───────────────────────────────────────────────────

def test_skill_cli_commands(temp_workspace):
    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Create skill via CLI
    res_create = runner.invoke(
        cli,
        [
            "skill",
            "create",
            "cli-test-skill",
            "-d",
            "Automated payment processor verification",
            "-r",
            "medium",
            "-a",
            "verify_api:bash:curl https://api.internal/health",
            "-p",
            str(temp_workspace),
        ],
    )
    assert res_create.exit_code == 0
    assert "Created candidate skill 'cli-test-skill' v1" in res_create.output

    # 2. List skills
    res_list = runner.invoke(cli, ["skill", "list", "-p", str(temp_workspace)])
    assert res_list.exit_code == 0
    assert "cli-test-skill" in res_list.output
    assert "CANDIDATE" in res_list.output

    # 3. Show skill
    res_show = runner.invoke(cli, ["skill", "show", "cli-test-skill", "-p", str(temp_workspace)])
    assert res_show.exit_code == 0
    assert "Automated payment processor verification" in res_show.output
    assert "verify_api" in res_show.output

    # 4. Promote skill
    res_promote = runner.invoke(cli, ["skill", "promote", "cli-test-skill", "-p", str(temp_workspace)])
    assert res_promote.exit_code == 0
    assert "Promoted skill 'cli-test-skill' v1" in res_promote.output

    # 5. Rollback skill
    res_rollback = runner.invoke(cli, ["skill", "rollback", "cli-test-skill", "--to-version", "1", "-p", str(temp_workspace)])
    assert res_rollback.exit_code == 0
    assert "Rolled back 'cli-test-skill' to v1" in res_rollback.output

    # 6. Deprecate skill
    res_dep = runner.invoke(cli, ["skill", "deprecate", "cli-test-skill", "-p", str(temp_workspace)])
    assert res_dep.exit_code == 0
    assert "DEPRECATED" in res_dep.output

    # 7. Stats
    res_stats = runner.invoke(cli, ["skill", "stats", "-p", str(temp_workspace)])
    assert res_stats.exit_code == 0
    assert "Distinct Skills" in res_stats.output
    assert "Total Version Manifests" in res_stats.output
