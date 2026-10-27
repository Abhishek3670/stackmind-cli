"""Tests for Role-Diversified Agent Skill Storage and Autonomous Specialist Acquisition.

Verifies:
1. Directory structure under ~/.stackmindcli/skills/agents-skills/<role>/ and local .sync/skills/agents-skills/<role>/.
2. Role segregation in SkillStore (role-specific skills isolated per agent, universal skills shared).
3. Autonomous skill acquisition by specialist agents (e.g. Frontend/Gemini) without Architect micro-management.
4. Decoupled PLAN.md generation and parsing without mandatory (Skills: ...) tags.
5. Preserved backward compatibility for explicit (Skills: ...) overrides.
6. Profile policies and token budgets (LOW, MEDIUM, HIGH) enforced across role-scoped skills.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from validators.kernel.home import (
    STACKMIND_HOME_ENV,
    get_global_agent_skills_dir,
    get_global_skills_dir,
    get_stackmind_home,
)
from validators.kernel.profile import ExecutionProfile
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
from validators.skill.selector import (
    format_skills_for_architect_prompt,
    get_available_skills,
    select_skills_for_task,
)
from validators.skill.store import SkillStore
from validators.harness.plan import (
    PLAN_GENERATION_INSTRUCTIONS,
    parse_plan,
    validate_plan_structure,
)


def _make_skill(
    name: str,
    description: str,
    role: str | None = None,
    target_modules: tuple[str, ...] = ("*",),
    confidence: float = 0.9,
) -> SkillRecord:
    return SkillRecord(
        skill_id=SkillRecord.mint_id(name, 1),
        name=name,
        version=1,
        status=SkillStatus.ACTIVE,
        risk_tier=RiskTier.LOW,
        description=description,
        applicability=SkillApplicability(
            preconditions=("Initialized workspace",),
            target_modules=target_modules,
        ),
        steps=(
            SkillStep(
                step_index=1,
                action=f"Execute {name} procedure",
                tool="bash",
                command_template=f"echo run {name}",
                expected_outcome="Success",
            ),
        ),
        provenance=SkillProvenance(
            source_experience_ids=(),
            created_at="2026-10-27T00:00:00Z",
            author_agent="architect",
        ),
        metrics=SkillMetrics(
            confidence_score=confidence,
            success_count=10,
            failure_count=0,
            success_rate=1.0,
        ),
        role=role,
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "central_home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(STACKMIND_HOME_ENV, str(home_dir))
    return home_dir


def test_home_layout_scaffolds_agent_skills_directories(isolated_home: Path):
    """Verify that ~/.stackmindcli scaffolds agent-skills directories for all standard roles."""
    home = get_stackmind_home()
    assert (home / "skills" / "global").is_dir()
    assert (home / "skills" / "agents-skills").is_dir()

    for role in ("frontend", "backend", "qa", "gitops", "architect"):
        role_dir = get_global_agent_skills_dir(role)
        assert role_dir.is_dir()
        assert role_dir == home / "skills" / "agents-skills" / role


def test_skill_store_role_segregation(isolated_home: Path, tmp_path: Path):
    """Verify that SkillStore segregates skills by role while sharing universal skills."""
    store = SkillStore(tmp_path)

    # 1. Frontend-specific skill
    frontend_skill = _make_skill(
        name="responsive-grid",
        description="CSS flexbox and mobile-responsive layout design guidelines",
        role="frontend",
    )
    store.save_agent_skill(frontend_skill, role="frontend", is_global=True)

    # 2. Backend-specific skill
    backend_skill = _make_skill(
        name="sql-indexing",
        description="Database indexing and query execution plan optimization",
        role="backend",
    )
    store.save_agent_skill(backend_skill, role="backend", is_global=True)

    # 3. Universal cross-cutting skill (shared by all roles)
    universal_skill = _make_skill(
        name="ponytail",
        description="Write the simplest, minimal code possible. Standard library first.",
        role=None,
    )
    store.save_global_skill(universal_skill)

    # Query for frontend role
    fe_skills = store.list_skills(role="frontend")
    fe_names = [s.name for s in fe_skills]
    assert "responsive-grid" in fe_names
    assert "ponytail" in fe_names
    assert "sql-indexing" not in fe_names, "Backend skill must not leak into frontend library!"

    # Query for backend role
    be_skills = store.list_skills(role="backend")
    be_names = [s.name for s in be_skills]
    assert "sql-indexing" in be_names
    assert "ponytail" in be_names
    assert "responsive-grid" not in be_names, "Frontend skill must not leak into backend library!"

    # Query without role (unscoped) returns all skills
    all_skills = store.list_skills()
    all_names = [s.name for s in all_skills]
    assert "responsive-grid" in all_names
    assert "sql-indexing" in all_names
    assert "ponytail" in all_names


def test_autonomous_skill_retrieval_by_role_in_context_assembly(isolated_home: Path, tmp_path: Path):
    """Verify that specialist workers autonomously acquire skills relevant to their role and work order."""
    store = SkillStore(tmp_path)

    # Seed frontend and backend skills
    fe_skill = _make_skill(
        name="responsive-ui",
        description="Responsive HTML5 layout and mobile navigation bar styling",
        role="frontend",
    )
    store.save_agent_skill(fe_skill, role="frontend", is_global=True)

    be_skill = _make_skill(
        name="rate-limiter",
        description="Token bucket rate limiter backend middleware",
        role="backend",
    )
    store.save_agent_skill(be_skill, role="backend", is_global=True)

    # Contract for Frontend Specialist (Gemini)
    fe_contract = AgentContract({
        "agent_id": "gemini",
        "work_order": "WO-201",
        "identity": {"role": "frontend", "reports_to": "claude"},
        "scope": {
            "allow": [{"module": "assets/**"}, {"module": "index.html"}],
            "deny": [{"module": ".git/**"}],
        },
        "budget": {"max_tokens": 10000, "max_cost_usd": 1.0},
    })

    api = KnowledgeAPI(tmp_path)

    # Frontend worker runs assemble_context WITHOUT explicit skills assigned by architect
    bundle = api.assemble_context(
        "create responsive mobile navigation bar and layout",
        contract=fe_contract,
        profile=ExecutionProfile.MEDIUM,
        skills=None,
    )

    injected_node_ids = [entry.node_id for entry in bundle.entries]
    assert fe_skill.skill_id in injected_node_ids, "Frontend specialist must autonomously acquire responsive-ui!"
    assert be_skill.skill_id not in injected_node_ids, "Backend skill must not be injected for frontend agent!"

    # Verify diagnostics record the role scoping
    diag = bundle.skill_diagnostics
    assert diag is not None
    assert diag["injected_count"] >= 1


def test_plan_milestone_without_skills_and_autonomous_worker(isolated_home: Path, tmp_path: Path):
    """Verify that PLAN.md without (Skills: ...) is fully valid and specialist agent autonomously acquires skills."""
    plan_text = """# Project Plan: E-Commerce Storefront

