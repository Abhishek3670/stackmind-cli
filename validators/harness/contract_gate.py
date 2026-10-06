"""Harness Integration & Contract Gate (PLANv3 §1.4)."""

from __future__ import annotations

import fnmatch
import hashlib
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
from validators.knowledge.api import KnowledgeAPI, ContextBundle

# Stable failure codes for governed post-execution blocks.  These codes travel
# in the durable evidence packet and drive the supervisor's recovery routing.
CONTRACT_FAILURE_CODES = {
    "CONTRACT_EXPIRED": "Contract expired before write-back",
    "CONTRACT_READ_ONLY_VIOLATION": "Files modified under a read-only contract",
    "CONTRACT_FILE_BUDGET_EXCEEDED": "Observed changed files exceeded the contract file budget",
    "CONTRACT_SCOPE_DENIED": "Modification explicitly denied by a contract deny rule",
    "CONTRACT_SCOPE_VIOLATION": "Modification outside the contract allow scope",
    "CONTRACT_VALIDATION_ERROR": "Post-execution contract validation error",
}


def _contract_file_sha256(project_path: Path, work_order_id: str | None) -> str | None:
    """Hash the raw contract artifact so retries can verify the contract is unchanged."""
    if not work_order_id:
        return None
    try:
        from cli.contract import find_contract
        path = find_contract(work_order_id, project_path)
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        pass
    return None


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
    declared = _norm(getattr(decision, "modified_files", None) or []) if decision is not None else []
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
    """Persist the evidence packet under the governed reports path before cleanup."""
    try:
        work_order_id = str(evidence.get("work_order_id") or "unknown")
        operation_id = str(evidence.get("operation_id") or "no-op")
        blockers_dir = Path(project_path) / ".sync" / "reports" / "blockers"
        blockers_dir.mkdir(parents=True, exist_ok=True)
        path = blockers_dir / f"{work_order_id}-{operation_id}.json"
        import json
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)
    except Exception:
        return None

def load_harness_contract(project_path: Path, agent: str, work_order_id: str | None) -> AgentContract | None:
    """Attempt to locate and load a structured contract for the current harness task."""
    # 1. Search for work order contract (e.g. .sync/contracts/WO-142.yaml)
    if work_order_id:
        try:
            from cli.contract import find_contract
            path = find_contract(work_order_id, project_path)
            if path.exists():
                return AgentContract.load(path, project_path)
        except (FileNotFoundError, Exception):
            pass
            
    # 2. Search for agent contract (e.g. .sync/agents/codex.contract.yaml)
    agent_contract_path = project_path / ".sync" / "agents" / f"{agent}.contract.yaml"
    if agent_contract_path.exists():
        try:
            return AgentContract.load(agent_contract_path, project_path)
        except Exception:
            pass
        
    return None

def verify_pre_execution(
    project_path: Path,
    agent: str,
    task: Any,  # HarnessTask
    context: ContextBundle,
) -> None:
    """Pre-execution validation: verify contract is valid, not expired, and D024 compliant for GitOps."""
    # D024 QA Gate: Programmatic enforcement preventing GitOps progression without QA approval
    if getattr(task, "work_order_id", None) and str(agent).lower().strip() in ("local-llm", "gitops"):
        from validators.harness.d024_gate import D024Gate
        D024Gate().verify_gitops_preconditions(project_path, task.work_order_id)

    contract = load_harness_contract(project_path, agent, task.work_order_id)
    if contract is None:
        return
        
    # Check expiration
    if contract.is_expired():
        raise ContractExpiredError(f"Contract {contract.work_order} has expired")
        
    # Verify work order ID matching
    if task.work_order_id and task.work_order_id != contract.work_order:
        raise ContractAccessDenied(
            f"Task work order {task.work_order_id} does not match contract work order {contract.work_order}"
        )

