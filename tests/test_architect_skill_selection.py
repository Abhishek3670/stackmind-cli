"""Tests for Architect Skill Selection and Recommendation Engine (PLANv3).

Verifies that the Lead Architect (Claude) can discover active procedural skills,
assign them in PLAN.md milestones, author them into child Work Orders and Contracts,
and have specialist workers receive them in their task contracts and prompts.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from validators.harness.plan import parse_plan, PlanStructure
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.store import SkillStore
from validators.skill.selector import (
    get_available_skills,
    format_skills_for_architect_prompt,
    select_skills_for_task,
)
from validators.kernel.daemon.authoring import (
    build_child_work_order,
    build_child_contract,
    synthesize_child_work_orders,
)
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.task_contract import (
    compile_worker_task_contract,
    render_worker_system,
)
from validators.knowledge.contract import AgentContract
from validators.knowledge.api import KnowledgeAPI


def test_plan_parser_extracts_milestone_skills():
    """Verify parse_plan extracts (Skills: ...) annotations without corrupting milestone titles."""
    plan_md = """# Project Plan: Rate Limiter

## Current Architecture
Core token bucket implementation with in-memory state.

## Milestones & Roadmap
- [ ] Milestone 1: Implement TokenBucket Core (Agent: codex) (Skills: ponytail)
  - [ ] Write token bucket algorithm in src/bucket.py
  - [ ] Support replenish rate and burst capacity

- [ ] Milestone 2: Database Schema Migration (Agent: codex) (Skills: ponytail, db_migration)
  - [ ] Create migration scripts in db/migrations/001_init.sql

- [ ] Milestone 3: Web Dashboard (Agent: gemini) (skills: ponytail)
  - [ ] Create UI components in src/dashboard.html

- [ ] Milestone 4: End-to-End QA Validation (Agent: gemma)
  - [ ] Write integration tests in tests/test_bucket.py
"""
    parsed = parse_plan(plan_md)
    assert len(parsed.milestones) == 4

    m1 = parsed.milestones[0]
    assert m1.title == "Implement TokenBucket Core"
    assert m1.agent == "codex"
    assert m1.skills == ["ponytail"]

    m2 = parsed.milestones[1]
    assert m2.title == "Database Schema Migration"
    assert m2.agent == "codex"
    assert m2.skills == ["ponytail", "db_migration"]

    m3 = parsed.milestones[2]
    assert m3.title == "Web Dashboard"
    assert m3.agent == "gemini"
    assert m3.skills == ["ponytail"]

    m4 = parsed.milestones[3]
    assert m4.title == "End-to-End QA Validation"
    assert m4.agent == "gemma"
    assert m4.skills == []


def test_select_skills_preserves_explicit_and_falls_back(tmp_path: Path):
    """Verify select_skills_for_task prioritizes explicit skills and falls back appropriately."""
    # Explicit skills take priority
    selected = select_skills_for_task(
        title="Custom Task",
        description="Do something specific",
        agent_id="codex",
        role="backend",
        deliverable_path="src/custom.py",
        explicit_skills=["ponytail", "security_audit"],
        project_path=tmp_path,
    )
    assert "ponytail" in selected
    assert "security_audit" in selected


def test_format_skills_for_architect_prompt_structure():
    """Verify format_skills_for_architect_prompt returns actionable briefing for Claude."""
    text = format_skills_for_architect_prompt()
    assert "Procedural Skills Library" in text
    assert "(Skills: <skill_name>)" in text
    assert "PLAN.md" in text


def test_work_order_and_contract_schemas_accept_skills(tmp_path: Path):
    """Verify build_child_work_order and build_child_contract produce schema-valid artifacts with skills."""
    gate = AuthoringGate(project_root=tmp_path)

    wo = build_child_work_order(
        wo_id="WO-001",
        title="Implement Rate Limiter",
        description="Write bucket algorithm",
        assigned_agents=["codex"],
        dependencies=[],
        priority="P0",
        deliverable={"type": "code", "path": "src/bucket.py", "description": "Bucket code"},
        skills=["ponytail"],
    )
    assert wo["skills"] == ["ponytail"]

    contract = build_child_contract(
        wo_id="WO-001",
        agent_id="codex",
        role="backend",
        deliverable_path="src/bucket.py",
        skills=["ponytail"],
    )
    assert contract["skills"] == ["ponytail"]

    # Validate against AuthoringGate (which checks json schema with additionalProperties: false)
    wo_yaml = yaml.safe_dump(wo, sort_keys=False)
    contract_yaml = yaml.safe_dump(contract, sort_keys=False)

    wo_dec = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", wo_yaml, agent="architecture", project_root=tmp_path)
    assert wo_dec.passed, f"Work order failed schema: {wo_dec.summary}"

    contract_dec = gate.validate_artifact_content(".sync/contracts/WO-001.yaml", contract_yaml, agent="architecture", project_root=tmp_path)
    assert contract_dec.passed, f"Contract failed schema: {contract_dec.summary}"


def test_synthesize_child_work_orders_embeds_architect_skills(tmp_path: Path):
    """Verify synthesize_child_work_orders parses skills from plan and writes them to disk."""
    plan_content = """# Project Plan: Core Engine

