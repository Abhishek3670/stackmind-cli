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
                return AgentContract.load(path, project_path)
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
            return contract
        except Exception:
            pass

    return None