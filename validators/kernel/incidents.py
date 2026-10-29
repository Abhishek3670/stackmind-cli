"""Incident recording and reflection ledger for StackMind CLI.

Maintains .sync/state/incidents.jsonl to enable agents to inspect and learn
from recent permission denials and policy boundaries instead of retrying dead-end actions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


INCIDENTS_RELATIVE_PATH = Path(".sync") / "state" / "incidents.jsonl"
DEFAULT_MAX_INCIDENTS = 50

ROLE_ALIASES: dict[str, set[str]] = {
    "architect": {"claude", "architect", "architecture"},
    "claude": {"claude", "architect", "architecture"},
    "architecture": {"claude", "architect", "architecture"},
    "backend": {"codex", "backend"},
    "codex": {"codex", "backend"},
    "frontend": {"gemini", "frontend"},
    "gemini": {"gemini", "frontend"},
    "qa": {"gemma", "qa", "quality"},
    "gemma": {"gemma", "qa", "quality"},
    "gitops": {"local-llm", "gitops", "release"},
    "local-llm": {"local-llm", "gitops", "release"},
}


def compute_actionable_remedy(
    role: str,
    tool: str,
    target: str,
    reason: str,
) -> str:
    """Compute a specific, actionable remedy tailored to the exact failure pattern."""
    clean_role = role.strip().lower()
    clean_target = (target or "").replace("\\", "/").strip("/")
    lower_target = clean_target.lower()
    lower_reason = (reason or "").lower()

    # 1. Flagged interpreter code execution (-c, -e, --eval)
    if any(k in lower_reason for k in ("code-string flag", "flagged interpreter", "denied at the agent boundary", "without a module name")):
        return (
            "Inline interpreter code flags ('-c', '-e', '--eval') are blocked by security policy. "
            "Write your verification or test code into a script file within your assigned scope (e.g. in 'server/' or 'tests/') "
            "and execute it via 'python <file_path>' or 'pytest <file_path>'."
        )

    # 2. Schema validation error
    if "authoring validation failed" in lower_reason or "schema error" in lower_reason:
        return (
            f"YAML authoring validation failed: {reason}. "
            "Correct the schema structure (remove unexpected fields or fix types) before retrying."
        )

    # 3. Work order direct access
    if ".sync/work-orders" in lower_target:
        if clean_role in ("claude", "architect", "architecture"):
            return "Inspect and manage work orders using the compiled board and governance tools, or update them via 'update_work_order'."
        return (
            "Direct access to '.sync/work-orders/' is restricted. Your active work order requirements, deliverables, "
            "and acceptance criteria are already provided in your Task Contract at the start of your turn. "
            "Deliver only to your declared deliverable path."
        )

    # 4. Contract direct access
    if ".sync/contracts" in lower_target:
        return (
            "Runtime contracts in '.sync/contracts/' are managed by the kernel. "
            "Your assigned scope and deliverables are already provided in your Task Contract."
        )

    # 5. Decisions or runtime state
    if ".sync/decisions" in lower_target or ".sync/runtime" in lower_target:
        return (
            "Runtime state in '.sync/' is managed by the kernel. "
            "Operate within your assigned workspace deliverable scope."
        )

    # 6. Architect specific restrictions
    if clean_role in ("claude", "architect", "architecture"):
        if tool in ("run_command", "bash", "execute_command"):
            return "Shell execution is blocked for Architect. Use 'read_file' to inspect files or 'list_directory' to explore paths. Delegate shell tasks to Backend (Codex) or QA (Gemma)."
        elif tool in ("write_file", "apply_patch", "edit_file", "patch_file"):
            return "You cannot directly write application files. Delegate this task to Frontend (Gemini) or Backend (Codex) via 'delegate_task' or by authoring a Work Order in .sync/work-orders/ACTIVE/."
        else:
            return "Action blocked by governance boundary. Consult the Permission Matrix."

    # 7. Frontend specialist restrictions
    if clean_role in ("gemini", "frontend"):
        if any(lower_target.startswith(p) for p in ("src/", "server/", "backend/")):
            return "Frontend specialist scope is restricted to UI/views (*.html, *.css, web/**, assets/**). Backend source files must be handled by Backend (Codex)."
        return "Frontend specialist scope is restricted to UI/views (*.html, *.css, assets/**). Do not write backend APIs or governance files."

    # 8. Backend specialist restrictions
    if clean_role in ("codex", "backend"):
        if any(lower_target.startswith(p) for p in ("web/", "assets/", "public/")) or lower_target.endswith((".html", ".css")):
            return "Backend specialist scope is restricted to backend components (src/**, server/**). Frontend views/assets must be handled by Frontend (Gemini)."
        return "Backend specialist scope is restricted to src/** and server components. Do not write frontend templates or governance files."

    # 9. QA specialist restrictions
    if clean_role in ("gemma", "qa"):
        return "QA specialist scope is restricted to test authoring (tests/**) and test execution. Do not modify production application source files."

    # 10. GitOps specialist restrictions
    if clean_role in ("local-llm", "gitops"):
        return "GitOps specialist scope is restricted to release manifests, documentation (VERSION.md, CHANGELOG.md, README.md), and packaging."

    return f"Operation '{tool}' on '{target}' is outside assigned scope. Consult your work order contract."


def record_permission_incident(
    project_path: Path | str | None,
    agent: str,
    role: str,
    tool: str,
    target: str,
    reason: str,
    remedy: str | None = None,
) -> dict[str, Any] | None:
    """Record a permission or boundary denial incident into .sync/state/incidents.jsonl."""
    if not project_path:
        return None

    base_path = Path(project_path).resolve()
    incidents_path = base_path / INCIDENTS_RELATIVE_PATH

    clean_role = role.strip().lower()
    if not remedy:
        remedy = compute_actionable_remedy(clean_role, tool, target, reason)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "role": clean_role,
        "tool": tool,
        "target": target,
        "reason": reason,
        "remedy": remedy,
    }

    try:
        incidents_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing records to enforce bounding
        existing_lines = (
            [l for l in incidents_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if incidents_path.is_file()
            else []
        )

        existing_lines.append(json.dumps(record, sort_keys=True))
        if len(existing_lines) > DEFAULT_MAX_INCIDENTS:
            existing_lines = existing_lines[-DEFAULT_MAX_INCIDENTS:]

        tmp_path = incidents_path.with_suffix(".tmp")
        tmp_path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, incidents_path)
        return record
    except (PermissionError, OSError):
        return record


def get_recent_incidents(
    project_path: Path | str | None,
    agent: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Retrieve recent permission denial incidents from .sync/state/incidents.jsonl."""
    if not project_path:
        return []

    base_path = Path(project_path).resolve()
    incidents_path = base_path / INCIDENTS_RELATIVE_PATH
    if not incidents_path.is_file():
        return []

    results: list[dict[str, Any]] = []
    allowed_aliases = (
        ROLE_ALIASES.get(agent.strip().lower(), {agent.strip().lower()})
        if agent
        else set()
    )
    try:
        lines = [l for l in incidents_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    if agent:
                        item_agent = str(data.get("agent", "")).lower()
                        item_role = str(data.get("role", "")).lower()
                        if item_agent not in allowed_aliases and item_role not in allowed_aliases:
                            continue
                    results.append(data)
                    if len(results) >= limit:
                        break
            except Exception:
                continue
    except Exception:
        return []

    return results


def format_incidents_for_prompt(
    project_path: Path | str | None,
    agent: str | None = None,
    limit: int = 4,
) -> str:
    """Format recent permission denial incidents as actionable guidance for agent prompt injection."""
    incidents = get_recent_incidents(project_path, agent=agent, limit=limit)
    if not incidents:
        return ""

    lines = [
        "### ⚠️ RECENT PERMISSION DENIALS ENCOUNTERED (LEARN FROM PAST MISTAKES):",
        "You or sibling agents recently attempted operations in this workspace that were BLOCKED by governance policy:",
    ]

    for inc in incidents:
        tool = inc.get("tool", "unknown_tool")
        target = inc.get("target", "")
        reason = inc.get("reason", "outside assigned scope")
        remedy = inc.get("remedy", "")
        target_disp = f" on '{target}'" if target else ""
        lines.append(f"- 🔴 Attempted `{tool}`{target_disp} ➔ **BLOCKED** ({reason}).")
        if remedy:
            lines.append(f"  *Resolution Guide*: {remedy}")

    lines.append("\n**MANDATORY RULE**: DO NOT retry the blocked operations listed above. Strictly obey the role permissions.")
    return "\n".join(lines)


__all__ = [
    "INCIDENTS_RELATIVE_PATH",
    "ROLE_ALIASES",
    "compute_actionable_remedy",
    "format_incidents_for_prompt",
    "get_recent_incidents",
    "record_permission_incident",
]
