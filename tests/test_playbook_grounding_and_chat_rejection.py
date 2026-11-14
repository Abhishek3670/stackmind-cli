"""Tests for Lead Architect Playbook Grounding and Chat Rejection.

Validates:
1. Writing conversational chat/greetings to PLAN.md is rejected with PermissionError.
2. compile_worker_task_contract injects architect_playbook_instructions for WO-000 / architect role.
3. repair_plan_structure infers frontend milestones (Agent: gemini) when UI keywords are present.
4. determine_assigned_agent grounds generic milestones in project_goal keywords.
5. decompose_milestone extracts project_goal from WO-000.yaml on disk to prevent blind backend assignment.
"""

from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from validators.harness.plan import (
    repair_plan_structure,
    validate_plan_structure,
    PLAN_GENERATION_INSTRUCTIONS,
    PlanMilestone,
)
from validators.harness.task_contract import (
    compile_worker_task_contract,
    render_worker_system,
)
from validators.kernel.daemon.authoring import (
    determine_assigned_agent,
    decompose_milestone,
)


def test_repair_plan_structure_infers_frontend_milestone_on_ui_keywords():
    """When a plan has no milestones but mentions web/UI, repair_plan_structure must assign gemini."""
    raw = """# Project Plan: Animated Portfolio
## Current Architecture
A single page animated portfolio webpage with Tailwind CSS and React.
"""
    repaired = repair_plan_structure(raw)
    valid, errors = validate_plan_structure(repaired)
    assert valid, f"Repaired plan invalid: {errors}"
    assert "(Agent: gemini)" in repaired
    assert "Frontend" in repaired


def test_determine_assigned_agent_grounds_generic_milestone_in_project_goal():
    """A generic 'Core Implementation' milestone must route to gemini if project_goal specifies UI/webpage."""
    agent, role = determine_assigned_agent(
        title="Core Implementation",
        tasks=[],
        agent_hint=None,
        project_goal='Build a login webpage with a "welcome Abhishek" animated landing page upon successful login',
    )
    assert agent == "gemini"
    assert role == "frontend"


def test_determine_assigned_agent_defaults_to_codex_for_backend_goal():
    """A generic 'Core Implementation' milestone routes to codex if project_goal is backend/API."""
    agent, role = determine_assigned_agent(
        title="Core Implementation",
        tasks=[],
        agent_hint=None,
        project_goal="Build a high performance rate limiter microservice with Redis",
    )
    assert agent == "codex"
    assert role == "backend"


def test_decompose_milestone_reads_wo000_goal_from_disk(tmp_path: Path):
    """decompose_milestone automatically inspects WO-000.yaml on disk to resolve goal context."""
    sync_wo = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    sync_wo.mkdir(parents=True)
    wo_000 = {
        "id": "WO-000",
        "title": "System architecture and execution plan",
        "status": "COMPLETED",
        "description": "Build an editorial corporate portfolio website with React and Vite",
    }
    (sync_wo / "WO-000.yaml").write_text(yaml.safe_dump(wo_000), encoding="utf-8")

    milestone = PlanMilestone(
        id="M1",
        title="Core Implementation",
        status="PENDING",
        tasks=[],
    )
    wos, last_id = decompose_milestone(milestone, start_idx=1, project_path=tmp_path)
    assert len(wos) >= 1
    child_wo = wos[0]
    # Should assign to gemini/frontend, NOT codex/backend
    assert child_wo["assigned_agents"] == ["gemini"]
    assert child_wo["deliverable"]["type"] == "code"


def test_task_contract_injects_architect_playbook_instructions_for_wo000(tmp_path: Path):
    """compile_worker_task_contract injects architect_playbook_instructions when deliverable is PLAN.md."""
    class MockTask:
        agent_id = "claude"
        role = "architecture"
        work_order_id = "WO-000"
        deliverable_path = ".sync/playbook/PLAN.md"

    wo_data = {
        "id": "WO-000",
        "deliverable": {"path": ".sync/playbook/PLAN.md", "type": "doc"},
        "assigned_agents": ["claude"],
    }
    contract_data = {
        "agent_id": "claude",
        "identity": {"role": "architecture"},
        "scope": {"allow": [{"module": ".sync/playbook/**"}]},
    }
    contract = compile_worker_task_contract(MockTask(), wo_data, contract_data, tmp_path)
    assert "architect_playbook_instructions" in contract
    system_text = render_worker_system(contract)
    assert "LEAD ARCHITECT PLAYBOOK DIRECTIVE (MANDATORY)" in system_text
    assert "DESIGN.md" in system_text
    assert "(Agent: gemini)" in system_text