## Current Architecture
Core engine architecture and UI layout.

## Milestones & Roadmap
- [ ] Milestone 1: Core Engine Logic (Agent: codex) (Skills: ponytail)
  - [ ] Implement engine in src/engine.py

- [ ] Milestone 2: Web UI (Agent: gemini) (Skills: ponytail)
  - [ ] Build UI in src/index.html
"""
    created_wos = synthesize_child_work_orders(tmp_path, plan_content)
    assert len(created_wos) == 2

    # Verify WO-001 on disk
    wo1_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
    contract1_file = tmp_path / ".sync" / "contracts" / "WO-001.yaml"
    assert wo1_file.is_file()
    assert contract1_file.is_file()

    wo1_data = yaml.safe_load(wo1_file.read_text(encoding="utf-8"))
    contract1_data = yaml.safe_load(contract1_file.read_text(encoding="utf-8"))

    assert wo1_data.get("skills") == ["ponytail"]
    assert contract1_data.get("skills") == ["ponytail"]


def test_compiled_task_contract_and_system_prompt_include_skills(tmp_path: Path):
    """Verify compile_worker_task_contract and render_worker_system surface assigned skills to workers."""
    wo_data = {
        "id": "WO-001",
        "title": "Implement Cache",
        "description": "Build in-memory cache",
        "skills": ["ponytail"],
    }
    contract_data = {
        "skills": ["ponytail"],
        "scope": {"allow": [{"module": "src/cache.py"}]},
        "budget": {"max_files_touched": 2},
    }

    class DummyTask:
        work_order_id = "WO-001"
        deliverable_path = "src/cache.py"
        title = "Implement Cache"
        body = "Build in-memory cache"

    compiled = compile_worker_task_contract(DummyTask(), wo_data, contract_data, tmp_path)
    assert compiled.get("assigned_skills") == ["ponytail"]

    rendered_system = render_worker_system(compiled)
    assert "The Architect has assigned procedural skills: ponytail" in rendered_system


def _make_test_skill(
    name: str = "ponytail",
    description: str = "Simplest solution that works.",
    applicability: SkillApplicability | None = None,
) -> SkillRecord:
    return SkillRecord(
        skill_id=SkillRecord.mint_id(name, 1),
        name=name,
        version=1,
        status=SkillStatus.ACTIVE,
        risk_tier=RiskTier.LOW,
        description=description,
        applicability=applicability if applicability is not None else SkillApplicability(),
        steps=(
            SkillStep(
                step_index=1,
                action="Keep it minimal and clean",
                tool="write_file",
            ),
        ),
        provenance=SkillProvenance(
            source_experience_ids=(),
            created_at="2026-10-13T00:00:00Z",
            author_agent="operator",
        ),
    )


def test_active_skills_in_store_prompt_and_auto_selection(tmp_path: Path):
    """Verify active skills are listed in Architect prompt and auto-matched for tasks."""
    store = SkillStore(tmp_path)
    skill = _make_test_skill(
        name="ponytail",
        description="Write the simplest, minimal code possible. Favor standard library over dependencies.",
    )
    store.save_version(skill)

    # 1. Verify prompt formatting includes the installed active skill
    prompt_text = format_skills_for_architect_prompt(tmp_path)
    assert "`ponytail`" in prompt_text
    assert "simplest, minimal code" in prompt_text
    assert "(Skills: ponytail)" in prompt_text

    # 2. Verify auto-selection picks ponytail for backend implementation tasks
    matched = select_skills_for_task(
        title="Implement authentication service",
        description="Create jwt auth handler",
        agent_id="codex",
        role="backend",
        deliverable_path="src/auth.py",
        project_path=tmp_path,
    )
    assert "ponytail" in matched


def test_knowledge_api_assemble_context_with_explicit_skills(tmp_path: Path):
    """Verify KnowledgeAPI.assemble_context injects Architect-assigned skills into context text."""
    store = SkillStore(tmp_path)
    skill = _make_test_skill(
        name="ponytail",
        description="Simplest solution that works.",
    )
    store.save_version(skill)

    # Assemble context passing explicit skills
    api = KnowledgeAPI(tmp_path)
    bundle = api.assemble_context(
        query="implement rate limiter",
        skills=["ponytail"],
    )
    assert bundle.text
    assert "ponytail" in bundle.text.lower()
    assert any("verified_procedural_skill" in e.why_retrieved for e in bundle.entries)


def test_skill_retrieval_with_contract_scope_and_universal_skills(tmp_path: Path):
    """Verify universal skills and architect-explicit skills are not blocked by contract scope checks."""
    from validators.knowledge.contract import AgentContract
    from validators.skill.retriever import SkillRetriever
    from validators.skill.models import SkillApplicability

    store = SkillStore(tmp_path)

    # 1. Universal skill with default target_modules ("*",)
    skill_universal = _make_test_skill(
        name="ponytail",
        description="Write the simplest, minimal code possible.",
    )
    store.save_version(skill_universal)

    # 2. Out-of-scope skill targeting db/**
    skill_db = _make_test_skill(
        name="db-wizard",
        description="PostgreSQL schema migration procedures.",
        applicability=SkillApplicability(target_modules=("db/**",)),
    )
    store.save_version(skill_db)

    # Contract restricted to src/**
    contract = AgentContract({
        "agent_id": "codex",
        "work_order": "WO-101",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "src/**"}, {"module": "*.py"}],
            "deny": [{"module": ".git/**"}, {"module": ".env"}],
        },
        "budget": {"max_tokens": 10000, "max_cost_usd": 1.0},
    })

    retriever = SkillRetriever(tmp_path)

    # Retrieve with explicit skill "ponytail" and contract
    results, diag = retriever.retrieve_skills_with_diagnostics(
        query="implement endpoint and minimal code",
        contract=contract,
        explicit_skills=["ponytail"],
    )

    injected_names = [r.skill.name for r in results]
    assert "ponytail" in injected_names, f"ponytail was not injected! Exclusions: {diag.exclusions}"

    excluded_reasons = {ex["skill_id"]: ex["reason"] for ex in diag.exclusions}
    assert excluded_reasons.get(skill_db.skill_id) == "contract_scope_denied"


def test_laya_decides_skills_for_clarity_and_implementation(tmp_path: Path):
    """Verify Laya decision engine autonomously assigns asd-ste100 and ponytail based on task intent."""
    from validators.routing.laya_adapter import LayaDecisionAdapter, LayaMode

    store = SkillStore(tmp_path)
    ste_skill = _make_test_skill(
        name="asd-ste100",
        description="Simplified Technical English for unambiguous agent instructions and prompts.",
    )
    store.save_version(ste_skill)

    ponytail_skill = _make_test_skill(
        name="ponytail",
        description="Write the simplest, minimal code possible.",
    )
    store.save_version(ponytail_skill)

    adapter = LayaDecisionAdapter(mode=LayaMode.ACTIVE)

    # 1. Task needing clear instructions/prompts -> Laya decides asd-ste100
    clarity_task = select_skills_for_task(
        title="Clarify instructions and prompt specification for downstream agents",
        description="Remove ambiguity and write controlled plain language rules",
        role="architect",
        deliverable_path="docs/SPEC.md",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "asd-ste100" in clarity_task

    # 2. Task implementing feature code -> Laya decides ponytail
    impl_task = select_skills_for_task(
        title="Implement user authentication API",
        description="Write JWT verification logic",
        role="backend",
        deliverable_path="src/auth.py",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "ponytail" in impl_task

    # 3. QA task with inter-agent coordination -> Laya selects asd-ste100
    qa_task = select_skills_for_task(
        title="QA Coordination and verification review",
        description="Coordinate verification handoff and write review report",
        role="qa",
        deliverable_path="tests/test_report.md",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "asd-ste100" in qa_task


def test_laya_decides_skills_for_arbitrary_new_skills(tmp_path: Path):
    """Verify Laya autonomously evaluates and selects newly added, custom procedural skills."""
    from validators.routing.laya_adapter import LayaDecisionAdapter, LayaMode

    store = SkillStore(tmp_path)

    # 1. New custom skill: biome-formatter
    biome_skill = _make_test_skill(
        name="biome-formatter",
        description="Fast code formatting and linting for web projects.",
    )
    store.save_version(biome_skill)

    # 2. New custom skill: prisma-migrate
    prisma_skill = _make_test_skill(
        name="prisma-migrate",
        description="Database schema migration and ORM model updates.",
        applicability=SkillApplicability(target_modules=("db/migrations/**", "prisma/**")),
    )
    store.save_version(prisma_skill)

    adapter = LayaDecisionAdapter(mode=LayaMode.ACTIVE)

    # Case A: Task mentioning tool and formatting intent -> selects biome-formatter
    task_biome = select_skills_for_task(
        title="Run biome to clean codebase",
        description="Fix lint errors across source files",
        role="backend",
        deliverable_path="src/index.ts",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "biome-formatter" in task_biome
    assert "prisma-migrate" not in task_biome

    # Case B: Task targeting database migration -> selects prisma-migrate
    task_prisma = select_skills_for_task(
        title="Apply database schema updates",
        description="Run migration for user table",
        role="backend",
        deliverable_path="db/migrations/001_create_users.sql",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "prisma-migrate" in task_prisma
    assert "biome-formatter" not in task_prisma

    # Case C: Unrelated task (e.g. documentation) -> selects neither
    task_doc = select_skills_for_task(
        title="Write release notes",
        description="Document version 1.0 features in markdown",
        role="architect",
        deliverable_path="docs/release.md",
        project_path=tmp_path,
        laya_adapter=adapter,
    )
    assert "biome-formatter" not in task_doc
    assert "prisma-migrate" not in task_doc