## Current Architecture
The system consists of a static web frontend and a Python REST backend.

## Milestones & Roadmap
- [ ] Milestone 1: Create Responsive Product Catalog (Agent: gemini)
  - [ ] Task 1.1: Build product grid in index.html
  - [ ] Task 1.2: Add mobile responsive CSS
- [ ] Milestone 2: Implement Cart Storage (Agent: codex)
  - [ ] Task 2.1: Implement session cart handler
"""

    # 1. Structural validation of PLAN.md must pass cleanly without (Skills: ...) tags
    is_valid, errors = validate_plan_structure(plan_text)
    assert is_valid, f"PLAN.md should be valid without (Skills: ...)! Errors: {errors}"

    # 2. Parsing milestones preserves agent and allows empty skills
    parsed = parse_plan(plan_text, project_path=tmp_path)
    assert len(parsed.milestones) == 2
    assert parsed.milestones[0].agent == "gemini"
    assert parsed.milestones[0].skills == []
    assert parsed.milestones[1].agent == "codex"
    assert parsed.milestones[1].skills == []

    # 3. Instructions given to Claude explicitly state autonomous acquisition
    assert "Specialist agents autonomously acquire optimum procedural skills" in PLAN_GENERATION_INSTRUCTIONS


def test_explicit_skill_override_still_supported(isolated_home: Path, tmp_path: Path):
    """Verify that explicit (Skills: ...) annotation in PLAN.md is still respected as an override."""
    plan_text = """# Project Plan: Infrastructure Hardening

## Architecture
Core CLI system components.

## Milestones & Roadmap
- [ ] Milestone 1: Ultra Minimalist Parser (Agent: codex) (Skills: ponytail)
  - [ ] Task 1.1: Refactor parser with zero dependencies
"""
    parsed = parse_plan(plan_text, project_path=tmp_path)
    assert len(parsed.milestones) == 1
    assert parsed.milestones[0].agent == "codex"
    assert parsed.milestones[0].skills == ["ponytail"], "Explicit (Skills: ponytail) override must be preserved!"


def test_execution_profile_limits_enforced_on_agent_skills(isolated_home: Path, tmp_path: Path):
    """Verify that execution profile policies (Low=0, Medium=2, High=5) are strictly enforced."""
    store = SkillStore(tmp_path)

    # Register 6 frontend skills
    for i in range(1, 7):
        s = _make_skill(
            name=f"frontend-guide-{i}",
            description=f"UI design pattern and component guide number {i} for web frontend",
            role="frontend",
        )
        store.save_agent_skill(s, role="frontend", is_global=True)

    fe_contract = AgentContract({
        "agent_id": "gemini",
        "work_order": "WO-301",
        "identity": {"role": "frontend", "reports_to": "claude"},
        "scope": {"allow": [{"module": "*"}], "deny": []},
        "budget": {"max_tokens": 10000, "max_cost_usd": 1.0},
    })

    retriever = SkillRetriever(tmp_path)

    # 1. Low profile -> 0 skills
    _, diag_low = retriever.retrieve_skills_with_diagnostics(
        "frontend design pattern component guide",
        contract=fe_contract,
        profile=ExecutionProfile.LOW,
    )
    assert diag_low.injected_count == 0

    # 2. Medium profile -> capped at 2 skills
    results_med, diag_med = retriever.retrieve_skills_with_diagnostics(
        "frontend design pattern component guide",
        contract=fe_contract,
        profile=ExecutionProfile.MEDIUM,
    )
    assert diag_med.injected_count <= 2
    assert len(results_med) <= 2

    # 3. High profile -> capped at 5 skills
    results_high, diag_high = retriever.retrieve_skills_with_diagnostics(
        "frontend design pattern component guide",
        contract=fe_contract,
        profile=ExecutionProfile.HIGH,
    )
    assert diag_high.injected_count <= 5
    assert len(results_high) <= 5