def verify_post_execution(
    project_path: Path,
    agent: str,
    task: Any,  # HarnessTask
    decision: Any,  # HarnessDecision
    observed_files: Any | None = None,  # Sequence[str] | None
) -> None:
    """Post-execution validation: verify decision output and observed changes against contract."""
    contract = load_harness_contract(project_path, agent, task.work_order_id)
    if contract is None:
        return
        
    # Check expiration
    if contract.is_expired():
        raise ContractExpiredError(f"Contract {contract.work_order} has expired")
        
    # Authoritative change set: prefer runner-observed changes over LLM claims
    if observed_files is not None:
        files_to_check = tuple(str(f) for f in observed_files)
    else:
        files_to_check = tuple(str(f) for f in decision.modified_files)

    # 1. Check write mode (read-only vs read-write)
    if files_to_check:
        if contract.write_mode == "read-only":
            raise ContractAccessDenied(
                f"Contract {contract.work_order} is read-only but {len(files_to_check)} file(s) were modified"
            )
            
    # 2. Check files touched budget
    max_files = contract.budget.get("max_files_touched")
    if max_files is not None and len(files_to_check) > max_files:
        raise ContractAccessDenied(
            f"{len(files_to_check)} file(s) modified, exceeding budget max_files_touched limit of {max_files}"
        )
        
    # 3. Check scope allowed/denied for each modified file
    api = KnowledgeAPI(project_path)
    for file_path in files_to_check:
        mod = path_to_module(file_path)
        norm_fp = file_path.replace("\\", "/")
        if norm_fp.startswith("./"):
            norm_fp = norm_fp[2:]
        elif norm_fp.startswith("/"):
            norm_fp = norm_fp[1:]
        
        # Deny check
        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module") or deny_rule.get("path") or deny_rule.get("target")
            if pattern:
                p_norm = pattern.replace("\\", "/")
                if p_norm.startswith("./"):
                    p_norm = p_norm[2:]
                if (
                    module_matches(mod, pattern)
                    or fnmatch.fnmatch(norm_fp, p_norm)
                    or fnmatch.fnmatch(norm_fp, p_norm.rstrip("/") + "/*")
                    or fnmatch.fnmatch(norm_fp, p_norm.rstrip("/") + "/**")
                    or norm_fp == p_norm
                    or norm_fp.startswith(p_norm.rstrip("/") + "/")
                ):
                    raise ContractAccessDenied(
                        f"Modification to file {file_path} is explicitly denied by rule: {pattern}"
                    )
                
        # Allow check
        allowed = False
        module_nodes = [s for s in api.ir.symbols if path_to_module(s.path) == mod]
        if module_nodes:
            allowed = any(contract.is_node_in_scope(s.node_id, api.ir) for s in module_nodes)
        else:
            # File might be new, check module pattern match directly
            for allow_rule in contract.allow_rules:
                pattern = allow_rule.get("module") or allow_rule.get("path") or allow_rule.get("target")
                if pattern:
                    p_norm = pattern.replace("\\", "/")
                    if p_norm.startswith("./"):
                        p_norm = p_norm[2:]
                    if (
                        module_matches(mod, pattern)
                        or fnmatch.fnmatch(norm_fp, p_norm)
                        or fnmatch.fnmatch(norm_fp, p_norm.rstrip("/") + "/*")
                        or fnmatch.fnmatch(norm_fp, p_norm.rstrip("/") + "/**")
                        or norm_fp == p_norm
                        or norm_fp.startswith(p_norm.rstrip("/") + "/")
                    ):
                        allowed = True
                        break
        if not allowed:
            raise ContractAccessDenied(
                f"Modification to file {file_path} is outside allowed contract scope"
            )
            
    # 4. Check D025 Destructive Operations in commands
    if hasattr(decision, "commands") and decision.commands:
        from validators.harness.d025_gate import D025Gate, D025ViolationError
        gate = D025Gate()
        gate_decision = gate.evaluate_sequence(decision.commands)
        gate.log_decision(project_path, agent, gate_decision, task_id=getattr(task, "identifier", None))
        if not gate_decision.passed:
            raise D025ViolationError(
                f"Command sequence triggered D025 Destructive Operations Safeguard: {gate_decision.reason}"
            )

    # 5. Check D024 QA Gate for GitOps progression
    if getattr(task, "work_order_id", None) and str(agent).lower().strip() in ("local-llm", "gitops"):
        from validators.harness.d024_gate import D024Gate
        D024Gate().verify_gitops_preconditions(project_path, task.work_order_id)
