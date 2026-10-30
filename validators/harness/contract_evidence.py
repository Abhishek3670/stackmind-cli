"""Contract failure evidence packet builder.

Builds the durable worker-blocker evidence packet for a contract failure.
The packet preserves the exact observed file set (not just a count), the
declaration mismatch, the file budget, the contract hash/revision, and a
stable ``failure_code`` — persisted before the scratch workspace is
discarded so the Architect and operator can inspect the real evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validators.knowledge.contract import (
    AgentContract,
    ContractAccessDenied,
    ContractExpiredError,
    path_to_module,
    module_matches,
)
from validators.knowledge.api import KnowledgeAPI


# Stable failure codes for governed post-execution blocks. These codes travel
# in the durable evidence packet and drive the supervisor's recovery routing.
CONTRACT_FAILURE_CODES = {
    "CONTRACT_EXPIRED": "Contract expired before write-back",
    "CONTRACT_READ_ONLY_VIOLATION": "Files modified under a read-only contract",
    "CONTRACT_FILE_BUDGET_EXCEEDED": "Observed changed files exceeded the contract file budget",
    "CONTRACT_SCOPE_DENIED": "Modification explicitly denied by a contract deny rule",
    "CONTRACT_SCOPE_VIOLATION": "Modification outside the contract allow scope",
    "CONTRACT_VALIDATION_ERROR": "Post-execution contract validation error",
}


def _norm(paths: Any) -> list[str]:
    """Normalize a path or list of paths to sorted unique forward-slash paths."""
    return sorted({
        str(f).replace("\\", "/").lstrip("/")
        for f in (paths or [])
        if str(f).strip()
    })


def build_contract_failure_evidence(
    project_path: Path,
    agent: str,
    task: Any,  # HarnessTask
    exc: Exception,
    *,
    observed_files: Any | None = None,
    decision: Any | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Build the durable worker-blocker evidence packet for a contract failure.

    The packet preserves the exact observed file set (not just a count), the
    declaration mismatch, the file budget, the contract hash/revision, and a
    stable ``failure_code`` — persisted before the scratch workspace is
    discarded so the Architect and operator can inspect the real evidence.
    """
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, ContractExpiredError):
        failure_code = "CONTRACT_EXPIRED"
    elif "is read-only but" in lowered:
        failure_code = "CONTRACT_READ_ONLY_VIOLATION"
    elif "exceeding budget max_files_touched" in lowered:
        failure_code = "CONTRACT_FILE_BUDGET_EXCEEDED"
    elif "explicitly denied by rule" in lowered:
        failure_code = "CONTRACT_SCOPE_DENIED"
    elif "outside allowed contract scope" in lowered:
        failure_code = "CONTRACT_SCOPE_VIOLATION"
    else:
        failure_code = "CONTRACT_VALIDATION_ERROR"

    work_order_id = getattr(task, "work_order_id", None)
    contract = load_harness_contract(project_path, agent, work_order_id)
    file_budget: int | None = None
    contract_hash = _contract_file_sha256(project_path, work_order_id)
    contract_revision = 1
    if contract is not None:
        budget = contract.budget or {}
        candidate = budget.get("max_files_touched")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            file_budget = candidate
        try:
            contract_revision = int(contract.data.get("revision", 1) or 1)
        except Exception:
            contract_revision = 1

    def _norm(paths: Any) -> list[str]:
        return sorted({
            str(f).replace("\\", "/").lstrip("/")
            for f in (paths or [])
            if str(f).strip()
        })

    observed = _norm(observed_files)
    declared = _norm(getattr(decision, "modified_files", None) if decision is not None else None) or []
    declaration_mismatch = None
    if declared or observed:
        declaration_mismatch = {
            "undeclared": [f for f in observed if f not in declared],
            "missing": [f for f in declared if f not in observed],
        }

    commands = [str(c) for c in (getattr(decision, "commands", None) or ())] if decision is not None else []

    canonical_message = f"{failure_code}: {message}"

    return {
        "failure_code": failure_code,
        "canonical_message": canonical_message,
        "raw_reason": message,
        "work_order_id": work_order_id,
        "agent": agent,
        "operation_id": operation_id,
        "observed_files": observed,
        "observed_file_count": len(observed),
        "declared_modified_files": declared,
        "declaration_mismatch": declaration_mismatch,
        "file_budget": file_budget,
        "contract_hash": contract_hash,
        "contract_revision": contract_revision,
        "commands": commands,
        "validation_diagnostics": [message],
        "blocked_at": datetime.now(timezone.utc).isoformat(),
    }


def persist_blocker_evidence(project_path: Path, evidence: dict[str, Any]) -> str | None:
    """Persist the evidence packet under the governed reports path before cleanup.

    Returns the path to the persisted evidence file, or None on failure.
    """
    try:
        work_order_id = str(evidence.get("work_order_id") or "unknown")
        operation_id = str(evidence.get("operation_id") or "no-op")
        blockers_dir = Path(project_path) / ".sync" / "reports" / "blockers"
        blockers_dir.mkdir(parents=True, exist_ok=True)
        path = blockers_dir / f"{work_order_id}-{operation_id}.json"
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except Exception:
        return None