def test_write_file_rejects_conversational_chat_to_plan_md(tmp_path: Path):
    """ToolGateway.write_file rejects conversational greeting text written to PLAN.md."""
    from validators.kernel import (
        AgentSession, ContractNormalizer,
        OperationJournal, RuntimeBoundary, ToolGateway,
        ScratchWorkspace,
    )
    from validators.kernel.identity import get_role_policy

    contract = ContractNormalizer.normalize({
        "agent": "claude",
        "wo": "WO-000",
        "scope": {"allowed": ["**"], "denied": [], "write": "read-write"},
        "budget": {"max_tokens": 10000, "max_files": 15},
    })
    session = AgentSession("claude", "test-provider", str(tmp_path), session_id="s1")
    attempt = session.create_attempt(contract, attempt_id="a1")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = get_role_policy("claude")
    workspace = ScratchWorkspace(authoritative_root=tmp_path, root=tmp_path, attempt_id="a1")
    gateway = ToolGateway(
        boundary=boundary,
        policy=policy,
        contract=attempt.contract,
        workspace=workspace,
        sandbox=None,
        session_id="s1",
        attempt_id="a1",
        actor_id="claude",
        provider_id="test-provider",
    )

    chat_text = """Hey! 👋

I'm Claude, the Lead Architect here in your workspace. Good to meet you.

Before we dive into anything, I'd love to hear what's on your mind. Some directions we could go:
- Building something new
- Extending an existing project

What would you like to build or work on?
"""
    with pytest.raises(PermissionError, match="Cannot write conversational greeting"):
        gateway.write_file(".sync/playbook/PLAN.md", chat_text)


def test_completion_fallback_does_not_synthesize_plan_for_adhoc_greeting(tmp_path: Path):
    """Verify _extract_unstructured_fallback does not write PLAN.md for adhoc conversational greetings."""
    from unittest.mock import MagicMock
    from validators.harness.runner.completion import _CompletionMixin
    from validators.harness.runner.models import HarnessTask, LLMRequest

    mixin = _CompletionMixin()
    mixin.project_path = tmp_path
    mixin.sync_path = tmp_path / ".sync"
    mixin.agent = "claude"

    task = HarnessTask(
        identifier="adhoc:hey",
        kind="adhoc",
        work_order_id=None,
        title="hey",
        deliverable_path=None,
        deliverable_type="doc",
        path=mixin.sync_path / "work-orders" / "ACTIVE" / "adhoc.yaml",
        body="",
        query="",
    )

    req = LLMRequest(
        agent="claude",
        session_count=1,
        task=task,
        context=MagicMock(),
        retrieval=MagicMock(),
    )

    chat_text = "Hello! I'm Claude, the Lead Architect. I'd love to hear about your project — what would you like to build or discuss today?"
    payload, written_files = mixin._extract_unstructured_fallback(
        request=req,
        raw_text=chat_text,
        tool_runtime=None,
        written_files=[],
    )

    assert payload.get("status") == "completed"
    assert not (tmp_path / ".sync" / "playbook" / "PLAN.md").exists()
    assert written_files == []
    assert payload.get("modified_files") == []
    assert "Hello! I'm Claude" in payload.get("summary", "")


def test_repair_plan_structure_does_not_repair_conversational_greetings():
    """Verify repair_plan_structure leaves pure conversational greetings intact and does not inject fake plans."""
    chat_text = "Hello! I'm Claude, the Lead Architect. I'd love to hear about your project — what would you like to build or discuss today?"
    repaired = repair_plan_structure(chat_text)
    assert repaired == chat_text
    assert "Milestone 1" not in repaired
    assert "# Project Plan" not in repaired

