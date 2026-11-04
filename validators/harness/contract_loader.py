"""Harness contract loader and manager.

Attempts to locate and load a structured contract for the current harness task.
Searches for work order contracts (e.g. .sync/contracts/WO-142.yaml) and
agent contracts (e.g. .sync/agents/codex.contract.yaml).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from validators.knowledge.contract import AgentContract, path_to_module, module_matches


def load_harness_contract(
    project_path: Path, agent: str, work_order_id: str | None
) -> AgentContract | None:
    """Attempt to locate and load a structured contract for the current harness task.

    Search strategy:
    1. Search for work order contract (e.g. .sync/contracts/WO-142.yaml)
    2. Search for agent contract (e.g. .sync/agents/codex.contract.yaml)

    Returns the loaded AgentContract, or None if no contract is found.
    """
    # 1. Search for work order contract (e.g. .sync/contracts/WO-142.yaml)
    if work_order_id:
        try:
            from cli.contract import find_contract

            path = find_contract(work_order_id, project_path)
            if path is not None and path.exists():
                c = AgentContract.load(path, project_path)
                norm_agent = str(agent or "").lower().strip()
                c_agent = str(getattr(c, "agent_id", "") or "").lower().strip()
                if not norm_agent or not c_agent or norm_agent == c_agent:
                    return c
                # If agent IDs differ, check if contract was synthesized for recovery-repair
                # or if norm_agent is the worker assigned to this work order:
                is_recovery_synth = (
                    c.data.get("synthesized_by") == "recovery-repair"
                    or c_agent in ("claude", "architecture")
                )
                from validators.harness import review_gate
                wo_data = review_gate.load_wo(project_path, work_order_id)
                assigned = [str(a).lower().strip() for a in (wo_data.get("assigned_agents") or [])]
                if is_recovery_synth or norm_agent in assigned or not assigned:
                    c.agent_id = norm_agent
                    c.data["agent_id"] = norm_agent
                    ident = c.identity if isinstance(c.identity, dict) else {}
                    ident["role"] = getattr(c, "identity", {}).get("role", "worker")
                    ident["reports_to"] = "claude"
                    # Reinforce deliverable paths and expected files
                    deliv_paths = review_gate.wo_deliverable_paths(wo_data)
                    existing_mods = {r.get("module") for r in c.allow_rules if isinstance(r, dict)}
                    for dp in deliv_paths:
                        if dp and dp not in existing_mods:
                            rule = {"module": dp}
                            c.allow_rules.append(rule)
                            c.scope.setdefault("allow", []).append(rule)
                            existing_mods.add(dp)
                    est = wo_data.get("implementation_estimate") or {}
                    for ef in est.get("expected_files") or []:
                        if ef and ef not in existing_mods:
                            rule = {"module": ef}
                            c.allow_rules.append(rule)
                            c.scope.setdefault("allow", []).append(rule)
                            existing_mods.add(ef)
                    return c
        except (FileNotFoundError, Exception):
            pass

    # 2. Search for agent contract (e.g. .sync/agents/codex.contract.yaml)
    agent_contract_path = project_path / ".sync" / "agents" / f"{agent}.contract.yaml"
    if agent_contract_path.exists():
        try:
            contract = AgentContract.load(agent_contract_path, project_path)
            if work_order_id:
                contract.work_order = work_order_id
                contract.data["work_order"] = work_order_id
                from validators.harness import review_gate
                wo_data = review_gate.load_wo(project_path, work_order_id)
                deliv_paths = review_gate.wo_deliverable_paths(wo_data)
                existing_mods = {r.get("module") for r in contract.allow_rules if isinstance(r, dict)}
                for dp in deliv_paths:
                    if dp and dp not in existing_mods:
                        rule = {"module": dp}
                        contract.allow_rules.append(rule)
                        contract.scope.setdefault("allow", []).append(rule)
                        existing_mods.add(dp)
                est = wo_data.get("implementation_estimate") or {}
                for ef in est.get("expected_files") or []:
                    if ef and ef not in existing_mods:
                        rule = {"module": ef}
                        contract.allow_rules.append(rule)
                        contract.scope.setdefault("allow", []).append(rule)
                        existing_mods.add(ef)
            return contract
        except Exception:
            pass

    return None