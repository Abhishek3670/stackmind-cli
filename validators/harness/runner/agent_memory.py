"""Persistent Agent Memory & Cross-Turn Continuity (SDP-MEM-001).

Maintains persistent memory for all StackMind agents (Architect, Backend, Frontend, QA, GitOps):
1. Self-Work Memory: Recalls tasks, deliverables, and decisions authored by the agent in past turns.
2. Task Attempt & Blocker Memory: Recalls prior attempts, failures, and amendments on the active work order.
3. Completed Deliverables Index: Aware of all completed work orders and files authored by peer agents.
4. Conversational Continuity: Preserves multi-turn dialogue context across daemon cycles and restarts.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Sequence

import yaml

logger = logging.getLogger(__name__)


def get_agent_self_memory(project_path: Path, agent: str, max_records: int = 5) -> str:
    """Retrieve concise memory of recent turns, deliverables, and decisions by this specific agent."""
    outbox_dir = project_path / ".sync" / "outbox" / agent
    if not outbox_dir.is_dir():
        return ""

    reports: list[Path] = sorted(outbox_dir.glob("harness-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return ""

    memories: list[str] = []
    for r_path in reports[:max_records]:
        try:
            content = r_path.read_text(encoding="utf-8", errors="replace")
            # Extract task ID
            m_task = re.search(r"-\s*task:\s*`?([a-zA-Z0-9_-]+)`?", content)
            task_id = m_task.group(1) if m_task else "task"

            # Extract status
            m_status = re.search(r"-\s*status:\s*`?([a-zA-Z0-9_-]+)`?", content)
            status = m_status.group(1) if m_status else "completed"

            # Extract summary
            m_sum = re.search(r"## Summary\s*\n+([^\n#]+)", content)
            summary = m_sum.group(1).strip() if m_sum else ""

            # Extract any reported blockers
            m_block = re.search(r"## Blockers\s*\n+([^\n#]+)", content)
            blocker = m_block.group(1).strip() if m_block else ""

            if summary or task_id:
                line = f"- [{task_id}] (Status: {status})"
                if summary:
                    line += f": {summary}"
                if blocker and blocker.lower() not in ("none", "null", "[]"):
                    line += f" (Note: {blocker})"
                memories.append(line)
        except Exception:
            continue

    if not memories:
        return ""

    return (
        "### YOUR PREVIOUS WORK & MEMORY IN THIS PROJECT:\n"
        "You previously executed the following tasks in this workspace. Maintain continuity with your past decisions:\n"
        + "\n".join(memories)
        + "\n"
    )


def get_task_attempt_history(project_path: Path, work_order_id: str | None) -> str:
    """Retrieve attempt history, past blockers, and revision amendments for the active work order."""
    if not work_order_id:
        return ""

    sync_dir = project_path / ".sync" / "work-orders"
    wo_path: Path | None = None
    for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
        candidate = sync_dir / sub / f"{work_order_id}.yaml"
        if candidate.is_file():
            wo_path = candidate
            break

    if not wo_path:
        return ""

    try:
        data = yaml.safe_load(wo_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""

        lines: list[str] = []
        rev = data.get("revision", 1)
        status = data.get("status", "ACTIVE")

        # Check for previous errors or blockers
        err = data.get("error") or data.get("blocked_reason") or data.get("cancellation_reason")
        if err:
            lines.append(f"- Prior Issue / Blocker: {err}")

        # Check amendments
        amendments = data.get("amendments") or []
        if amendments:
            lines.append("- Recorded Amendments:")
            for a in amendments[-3:]:
                if isinstance(a, dict):
                    r_num = a.get("revision", "?")
                    r_reason = a.get("reason", "Amended")
                    lines.append(f"  * Rev {r_num}: {r_reason}")

        # Check logs
        log_entries = data.get("log") or []
        if log_entries:
            lines.append("- Recent Activity Log:")
            for entry in log_entries[-3:]:
                lines.append(f"  * {str(entry).strip()}")

        if not lines:
            return ""

        header = f"### TASK ATTEMPT & AMENDMENT HISTORY (CURRENT WORK ORDER: {work_order_id}, REVISION {rev}):\n"
        footer = (
            "IMPORTANT INVARIANT: If amending or repairing this active work order, you MUST increment the revision number "
            f"(to revision: {rev + 1}) and record an amendments entry with the concrete reason. Never clobber an active task in-place without bumping revision.\n"
            if status == "ACTIVE" else ""
        )
        return header + "\n".join(lines) + ("\n" + footer if footer else "\n")
    except Exception:
        return ""


def get_completed_deliverables_summary(project_path: Path) -> str:
    """Retrieve a concise directory of all completed work orders and their authored files."""
    completed_dir = project_path / ".sync" / "work-orders" / "COMPLETED"
    if not completed_dir.is_dir():
        return ""

    items: list[str] = []
    for p in sorted(completed_dir.glob("WO-*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            wo_id = data.get("id") or p.stem
            title = data.get("title", "")
            agents = data.get("assigned_agents") or []
            agent_str = agents[0] if agents else "specialist"
            deliv = data.get("deliverable") or {}
            deliv_path = deliv.get("path") or ""

            entry = f"- {wo_id} ({agent_str}): {title}"
            if deliv_path:
                entry += f" -> `{deliv_path}`"
            items.append(entry)
        except Exception:
            continue

    if not items:
        return ""

    return (
        "### COMPLETED PROJECT DELIVERABLES (WHAT ALREADY EXISTS ON DISK):\n"
        "The following deliverables have already been completed and verified by your team members. "
        "Align with and build upon these deliverables:\n"
        + "\n".join(items)
        + "\n"
    )


def build_agent_persistent_memory_context(
    project_path: Path,
    agent: str,
    work_order_id: str | None = None,
) -> str:
    """Assemble complete multi-faceted persistent memory context for the agent's active turn."""
    blocks: list[str] = []

    # 1. Self past work memory
    self_mem = get_agent_self_memory(project_path, agent)
    if self_mem:
        blocks.append(self_mem)

    # 2. Work order attempt and amendment history
    task_hist = get_task_attempt_history(project_path, work_order_id)
    if task_hist:
        blocks.append(task_hist)

    # 3. Completed deliverables index
    deliv_mem = get_completed_deliverables_summary(project_path)
    if deliv_mem:
        blocks.append(deliv_mem)

    return "\n".join(blocks).strip()
