"""Role authorization for authored artifacts (CONTRACT-01 / AGENTS.md).\n\nEnforces that only authorized roles (claude, architecture, ceo) may author\ncontracts and work orders. Workers (codex, gemini, local-llm, gemma) are strictly\nforbidden.\n"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

AUTHORIZED_AUTHORS = {"claude", "architecture", "architect", "ceo", "admin", "system", "test"}
WORKER_ROLES = {"codex", "gemini", "local-llm", "gemma"}


def is_gitops_work_order(data: dict[str, Any]) -> bool:
    """Check if a work order is designated for the GitOps release role."""
    if not isinstance(data, dict):
        return False
    assigned = data.get("assigned_agents") or []
    if isinstance(assigned, str):
        assigned = [assigned]
    assigned_norm = [str(a).lower().strip() for a in assigned]
    if any(any(kw in a for kw in ("local-llm", "local_llm", "gitops", "release")) for a in assigned_norm):
        return True
    wo_title = str(data.get("title", "")).lower()
    wo_type = str(data.get("type", "")).lower()
    if any(kw in wo_title for kw in ("gitops", "release")) or wo_type in ("release", "gitops"):
        return True
    return False


def is_authorized(agent: str | None) -> tuple[bool, str | None]:
    """Verify that the requesting agent has authority to author governed artifacts.\n\nReturns (authorized, reason). If authorized, reason is None.\nIf not authorized, reason describes the authorization failure.\n"""
    if not agent:
        return True, None
    agent_norm = agent.lower().strip()
    if agent_norm in WORKER_ROLES:
        return (
            False,
            f"Worker role '{agent}' is not authorized to author or modify governed artifact.",
        )
    if agent_norm not in AUTHORIZED_AUTHORS:
        return (
            False,
            f"Agent role '{agent}' is not authorized to author governed artifacts. "
            f"Only Claude (Architect) or CEO hold authoring authority.",
        )
    return True, None


def classify_artifact(path: str) -> str:
    """Classify a relative path as 'work_order', 'contract', 'index', or 'unknown'."""
    norm = path.replace("\\", "/").strip().lstrip("/")
    if norm.endswith("INDEX.yaml") or norm.endswith("INDEX.yml"):
        return "index"
    if ".sync/contracts" in norm or norm.startswith("contracts/"):
        return "contract"
    if (".sync/work-orders" in norm or norm.startswith("work-orders/")) and re.search(r"WO-[0-9]{3}\.ya?ml$", norm):
        return "work_order"
    if re.search(r"^WO-[0-9]{3}\.ya?ml$", norm):
        if "contract" in norm:
            return "contract"
        return "work_order"
    return "unknown"