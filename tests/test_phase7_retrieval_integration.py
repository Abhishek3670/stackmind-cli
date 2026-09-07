"""Comprehensive test suite for Phase 7 (Retrieval Integration).

Covers:
1. SkillRetriever filters only ACTIVE skills (excluding CANDIDATE, DEPRECATED, etc.).
2. Query relevance ranking and precondition matching.
3. Contract Scope Boundary: Out-of-scope or denied module skills are strictly filtered out.
4. KnowledgeAPI.assemble_context() surfaces active procedural skills in ContextBundle.
5. Formatting of prompt-ready procedural guidance blocks.
6. CLI skill retrieve command.
"""

from __future__ import annotations

import shutil
from pathlib import Path
import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.contract import AgentContract
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
    project = tmp_path / "retrieval-test-workspace"
    init(project, name="Retrieval Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _save_and_activate_skill(
    store: SkillStore,
    name: str,
    *,
    status: SkillStatus = SkillStatus.ACTIVE,
    target_modules: tuple[str, ...] = ("src/*",),
    preconditions: tuple[str, ...] = ("Env ready",),
    steps: tuple[SkillStep, ...] = (SkillStep(step_index=1, action="Execute check", tool="bash", command_template="pytest"),),
    description: str = "Test procedure",
    risk: RiskTier = RiskTier.LOW,
    confidence: float = 0.9,
) -> SkillRecord:
    rec = SkillRecord(
        skill_id=SkillRecord.mint_id(name, 1),
        name=name,
        version=1,
        status=status,
        risk_tier=risk,
        description=description,
        applicability=SkillApplicability(preconditions=preconditions, target_modules=target_modules),
        steps=steps,
        provenance=SkillProvenance(source_experience_ids=(), created_at="2026-09-01T12:00:00Z", author_agent="claude"),
        metrics=SkillMetrics(confidence_score=confidence),
    )
    store.save_version(rec)
    if status == SkillStatus.ACTIVE:
        store.promote_version(name, 1, skip_pipeline=True, skip_governor=True)
    return rec


# ─── 1. Only ACTIVE Skills Retrieved ─────────────────────────────────────────

def test_retriever_filters_active_skills_only(temp_workspace):
    store = SkillStore(temp_workspace)
    retriever = SkillRetriever(temp_workspace)

    # 1. Candidate skill (not active)
    _save_and_activate_skill(store, "candidate_migrate", status=SkillStatus.CANDIDATE, description="database migration candidate")
    # 2. Active skill
    _save_and_activate_skill(store, "active_migrate", status=SkillStatus.ACTIVE, description="database migration active")

    results = retriever.retrieve_skills("database migration")
    assert len(results) == 1
    assert results[0].skill.name == "active_migrate"


# ─── 2. Query Relevance Ranking ──────────────────────────────────────────────

def test_query_relevance_ranking(temp_workspace):
    store = SkillStore(temp_workspace)
    retriever = SkillRetriever(temp_workspace)

    _save_and_activate_skill(
        store,
        "redis_tune",
        description="tune redis cache connection pool and maxclients",
        steps=(SkillStep(step_index=1, action="Set maxclients", tool="bash", command_template="redis-cli config set maxclients 10000"),),
        confidence=0.95,
    )
    _save_and_activate_skill(
        store,
        "auth_rotate",
        description="rotate jwt auth signing keys",
        steps=(SkillStep(step_index=1, action="Rotate secrets", tool="bash", command_template="kubectl create secret"),),
        confidence=0.85,
    )

    results = retriever.retrieve_skills("tune redis pool")
    assert len(results) >= 1
    assert results[0].skill.name == "redis_tune"
    assert "redis" in results[0].matched_terms


# ─── 3. Contract Scope Boundary Filtering ─────────────────────────────────────

def test_contract_scope_boundary_filtering(temp_workspace):
    store = SkillStore(temp_workspace)
    retriever = SkillRetriever(temp_workspace)

    # Auth skill (in auth/* module)
    _save_and_activate_skill(
        store,
        "auth_flow",
        target_modules=("auth/*",),
        description="manage authentication tokens and sessions",
    )
    # Billing skill (in billing/* module)
    _save_and_activate_skill(
        store,
        "billing_flow",
        target_modules=("billing/*",),
        description="manage billing subscriptions and invoices",
    )

    # Contract restricted to auth/*, explicitly denying billing/*
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-050",
        "identity": {"role": "backend"},
        "scope": {
            "allow": [{"module": "auth.*"}],
            "deny": [{"module": "billing.*"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 1000},
    }
    contracts_dir = temp_workspace / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract_file = contracts_dir / "WO-050.yaml"
    import yaml
    with open(contract_file, "w", encoding="utf-8") as f:
        yaml.dump(contract_data, f)

    contract = AgentContract.load(contract_file, temp_workspace)

    # 1. Query matching auth returns auth_flow
    results_auth = retriever.retrieve_skills("authentication tokens", contract=contract)
    assert len(results_auth) == 1
    assert results_auth[0].skill.name == "auth_flow"

    # 2. Query matching billing returns NOTHING because billing/* is denied by contract
    results_billing = retriever.retrieve_skills("billing invoices", contract=contract)
    assert len(results_billing) == 0, "Out-of-scope/denied skill must be strictly filtered out by contract boundary"


# ─── 4. KnowledgeAPI.assemble_context Integration ────────────────────────────

def test_knowledge_api_assemble_context_surfaces_skills(temp_workspace):
    store = SkillStore(temp_workspace)
    _save_and_activate_skill(
        store,
        "test_suite_runner",
        description="run pytest test suite with coverage report",
        steps=(SkillStep(step_index=1, action="Run tests", tool="bash", command_template="pytest --cov=src/"),),
        confidence=0.92,
    )

    api = KnowledgeAPI(temp_workspace)

    # 1. Context bundle with include_skills=True
    bundle = api.assemble_context("run pytest test suite", include_skills=True)
    assert "test_suite_runner" in bundle.text
    assert any(e.reason == "procedural_skill" for e in bundle.entries)

    # 2. Context bundle with include_skills=False
    bundle_no_skills = api.assemble_context("run pytest test suite", include_skills=False)
    assert not any(e.reason == "procedural_skill" for e in bundle_no_skills.entries)


# ─── 5. CLI Skill Retrieve Command ───────────────────────────────────────────

def test_cli_skill_retrieve_command(temp_workspace):
    store = SkillStore(temp_workspace)
    _save_and_activate_skill(
        store,
        "db_migration_apply",
        description="apply database schema migrations with alembic",
        steps=(SkillStep(step_index=1, action="Run alembic", tool="bash", command_template="alembic upgrade head"),),
    )

    runner = CliRunner(env={"COLUMNS": "160"})
    res = runner.invoke(cli, ["skill", "retrieve", "database schema migrations", "-p", str(temp_workspace)])
    assert res.exit_code == 0
    assert "Retrieved Procedural Skills" in res.output
    assert "db_migration_apply" in res.output
    assert "Execution Procedure:" in res.output
