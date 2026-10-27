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

    # Generate helpful default remedy based on role and tool
    clean_role = role.strip().lower()
    if not remedy:
        if clean_role in ("claude", "architect", "architecture"):
            if tool in ("run_command", "bash", "execute_command"):
                remedy = "Shell execution is blocked for Architect. Use 'read_file' to inspect files or 'list_directory' to explore paths."
            elif tool in ("write_file", "apply_patch", "edit_file"):
                remedy = "You cannot directly write application files. Delegate this task to Frontend (Gemini) or Backend (Codex) via 'delegate_task' or by authoring a Work Order in .sync/work-orders/ACTIVE/."
            else:
                remedy = "Action blocked by governance boundary. Consult the Permission Matrix."
        elif clean_role in ("gemini", "frontend"):
            remedy = "Frontend specialist scope is restricted to UI/views (*.html, *.css, assets/**). Do not write backend APIs or governance files."
        elif clean_role in ("codex", "backend"):
            remedy = "Backend specialist scope is restricted to src/** and server components. Do not write frontend templates or governance files."
        else:
            remedy = f"Operation '{tool}' on '{target}' is outside assigned scope. Consult your work order contract."

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
    try:
        lines = [l for l in incidents_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    if agent and str(data.get("agent", "")).lower() != agent.strip().lower():
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
    "format_incidents_for_prompt",
    "get_recent_incidents",
    "record_permission_incident",
]
