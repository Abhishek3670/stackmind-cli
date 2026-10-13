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
            "### Verified Procedural Skills Library\n"
            "No active procedural skills currently installed in ~/.stackmindcli/skills/global/ or .sync/skills/active/.\n"
            "(Skills can be imported into the workspace at any time via `:consume-skills <path>`).\n\n"
            "SKILL ASSIGNMENT SYNTAX:\n"
            "If procedural skills are requested or available:\n"
            "Append `(Skills: <skill_name>)` to milestone titles in PLAN.md alongside `(Agent: <agent>)`.\n"
        )

    lines: list[str] = [
        "### Verified Procedural Skills Library (Assign to Specialists)",
        "The following active procedural skills are available in the workspace library.",
        "As Senior Architect, you decide which specialist agents receive these skills to guide their implementation:\n",
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
        "The execution harness automatically binds the assigned procedural runbook to the specialist agent's prompt.",
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
) -> list[str]:
    """Select the best matching procedural skills for a given work order.
    
    Priority:
    1. Explicit skills assigned by the Architect in PLAN.md or Work Order.
    2. Intelligent matching based on role, deliverable path, and semantic keywords.
    """
    selected: list[str] = []
    seen: set[str] = set()

    active_skills = get_available_skills(project_path)
    skill_map = {s.name.lower(): s for s in active_skills}

    # 1. Process explicit skills chosen by Architect
    if explicit_skills:
        for sk in explicit_skills:
            clean = str(sk).strip().lower()
            if not clean:
                continue
            # If skill name exists in store or is a valid slug, preserve Architect intent
            if clean in skill_map:
                if clean not in seen:
                    selected.append(clean)
                    seen.add(clean)
            else:
                # If name has underscores/hyphens variation
                norm = clean.replace("-", "_")
                alt = clean.replace("_", "-")
                if norm in skill_map and norm not in seen:
                    selected.append(norm)
                    seen.add(norm)
                elif alt in skill_map and alt not in seen:
                    selected.append(alt)
                    seen.add(alt)
                elif clean not in seen:
                    # Still keep explicit Architect choice even if not loaded yet
                    selected.append(clean)
                    seen.add(clean)

    # 2. Automated matching if none explicitly designated
    if not selected:
        combined = f"{title} {description}".lower()
        role_norm = role.lower()
        deliv = (deliverable_path or "").lower()

        for s in active_skills:
            s_name = s.name.lower()
            if s_name in seen:
                continue

            # Heuristic rule: Ponytail applies to code creation/implementation tasks
            # (standard library before dependencies, minimal code, YAGNI)
            if s_name == "ponytail":
                is_code = any(deliv.endswith(ext) for ext in (".py", ".ts", ".js", ".html", ".go", ".rs")) or deliv.startswith("src/")
                is_impl = any(kw in combined for kw in ("implement", "feature", "logic", "module", "service", "build", "refactor", "create", "api"))
                if (is_code or is_impl) and role_norm in ("backend", "frontend", "codex", "gemini"):
                    selected.append(s.name)
                    seen.add(s_name)
                    continue

            # Check target module match
            matched_mod = False
            for mod in s.applicability.target_modules:
                if mod == "*":
                    continue
                clean_mod = mod.rstrip("*").rstrip("/")
                if clean_mod and (clean_mod in deliv or clean_mod in combined):
                    matched_mod = True
                    break

            # Keyword matching against skill description and name
            skill_tokens = set(re.findall(r"\w+", f"{s.name} {s.description}".lower()))
            # Remove very common words
            stop_words = {"the", "and", "for", "with", "that", "this", "from", "into", "skill", "procedure"}
            meaningful_tokens = {t for t in skill_tokens if len(t) > 3 and t not in stop_words}
            
            task_tokens = set(re.findall(r"\w+", combined))
            overlap = meaningful_tokens.intersection(task_tokens)

            if matched_mod or len(overlap) >= 2:
                selected.append(s.name)
                seen.add(s_name)

    return selected


__all__ = [
    "format_skills_for_architect_prompt",
    "get_available_skills",
    "select_skills_for_task",
]
