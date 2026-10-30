"""Contract pre and post-execution validation.

Handles:
- D024 QA Gate: Programmatic enforcement preventing GitOps progression without QA approval
- Contract expiration checks
- Work order ID matching
- Write mode verification (read-only vs read-write)
- File touch budget checking
- Scope allowed/denied checks per modified file
- D025 Destructive Operations safeguard
- D024 QA Gate for GitOps progression
"""

from __future__ import annotations

import fnmatch
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


CONTRACT_FAILURE_CODES = {
    "CONTRACT_EXPIRED": "Contract expired before write-back",
    "CONTRACT_READ_ONLY_VIOLATION": "Files modified under a read-only contract",
    "CONTRACT_FILE_BUDGET_EXCEEDED": "Observed changed files exceeded the contract file budget",
    "CONTRACT_SCOPE_DENIED": "Modification explicitly denied by a contract deny rule",
    "CONTRACT_SCOPE_VIOLATION": "Modification outside the contract allow scope",
    "CONTRACT_VALIDATION_ERROR": "Post-execution contract validation error",
}


def _norm_paths(paths: Any) -> list[str]:
    """Normalize a path or list of paths to sorted unique forward-slash paths."""
    return sorted({
        str(f).replace("\\", "/").lstrip("/")
        for f in (paths or [])
        if str(f).strip()
    })


def verify_pre_execution(
    project_path: Path,
    agent: str,
    task: Any,  # HarnessTask
    context: ContextBundle,
) -> None:
    """Pre-execution validation: verify contract is valid, not expired, and D024 compliant for GitOps.

    D024 QA Gate: Programmatic enforcement preventing GitOps progression without QA approval.
    """
    # D024 QA Gate: Programmatic enforcement preventing GitOps progression without QA approval
    if getattr(task, "work_order_id", None) and str(agent).lower().strip() in ("local-llm", "gitops"):
        from .d024_gate import D024Gate

        D024Gate().verify_gitops_preconditions(project_path, task.work_order_id)

    contract = load_harness_contract(project_path, agent, task.work_order_id)
    if contract is None:
        return

    # Check expiration
    if contract.is_expired():
        raise ContractExpiredError(f"Contract {contract.work_order} has expired")

    # Verify work order ID matching
    if task.work_order_id and task.work_order_id != contract.work_order:
        if contract.work_order == "WO-000":
            contract.work_order = task.work_order_id
            contract.data["work_order"] = task.work_order_id
        else:
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
    """Post-execution validation: verify decision output and observed changes against contract.

    Validates:
    1. Contract expiration
    2. Write mode (read-only vs read-write)
    3. Files touched budget (excluding internal bookkeeping/protocol files under .sync/)
    4. Scope allowed/denied for each modified file
    5. D025 Destructive Operations in commands
    6. D024 QA Gate for GitOps progression
    """
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

    # 2. Check files touched budget (excluding internal bookkeeping/protocol files under .sync/)
    max_files = contract.budget.get("max_files_touched") if contract.budget else None
    project_files_to_check = tuple(
        f for f in files_to_check
        if not str(f).replace("\\", "/").startswith(".sync/")
        and not any(
            str(f).replace("\\", "/").lstrip("/").startswith(p)
            for p in ("node_modules/", ".next/", ".turbo/", "dist/", "build/")
        )
    )
    if max_files is not None and len(project_files_to_check) > max_files:
        raise ContractAccessDenied(
            f"{len(project_files_to_check)} project file(s) modified, exceeding budget max_files_touched limit of {max_files}"
        )

    # 3. Check scope allowed/denied for each modified file
    api = KnowledgeAPI(project_path)
    from .contract_loader import load_harness_contract as _load_contract

    peer_owned = _collect_peer_owned_deliverables(
        project_path, getattr(task, "work_order_id", None), agent
    )
    for file_path in files_to_check:
        norm_fp = file_path.replace("\\", "/").lstrip("/")
        if norm_fp.startswith("./"):
            norm_fp = norm_fp[2:]

        if any(norm_fp.startswith(p) for p in ("node_modules/", ".next/", ".turbo/", "dist/", "build/")):
            continue

        # Cross-worker modification guard: workers cannot modify each other's work
        if norm_fp in peer_owned:
            peer_wo, peer_agent = peer_owned[norm_fp]
            raise ContractAccessDenied(
                f"Modification to file '{file_path}' is rejected: owned by another work order "
                f"('{peer_wo}' assigned to '{peer_agent}'). Workers cannot modify each other's work; "
                "inter-worker needs must be coordinated through Architecture."
            )

        # Normalize file path for comparison
        norm_fp = file_path.replace("\\", "/").lstrip("/")
        if norm_fp.startswith("./"):
            norm_fp = norm_fp[2:]

        mod = path_to_module(norm_fp)
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
                    p_clean = p_norm.rstrip("/*").rstrip("/")
                    overridden = False
                    for allow_rule in contract.allow_rules:
                        a_pat = allow_rule.get("module") or allow_rule.get("path") or allow_rule.get("target")
                        if a_pat:
                            a_norm = a_pat.replace("\\", "/").lstrip("./")
                            a_clean = a_norm.rstrip("/*").rstrip("/")
                            if (
                                norm_fp == a_norm
                                or norm_fp.startswith(a_clean + "/")
                                or fnmatch.fnmatch(norm_fp, a_norm)
                            ) and (
                                (a_clean.startswith(p_clean + "/") or fnmatch.fnmatch(a_clean, p_norm))
                                and not (p_clean.startswith(a_clean + "/") or fnmatch.fnmatch(p_clean, a_norm))
                            ):
                                overridden = True
                                break
                    if not overridden:
                        raise ContractAccessDenied(
                            f"Modification to file {file_path} is explicitly denied by rule: {pattern}"
                        )

        # Allow check
        allowed = False
        module_nodes = [s for s in api.ir.symbols if path_to_module(s.path) == mod]
        if module_nodes:
            allowed = any(
                contract.is_node_in_scope(s.node_id, api.ir) for s in module_nodes
            )
        if not contract.allow_rules:
            # Specialized workers are free to write own files without microscopic scope.allow
            allowed = True
        elif not allowed:
            # File might be new or not resolved in IR, check module pattern match directly
            for allow_rule in contract.allow_rules:
                pattern = allow_rule.get("module") or allow_rule.get("path") or allow_rule.get("target")
                if pattern:
                    p_norm = pattern.replace("\\", "/")
                    if p_norm.startswith("./"):
                        p_norm = p_norm[2:]
                    p_clean = p_norm.rstrip("/*").rstrip("/")
                    if (
                        module_matches(mod, pattern)
                        or fnmatch.fnmatch(norm_fp, p_norm)
                        or fnmatch.fnmatch(norm_fp, p_clean + "/*")
                        or fnmatch.fnmatch(norm_fp, p_clean + "/**")
                        or norm_fp == p_norm
                        or norm_fp == p_clean
                        or norm_fp.startswith(p_clean + "/")
                    ):
                        allowed = True
                        break
        if not allowed:
            raise ContractAccessDenied(
                f"Modification to file {file_path} is outside allowed contract scope"
            )

    # 4. Check D025 Destructive Operations in commands
    if hasattr(decision, "commands") and decision.commands:
        from .d025_gate import D025Gate, D025ViolationError

        gate = D025Gate()
        gate_decision = gate.evaluate_sequence(decision.commands)
        gate.log_decision(project_path, agent, gate_decision, task_id=getattr(task, "identifier", None))
        if not gate_decision.passed:
            raise D025ViolationError(
                f"Command sequence triggered D025 Destructive Operations Safeguard: {gate_decision.reason}"
            )

    # 5. Check D024 QA Gate for GitOps progression
    if getattr(task, "work_order_id", None) and str(agent).lower().strip() in ("local-llm", "gitops"):
        from .d024_gate import D024Gate

        D024Gate().verify_gitops_preconditions(project_path, task.work_order_id)


