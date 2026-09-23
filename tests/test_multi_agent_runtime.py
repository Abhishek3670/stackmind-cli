from pathlib import Path

import pytest

from validators.kernel import AgentContract, AgentRole, MultiAgentSupervisor
from validators.kernel.daemon import DaemonStorage, SessionManager


def contract(agent: str, write="read-write"):
    return AgentContract(agent, "WO-008", ("workspace/**",), (), write)


def supervisor(tmp_path: Path):
    (tmp_path / "repo").mkdir()
    manager = SessionManager(DaemonStorage(tmp_path / "state"))
    sup = MultiAgentSupervisor(manager, tmp_path / "repo")
    roles = {
        role: {
            "agent_id": role.value,
            "contract": contract(
                role.value, "read-only" if role is not AgentRole.WORKER else "read-write"
            ),
        }
        for role in AgentRole
    }
    sup.create_ensemble("ens", roles)
    return sup, manager


def test_supervised_workflow_lifecycle(tmp_path):
    sup, manager = supervisor(tmp_path)
    task = sup.delegate_task("ens", AgentRole.PLANNER, AgentRole.WORKER, "implement")
    handoff = sup.handoff("ens", task.task_id, "scratch diff", {"scope": True, "behavioral": True})
    assert sup.route_review(handoff) and sup.complete_ensemble("ens")["complete"]
    assert any(e.name == "multi.delegation" for e in manager.events.events())


def test_workspace_isolation_containment(tmp_path):
    sup, _ = supervisor(tmp_path)
    members = sup.ensembles["ens"]
    assert sup.enforce_isolation(
        "ens", "worker", str(members[AgentRole.WORKER].workspace.root / "a")
    )
    with pytest.raises(PermissionError, match="cross-workspace"):
        sup.enforce_isolation(
            "ens", "worker", str(members[AgentRole.REVIEWER].workspace.root / "a")
        )


def test_per_role_contract_and_privilege_escalation_blocked(tmp_path):
    sup, _ = supervisor(tmp_path)
    with pytest.raises(PermissionError):
        sup.enforce_isolation("ens", "attacker", "x")
    with pytest.raises(PermissionError):
        sup.delegate_task("ens", AgentRole.WORKER, AgentRole.REVIEWER, "bypass")


def test_governed_delegation_and_handoff(tmp_path):
    sup, _ = supervisor(tmp_path)
    task = sup.delegate_task("ens", AgentRole.PLANNER, AgentRole.WORKER, "change")
    record = sup.handoff("ens", task.task_id, "diff", {"authentic": True})
    assert record.workspace == str(sup.ensembles["ens"][AgentRole.WORKER].workspace.root)
