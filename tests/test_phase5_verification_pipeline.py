"""Comprehensive test suite for Phase 5 (Verification Pipeline - Replay / Canary).

Covers:
1. Stage 1: Structural Verification (Schema, Step completeness, D025 compliance).
2. Stage 2: Historical Replay Verification (Fidelity alignment with source experience records).
3. Stage 3: Canary Verification (Sandbox dry-run, Boundary validation, Preconditions audit).
4. End-to-end VerificationPipeline and deterministic verification receipt emission.
5. Strict Promotion Gate: Failing any stage blocks promotion; all 3 must pass.
6. Verification CLI commands (stackmind skill test, stackmind skill promote).
"""

from __future__ import annotations

import json
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
from validators.verification.canary import CanaryVerifier
from validators.verification.pipeline import VerificationPipeline
from validators.verification.replay import ReplayVerifier
from validators.verification.structural import StructuralVerifier


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "verification-test-workspace"
    init(project, name="Verification Pipeline Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _create_source_experience(
    store: ExperienceStore,
    signature: str,
    timestamp: str,
    tools_and_commands: list[tuple[str, str]],
) -> ExperienceRecord:
    exp_id = ExperienceRecord.mint_id(signature, timestamp)
    actions = tuple(
        ExperienceAction(tool=tool, command_or_symbol=cmd, input_summary=cmd, timestamp=timestamp)
        for tool, cmd in tools_and_commands
    )
    observations = tuple(
        ExperienceObservation(output_summary="Success", exit_code=0, is_error=False)
        for _ in tools_and_commands
    )
    diff = WorkspaceDiff(modified=("src/db/router.py",))
    verification = ExperienceVerification(
        dimensions=VerificationDimensions(
            scope_verified=True,
            state_verified=True,
            code_verified=True,
            behavioral_verified=True,
            security_verified=True,
            outcome_verified=True,
        ),
        observed_diff=diff,
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
        trust_level=TrustLevel.LEARNING_ELIGIBLE,
        learning_eligible=True,
        outcome="completed",
        recorded_at=timestamp,
    )
    store.save_record(rec)
    return rec


# ─── 1. Stage 1: Structural Verification ─────────────────────────────────────

