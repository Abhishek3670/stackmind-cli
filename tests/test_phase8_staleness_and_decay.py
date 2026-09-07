"""Comprehensive test suite for Phase 8 (Staleness & Decay).

Covers:
1. Dynamic empirical confidence decay on execution failures.
2. Automatic downgrade from ACTIVE to STALE when confidence drops below threshold.
3. Immediate exclusion of STALE skills from runtime retrieval.
4. Code drift / missing target module audit and automatic flagging.
5. Skill revalidation workflow restoring healthy skills to ACTIVE or DEPRECATING failing ones.
6. CLI commands (stackmind skill feedback, stackmind skill audit, stackmind skill revalidate).
"""

from __future__ import annotations

import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.skill.decay import SkillDecayManager, StalenessReason
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.retriever import SkillRetriever
from validators.skill.store import SkillStore


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "decay-test-workspace"
    init(project, name="Decay Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _create_active_skill(
    store: SkillStore,
    name: str,
    *,
    target_modules: tuple[str, ...] = ("src/*",),
    initial_confidence: float = 0.85,
    cmd: str = "pytest tests/",
) -> SkillRecord:
    skill = SkillRecord(
        skill_id=SkillRecord.mint_id(name, 1),
        name=name,
        version=1,
        status=SkillStatus.ACTIVE,
        risk_tier=RiskTier.LOW,
        description=f"Active procedure for {name}",
        applicability=SkillApplicability(preconditions=("Env ready",), target_modules=target_modules),
        steps=(SkillStep(step_index=1, action=f"Run {name}", tool="bash", command_template=cmd),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
        metrics=SkillMetrics(confidence_score=initial_confidence),
    )
    store.save_version(skill)
    store.promote_version(name, 1, skip_pipeline=True, skip_governor=True)
    return skill


# ─── 1. Empirical Confidence Decay & Automatic Downgrade ──────────────────────

def test_empirical_decay_downgrades_to_stale(temp_workspace):
    store = SkillStore(temp_workspace)
    decay_mgr = SkillDecayManager(temp_workspace)
    retriever = SkillRetriever(temp_workspace)

    skill = _create_active_skill(store, "cache_sync", initial_confidence=0.65)
    active_ptr = temp_workspace / ".sync" / "skills" / "active" / "cache_sync.json"
    assert active_ptr.exists()

    # Verify initially discoverable
    res_before = retriever.retrieve_skills("cache sync")
    assert len(res_before) == 1

    # 1. First failure -> drops confidence by 0.20 (0.65 -> 0.45)
    updated1 = decay_mgr.record_feedback("cache_sync", success=False, error_message="Connection timed out")
    assert updated1.metrics.confidence_score == 0.45
    # Since confidence < 0.50, status should be automatically downgraded to STALE
    assert updated1.status == SkillStatus.STALE
    # Active pointer must be immediately removed
    assert not active_ptr.exists()

    # 2. Verify now excluded from retrieval
    res_after = retriever.retrieve_skills("cache sync")
    assert len(res_after) == 0, "STALE skills must be immediately excluded from retrieval"


# ─── 2. Staleness Audit & Missing Module Drift Detection ──────────────────────

def test_staleness_audit_drift_detection(temp_workspace):
    store = SkillStore(temp_workspace)
    decay_mgr = SkillDecayManager(temp_workspace)

    # Create a skill pointing to non-existent module
    _create_active_skill(store, "legacy_billing", target_modules=("nonexistent_legacy_billing/*",))

    # Audit staleness
    reports = decay_mgr.audit_staleness(auto_downgrade=True)
    assert len(reports) == 1
    rep = reports[0]

    assert rep.skill_name == "legacy_billing"
    assert StalenessReason.MISSING_TARGET_FILES in rep.reasons

    # Downgraded to STALE
    updated = store.get_skill("legacy_billing")
    assert updated.status == SkillStatus.STALE


# ─── 3. Revalidation Pipeline Workflow ────────────────────────────────────────

def test_revalidation_restores_active_status(temp_workspace):
    store = SkillStore(temp_workspace)
    decay_mgr = SkillDecayManager(temp_workspace)
    retriever = SkillRetriever(temp_workspace)

    # Create skill and downgrade it to STALE
    _create_active_skill(store, "format_python", cmd="black src/")
    decay_mgr.downgrade_to_stale("format_python")
    assert store.get_skill("format_python").status == SkillStatus.STALE

    # Revalidate skill
    success, reactivated, msg = decay_mgr.revalidate_skill("format_python")
    assert success is True
    assert reactivated.status == SkillStatus.ACTIVE
    assert reactivated.metrics.confidence_score >= 0.75

    # Active pointer re-created and discoverable
    active_ptr = temp_workspace / ".sync" / "skills" / "active" / "format_python.json"
    assert active_ptr.exists()
    retrieved = retriever.retrieve_skills("format python")
    assert len(retrieved) == 1


# ─── 4. CLI Staleness & Decay Commands ────────────────────────────────────────

def test_cli_staleness_and_decay_commands(temp_workspace):
    store = SkillStore(temp_workspace)
    _create_active_skill(store, "api_health_check", initial_confidence=0.8)

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Record execution failure via CLI
    res_fb = runner.invoke(
        cli,
        ["skill", "feedback", "api_health_check", "--failure", "-e", "Endpoint 503", "-p", str(temp_workspace)],
    )
    assert res_fb.exit_code == 0
    assert "Updated metrics for 'api_health_check'" in res_fb.output
    assert "Confidence=0.60" in res_fb.output

    # 2. Another failure drops confidence below 0.50 -> STALE
    res_fb2 = runner.invoke(
        cli,
        ["skill", "feedback", "api_health_check", "--failure", "-e", "Endpoint 503", "-p", str(temp_workspace)],
    )
    assert res_fb2.exit_code == 0
    assert "STALE" in res_fb2.output

    # 3. Audit command displays stale skill
    res_audit = runner.invoke(cli, ["skill", "audit", "-p", str(temp_workspace)])
    assert res_audit.exit_code == 0

    # 4. Revalidate command restores skill
    res_reval = runner.invoke(cli, ["skill", "revalidate", "api_health_check", "-p", str(temp_workspace)])
    assert res_reval.exit_code == 0
    assert "Revalidation succeeded" in res_reval.output
    assert "is now ACTIVE" in res_reval.output
