"""Role authorization for authored artifacts (CONTRACT-01 / AGENTS.md).\n\nEnforces that only authorized roles (claude, architecture, ceo) may author\ncontracts and work orders. Workers (codex, gemini, local-llm, gemma) are strictly\nforbidden.\n"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

AUTHORIZED_AUTHORS = {"claude", "architecture", "architect", "ceo", "admin", "system", "test"}
WORKER_ROLES = {"codex", "gemini", "local-llm", "gemma"}

# SDP-FIX-009A-HF1: the `.sync/**` deny-rule exemption is only ever granted to
# the Architecture role itself (not CEO/admin/system), and only for recovery
# actions that legitimately re-author governance contracts.
ARCHITECTURE_ROLES = frozenset({"claude", "architecture", "architect"})
PERMITTED_CONTRACT_REPAIR_ACTIONS = frozenset({
    "amend_contract",
    "split_work_order",
    "create_dependency_work_order",
    "architect_fix",
    "resolve_blocker",
})


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


def normalize_rel_path(path: str | None) -> str:
    """Normalize a relative path to forward slashes without leading `./` or `/`."""
    norm = str(path or "").replace("\\", "/").strip().lstrip("/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_contract_repair_path(path: str | None) -> bool:
    """True only for a direct `.sync/contracts/<name>.yaml|yml` path.

    Rejects the `.sync/` root, `.sync/policies|governance|work-orders`, nested
    subdirectories, traversal segments, and every other project path.
    """
    norm = normalize_rel_path(path)
    if not norm or "\\" in norm:
        return False
    parts = norm.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        return False
    if len(parts) != 3 or parts[0] != ".sync" or parts[1] != "contracts":
        return False
    name = parts[2]
    if name.startswith(".") or not name.endswith((".yaml", ".yml")):
        return False
    if len(name) <= 5:  # ".yaml"/".yml" alone is not a contract artifact
        return False
    return True


def is_sync_deny_pattern(pattern: str | None) -> bool:
    """True when a deny rule targets `.sync` or a path inside it."""
    p = normalize_rel_path(pattern)
    return p in (".sync/**", ".sync/*", ".sync") or p.startswith(".sync/")


def load_repair_authorization(
    project_path: Path | str | None, work_order_id: str | None
) -> dict[str, Any] | None:
    """Load the supervisor-written recovery decision authorizing a contract repair.

    Reads the existing trusted provenance channel (the supervisor writes
    `.sync/decisions/recovery/<WO>.decision.json`). It does not introduce a
    second authorization system.
    """
    if not project_path or not work_order_id:
        return None
    decision_file = (
        Path(project_path) / ".sync" / "decisions" / "recovery" / f"{work_order_id}.decision.json"
    )
    try:
        data = json.loads(decision_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("work_order") or "").strip() != str(work_order_id).strip():
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in PERMITTED_CONTRACT_REPAIR_ACTIONS:
        return None
    return {
        "work_order": str(work_order_id),
        "action": action,
        "provenance_path": str(decision_file),
        "contract_revision": data.get("contract_revision"),
        "failure_code": data.get("failure_code"),
    }


def architecture_contract_repair_exempt(
    project_path: Path | str | None,
    agent: str | None,
    task: Any,
    contract: Any,
    path: str | None,
) -> bool:
    """Authorize the narrowly scoped `.sync/**` deny-rule exemption.

    All conditions must hold together; each one alone is insufficient:
      1. authoritative Architecture role identity (workers and arbitrary
         agent-name strings fail, as does worker impersonation),
      2. an explicitly authorized authoring/contract-repair task for a work
         order (the authoring flag alone is not enough),
      3. supervisor-written recovery authorization provenance for that exact
         work order, with a permitted contract-repair action,
      4. an exact, non-traversing `.sync/contracts/<name>.yaml|yml` path.
    """
    # 1. Trusted role authorization: worker roles are denied outright; only the
    # Architecture role itself may use the exemption (no CEO/admin/system).
    authorized, _reason = is_authorized(agent)
    if not authorized:
        return False
    agent_norm = str(agent or "").lower().strip()
    if agent_norm not in ARCHITECTURE_ROLES:
        return False

    # 2. Explicit contract-repair task identity. `is_authoring` is required but
    # never sufficient on its own.
    if not bool(getattr(task, "is_authoring", False)):
        return False
    wo_id = str(getattr(task, "work_order_id", None) or "").strip()
    if not wo_id or re.search(r"[\./\\]", wo_id):
        return False
    contract_wo = str(getattr(contract, "work_order", "") or "").strip()
    if contract_wo and contract_wo != wo_id:
        return False

    # 3. Traceable authorization provenance from the supervisor's recovery
    # decision channel.
    provenance = load_repair_authorization(project_path, wo_id)
    if provenance is None:
        return False

    # 4. Exact permitted path: only direct contract YAML files under
    # `.sync/contracts/`, never `.sync/` root, policies, governance or work orders.
    if not is_contract_repair_path(path):
        return False
    return True


def classify_artifact(path: str) -> str:
    """Classify a relative path as 'work_order', 'contract', 'index', or 'unknown'."""
    norm = path.replace("\\", "/").strip().lstrip("/")
    if norm.endswith("INDEX.yaml") or norm.endswith("INDEX.yml"):
        return "index"
    if ".sync/contracts" in norm or norm.startswith("contracts/"):
        return "contract"
    if (".sync/work-orders" in norm or norm.startswith("work-orders/")) and norm.endswith((".yaml", ".yml")) and not norm.endswith(".gitkeep"):
        return "work_order"
    if re.search(r"^WO-.*\.ya?ml$", norm):
        if "contract" in norm:
            return "contract"
        return "work_order"
    return "unknown"