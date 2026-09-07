"""Comprehensive test suite for Phase 6 (Risk-Tiered Promotion).

Covers:
1. LOW risk tier autonomous promotion.
2. MEDIUM risk tier lead agent vs worker governance.
3. HIGH / CRITICAL risk tier human-in-the-loop review approval requirement.
4. ApprovalReceipt generation, serialization, and disk persistence.
5. PromotionGovernor check and enforcement methods.
6. CLI commands (stackmind skill approve, stackmind skill auto-promote, stackmind skill promote --human).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.skill.governor import (
    ApprovalReceipt,
    HumanApprovalRequiredError,
    PromotionGovernor,
    RiskPromotionGateError,
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


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "governor-test-workspace"
    init(project, name="Risk Promotion Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _make_candidate(
    name: str,
    version: int,
    risk: RiskTier,
    tool: str = "bash",
    cmd: str = "pytest tests/",
) -> SkillRecord:
    return SkillRecord(
        skill_id=SkillRecord.mint_id(name, version),
        name=name,
        version=version,
        status=SkillStatus.CANDIDATE,
        risk_tier=risk,
        description=f"Candidate procedure for {name} ({risk.value})",
        applicability=SkillApplicability(preconditions=("Env ready",), target_modules=("src/*",)),
        steps=(SkillStep(step_index=1, action=f"Run {name}", tool=tool, command_template=cmd),),
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
        metrics=SkillMetrics(),
    )


# ─── 1. LOW Risk Autonomous Promotion ─────────────────────────────────────────

def test_low_risk_autonomous_promotion(temp_workspace):
    store = SkillStore(temp_workspace)
    low_skill = _make_candidate("lint_check", 1, RiskTier.LOW, cmd="flake8 src/")
    store.save_version(low_skill)

    # Low risk promotes autonomously even when called by worker agent
    promoted = store.promote_version("lint_check", 1, author_agent="codex")
    assert promoted.status == SkillStatus.ACTIVE
    assert promoted.risk_tier == RiskTier.LOW


# ─── 2. MEDIUM Risk Lead Agent vs Worker Governance ──────────────────────────

def test_medium_risk_governance(temp_workspace):
    store = SkillStore(temp_workspace)
    med_skill = _make_candidate("db_schema_patch", 1, RiskTier.MEDIUM, cmd="alembic upgrade head")
    store.save_version(med_skill)

    # 1. Worker agent (codex) without authorization fails
    with pytest.raises(RiskPromotionGateError, match="requires Claude/CEO authorization"):
        store.promote_version("db_schema_patch", 1, author_agent="codex")

    # 2. Lead architect (claude) or CEO succeeds
    promoted = store.promote_version("db_schema_patch", 1, author_agent="claude")
    assert promoted.status == SkillStatus.ACTIVE


# ─── 3. HIGH & CRITICAL Risk Human-In-The-Loop Gate ──────────────────────────

def test_high_risk_requires_human_approval(temp_workspace):
    store = SkillStore(temp_workspace)
    governor = PromotionGovernor(temp_workspace)
    high_skill = _make_candidate("rotate_auth_keys", 1, RiskTier.HIGH, cmd="kubectl apply -f secrets.yaml")
    store.save_version(high_skill)

    # 1. Lead agent (claude) and worker (codex) both fail without approval receipt
    with pytest.raises(HumanApprovalRequiredError, match="Mandatory Human or CEO review required"):
        store.promote_version("rotate_auth_keys", 1, author_agent="claude")

    with pytest.raises(HumanApprovalRequiredError, match="Mandatory Human or CEO review required"):
        store.promote_version("rotate_auth_keys", 1, author_agent="codex")

    # 2. Record human approval receipt
    receipt = governor.grant_approval(
        high_skill,
        approver="security-admin",
        rationale="Cryptographic key rotation audited and validated in staging",
    )
    assert receipt.approval_id.startswith("APPR-")

    # 3. Now promotion succeeds with the approval receipt on disk
    promoted = store.promote_version("rotate_auth_keys", 1, author_agent="claude")
    assert promoted.status == SkillStatus.ACTIVE


# ─── 4. Direct Human Operator Authorization ──────────────────────────────────

def test_direct_human_operator_promotion(temp_workspace):
    store = SkillStore(temp_workspace)
    crit_skill = _make_candidate("prod_deploy_sync", 1, RiskTier.CRITICAL, cmd="terraform apply -auto-approve")
    store.save_version(crit_skill)

    # Direct human promotion flag bypasses need for pre-existing receipt file
    promoted = store.promote_version("prod_deploy_sync", 1, is_human=True, author_agent="human")
    assert promoted.status == SkillStatus.ACTIVE
    assert promoted.risk_tier == RiskTier.CRITICAL


# ─── 5. CLI Approval and Auto-Promote Commands ────────────────────────────────

def test_cli_risk_promotion_commands(temp_workspace):
    store = SkillStore(temp_workspace)
    low_skill = _make_candidate("auto_fmt", 1, RiskTier.LOW, cmd="black src/")
    high_skill = _make_candidate("infra_reconfig", 1, RiskTier.HIGH, cmd="kubectl rollout restart deployment")
    store.save_version(low_skill)
    store.save_version(high_skill)

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Auto-promote with max-risk-tier=low
    res_auto = runner.invoke(cli, ["skill", "auto-promote", "--max-risk-tier", "low", "-p", str(temp_workspace)])
    assert res_auto.exit_code == 0
    assert "PROMOTED" in res_auto.output
    assert "auto_fmt" in res_auto.output
    assert "Skipping 'infra_reconfig'" in res_auto.output

    # 2. Approve high-risk skill via CLI
    res_appr = runner.invoke(
        cli,
        [
            "skill",
            "approve",
            "infra_reconfig",
            "-a",
            "ops-lead",
            "-r",
            "Reviewed rollback safeguards",
            "-p",
            str(temp_workspace),
        ],
    )
    assert res_appr.exit_code == 0
    assert "Granted review approval" in res_appr.output

    # 3. Promote approved high-risk skill
    res_prom = runner.invoke(cli, ["skill", "promote", "infra_reconfig", "-p", str(temp_workspace)])
    assert res_prom.exit_code == 0
    assert "Promoted skill 'infra_reconfig' v1" in res_prom.output
