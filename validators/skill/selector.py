"""Architect Skill Selection and Recommendation Engine for StackMind.

Enables the Lead Architect (Claude) to discover, evaluate, and assign verified
procedural skills from the central (~/.stackmindcli) and local (.sync/skills)
libraries to specialist downstream agents (Codex, Gemini, Gemma, Local-LLM).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from validators.skill.models import RiskTier, SkillRecord, SkillStatus
from validators.skill.store import SkillStore


def get_available_skills(project_path: Path | str | None = None) -> list[SkillRecord]:
    """Retrieve all active verified procedural skills from global and local stores."""
    try:
        store = SkillStore(project_path)
        return store.list_skills(status=SkillStatus.ACTIVE, include_global=True)
    except Exception:
        return []


def format_skills_for_architect_prompt(project_path: Path | str | None = None) -> str:
    """Format active procedural skills into guidance instructions for Architect planning."""
    skills = get_available_skills(project_path)
    if not skills:
        return (
            "### Verified Procedural Skills Library (Governed by Laya Decision Engine)\n"
            "No active procedural skills currently installed in ~/.stackmindcli/skills/global/ or .sync/skills/active/.\n"
            "(Skills can be imported into the workspace at any time via `:consume-skills <path>`).\n\n"
            "SKILL ASSIGNMENT SYNTAX:\n"
            "If procedural skills are requested or available:\n"
            "Append `(Skills: <skill_name>)` to milestone titles in PLAN.md alongside `(Agent: <agent>)`.\n"
        )

    lines: list[str] = [
        "### Verified Procedural Skills Library (Governed by Laya Decision Engine)",
        "The following active procedural skills are available in the workspace library.",
        "Skill selection is autonomously decided by the Laya Decision Engine (LayaDecisionAdapter) based on task domain, specialist role, and procedural rules.",
        "As Senior Architect, you may optionally designate or recommend skills to guide implementation:\n",
    ]

    for s in skills:
        desc_clean = s.description.strip().replace("\n", " ")
        if len(desc_clean) > 140:
            desc_clean = desc_clean[:140] + "..."
        lines.append(f"- `{s.name}` (Risk: {s.risk_tier.value.upper()}, {len(s.steps)} steps): {desc_clean}")

    lines.extend([
        "\nSKILL ASSIGNMENT SYNTAX:",
        "1. In PLAN.md milestones: Append `(Skills: <skill_name>)` to each milestone title alongside `(Agent: <agent>)`.",
        "   Example: `- [ ] Milestone 1: Implement TokenBucket Rate Limiter (Agent: codex) (Skills: ponytail)`",
        "   Multiple skills can be comma-separated: `(Skills: ponytail, db_migration)`",
        "2. In Work Order YAML (.sync/work-orders/ACTIVE/WO-xxx.yaml): Declare the `skills` list:",
        "   ```yaml",
        "   assigned_agents:",
        "     - \"codex\"",
        "   skills:",
        "     - \"ponytail\"",
        "   ```",
        "The Laya Decision Engine automatically binds the assigned procedural runbook to the specialist agent's prompt.",
    ])

    return "\n".join(lines)


def select_skills_for_task(
    title: str,
    description: str = "",
    agent_id: str = "codex",
    role: str = "backend",
    deliverable_path: str | None = None,
    explicit_skills: Sequence[str] | None = None,
    project_path: Path | str | None = None,
    laya_adapter: Any | None = None,
) -> list[str]:
    """Select the best matching procedural skills for a given work order.
    
    Delegates skill selection decision to the Laya Decision Engine (LayaDecisionAdapter),
    preserving any explicit skill designations declared in PLAN.md or Work Orders.
    """
    from validators.routing.laya_adapter import LayaDecisionAdapter

    if laya_adapter is None:
        try:
            from validators.kernel.settings import load_settings
            settings = load_settings(project_path)
            mode = getattr(settings, "laya_mode", "active")
        except Exception:
            mode = "active"
        laya_adapter = LayaDecisionAdapter(mode=mode)

    active_skills = get_available_skills(project_path)
    return laya_adapter.decide_skills_for_task(
        title=title,
        description=description,
        role=role,
        deliverable_path=deliverable_path,
        available_skills=active_skills,
        explicit_skills=explicit_skills,
        project_path=project_path,
    )


__all__ = [
    "format_skills_for_architect_prompt",
    "get_available_skills",
    "select_skills_for_task",
]