def _collect_peer_owned_deliverables(
    project_path: Path, own_wo_id: str | None, current_agent: str
) -> dict[str, tuple[str, str]]:
    """Map normalized file path -> (work_order_id, assigned_agent) for files owned by other workers.

    This is duplicated from authoring_gate.py to avoid circular imports; consider
    consolidating in a future refactor.
    """
    owned: dict[str, tuple[str, str]] = {}
    base = Path(project_path) / ".sync" / "work-orders"
    if not base.is_dir():
        return owned
    norm_agent = str(current_agent or "").lower().strip()
    import yaml

    for bucket in ("ACTIVE", "COMPLETED"):
        b_dir = base / bucket
        if not b_dir.is_dir():
            continue
        for wo_file in b_dir.glob("*.yaml"):
            if wo_file.stem == own_wo_id or wo_file.stem in ("WO-000", ".gitkeep"):
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if str(data.get("status", "")).upper() in ("CANCELLED", "FAILED"):
                continue
            assigned = data.get("assigned_agents") or []
            if isinstance(assigned, str):
                assigned = [assigned]
            assigned_norm = [str(a).lower().strip() for a in assigned]
            if norm_agent and norm_agent in assigned_norm:
                continue
            peer_agent = assigned_norm[0] if assigned_norm else "unknown"
            deliv = data.get("deliverable")
            if isinstance(deliv, dict) and deliv.get("path"):
                p = str(deliv["path"]).replace("\\", "/").strip().lstrip("/")
                if p:
                    owned[p] = (wo_file.stem, peer_agent)
            for extra in data.get("deliverables") or []:
                if isinstance(extra, dict) and extra.get("path"):
                    p = str(extra["path"]).replace("\\", "/").strip().lstrip("/")
                    if p:
                        owned[p] = (wo_file.stem, peer_agent)
                elif isinstance(extra, str) and extra.strip():
                    p = extra.replace("\\", "/").strip().lstrip("/")
                    if p:
                        owned[p] = (wo_file.stem, peer_agent)
    return owned