def test_structural_verification_valid_and_invalid(temp_workspace):
    # 1. Valid skill
    valid_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("valid_skill", 1),
        name="valid_skill",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Valid procedural skill",
        applicability=SkillApplicability(preconditions=("Env ready",), target_modules=("src/*",)),
        steps=(SkillStep(step_index=1, action="Run pytest", tool="bash", command_template="pytest tests/"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    res_valid = StructuralVerifier.verify(valid_skill, temp_workspace)
    assert res_valid.passed
    assert res_valid.score == 1.0

    # 2. D025 Destructive violation
    destructive_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("destructive_skill", 1),
        name="destructive_skill",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.HIGH,
        description="Destructive procedural skill",
        applicability=SkillApplicability(preconditions=("Env ready",), target_modules=("src/*",)),
        steps=(SkillStep(step_index=1, action="Wipe database", tool="bash", command_template="DROP DATABASE production"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    res_destructive = StructuralVerifier.verify(destructive_skill, temp_workspace)
    assert not res_destructive.passed
    assert any("D025" in m for m in res_destructive.messages)


# ─── 2. Stage 2: Historical Replay Verification ──────────────────────────────

def test_historical_replay_verification(temp_workspace):
    store = ExperienceStore(temp_workspace)
    exp1 = _create_source_experience(
        store,
        "migration:one",
        "2026-09-01T10:00:00Z",
        [("bash", "alembic current"), ("bash", "alembic upgrade head")],
    )
    exp2 = _create_source_experience(
        store,
        "migration:two",
        "2026-09-01T11:00:00Z",
        [("bash", "alembic current"), ("bash", "alembic upgrade head")],
    )

    # 1. Congruent candidate
    congruent_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("db_migrate", 1),
        name="db_migrate",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.MEDIUM,
        description="Apply forward migrations",
        applicability=SkillApplicability(preconditions=("DB up",), target_modules=("alembic/*",)),
        steps=(
            SkillStep(step_index=1, action="Check current", tool="bash", command_template="alembic current"),
            SkillStep(step_index=2, action="Upgrade head", tool="bash", command_template="alembic upgrade head"),
        ),
        provenance=SkillProvenance(
            source_experience_ids=(exp1.experience_id, exp2.experience_id),
            created_at="2026-09-01T12:00:00Z",
            author_agent="claude",
        ),
    )
    res_congruent = ReplayVerifier.verify(congruent_skill, temp_workspace)
    assert res_congruent.passed
    assert res_congruent.score >= 0.8

    # 2. Incongruent candidate
    incongruent_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("db_migrate_bad", 1),
        name="db_migrate_bad",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.MEDIUM,
        description="Apply forward migrations bad",
        applicability=SkillApplicability(preconditions=("DB up",), target_modules=("alembic/*",)),
        steps=(
            SkillStep(step_index=1, action="Different tool", tool="bash", command_template="kubectl delete pod --all"),
        ),
        provenance=SkillProvenance(
            source_experience_ids=(exp1.experience_id, exp2.experience_id),
            created_at="2026-09-01T12:00:00Z",
            author_agent="claude",
        ),
    )
    res_incongruent = ReplayVerifier.verify(incongruent_skill, temp_workspace)
    assert not res_incongruent.passed
    assert res_incongruent.score < 0.6


# ─── 3. Stage 3: Canary / Sandbox Verification ───────────────────────────────

def test_canary_verification_boundaries(temp_workspace):
    # 1. Valid canary boundary
    valid_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("auth_service", 1),
        name="auth_service",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Auth service verification",
        applicability=SkillApplicability(preconditions=("Service up",), target_modules=("auth/*", "src/*")),
        steps=(SkillStep(step_index=1, action="Verify auth router", tool="bash", command_template="pytest tests/test_auth.py"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    res_valid = CanaryVerifier.verify(valid_skill, temp_workspace)
    assert res_valid.passed

    # 2. Path traversal out-of-boundary
    traversal_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("traversal_skill", 1),
        name="traversal_skill",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.HIGH,
        description="Traversal skill",
        applicability=SkillApplicability(preconditions=("Service up",), target_modules=("../../etc/shadow",)),
        steps=(SkillStep(step_index=1, action="Inspect host", tool="bash", command_template="cat /etc/shadow"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    res_traversal = CanaryVerifier.verify(traversal_skill, temp_workspace)
    assert not res_traversal.passed
    assert any("target_modules" in m for m in res_traversal.messages)


# ─── 4. End-to-End Pipeline & Receipt Generation ─────────────────────────────

def test_verification_pipeline_receipt_generation(temp_workspace):
    skill = SkillRecord(
        skill_id=SkillRecord.mint_id("payment_gateway", 1),
        name="payment_gateway",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="Payment gateway sanity test",
        applicability=SkillApplicability(preconditions=("Stripe mock ready",), target_modules=("payments/*",)),
        steps=(SkillStep(step_index=1, action="Test payment processing", tool="bash", command_template="pytest tests/test_payments.py"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )

    pipeline_result = VerificationPipeline.verify_skill(skill, temp_workspace)
    assert pipeline_result.passed
    assert len(pipeline_result.stage_results) == 3
    assert pipeline_result.receipt_id.startswith("RECEIPT-")

    # Receipt file persisted on disk
    receipt_file = temp_workspace / ".sync" / "skills" / "receipts" / f"{pipeline_result.receipt_id}.json"
    assert receipt_file.exists()
    with open(receipt_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["receipt_id"] == pipeline_result.receipt_id
    assert data["passed"] is True


# ─── 5. Promotion Hard Gate Enforced in SkillStore ────────────────────────────

def test_promotion_hard_gate_enforced(temp_workspace):
    store = SkillStore(temp_workspace)

    # 1. Create a bad skill that violates D025
    bad_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("bad_migration", 1),
        name="bad_migration",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.HIGH,
        description="Bad migration skill",
        applicability=SkillApplicability(preconditions=("DB up",), target_modules=("db/*",)),
        steps=(SkillStep(step_index=1, action="Drop table", tool="bash", command_template="DROP TABLE users CASCADE"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    store.save_version(bad_skill)

    # Attempting to promote bad_skill MUST fail
    with pytest.raises(ValueError, match="failed verification pipeline"):
        store.promote_version("bad_migration", 1)

    # Status remains CANDIDATE and active pointer is not created
    assert store.get_skill("bad_migration").status == SkillStatus.CANDIDATE
    assert not (temp_workspace / ".sync" / "skills" / "active" / "bad_migration.json").exists()

    # 2. Create a good skill
    good_skill = SkillRecord(
        skill_id=SkillRecord.mint_id("good_migration", 1),
        name="good_migration",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.MEDIUM,
        description="Good migration skill",
        applicability=SkillApplicability(preconditions=("DB up",), target_modules=("alembic/*",)),
        steps=(SkillStep(step_index=1, action="Upgrade head", tool="bash", command_template="alembic upgrade head"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    store.save_version(good_skill)

    promoted = store.promote_version("good_migration", 1)
    assert promoted.status == SkillStatus.ACTIVE
    assert (temp_workspace / ".sync" / "skills" / "active" / "good_migration.json").exists()


# ─── 6. Verification CLI Commands ────────────────────────────────────────────

def test_verification_cli_commands(temp_workspace):
    store = SkillStore(temp_workspace)
    skill = SkillRecord(
        skill_id=SkillRecord.mint_id("cli_test_procedure", 1),
        name="cli_test_procedure",
        version=1,
        status=SkillStatus.CANDIDATE,
        risk_tier=RiskTier.LOW,
        description="CLI verification test procedure",
        applicability=SkillApplicability(preconditions=("Env initialized",), target_modules=("src/*",)),
        steps=(SkillStep(step_index=1, action="Run linting", tool="bash", command_template="flake8 src/"),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
    )
    store.save_version(skill)

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Run 'skill test'
    res_test = runner.invoke(cli, ["skill", "test", "cli_test_procedure", "-p", str(temp_workspace)])
    assert res_test.exit_code == 0
    assert "3-Stage Verification Pipeline" in res_test.output
    assert "Overall Pipeline Outcome: PASSED" in res_test.output

    # 2. Run 'skill promote'
    res_promote = runner.invoke(cli, ["skill", "promote", "cli_test_procedure", "-p", str(temp_workspace)])
    assert res_promote.exit_code == 0
    assert "Promoted skill 'cli_test_procedure' v1" in res_promote.output
