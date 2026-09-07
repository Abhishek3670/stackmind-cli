"""Harness Integration & Contract Gate (PLANv3 §1.4)."""

from __future__ import annotations

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
    """Pre-execution validation: verify contract is valid and not expired."""
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
        
        # Deny check
        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module")
            if pattern and module_matches(mod, pattern):
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
                pattern = allow_rule.get("module")
                if pattern and module_matches(mod, pattern):
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
