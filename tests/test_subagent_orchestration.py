"""Comprehensive test suite for multi-role Work Order dispatch and subagent orchestration (P7-3).

Covers PLAN_STACKMIND_CLI_FINAL.md §22 and PLAN_TUI_v7.md §29:
- Multi-role Work Order dispatch (Architecture dispatching to backend, frontend, qa, gitops)
- Subagent tree hierarchy, linking, and event publication (event.agentSpawned)
- Child contract scope inheritance and narrowing validation
- Result aggregation into parent record
- Parent completion blocking by non-terminal children and failure propagation
- Targeted child cancellation isolation (parent and siblings untouched)
- Parent cascade cancellation to active children (completed children preserved)
- Durable tree recovery across daemon restart
- Strict role rebinding guard enforcement with active subagents
- JSON-RPC protocol methods: agent.list, agent.cancel, agent.inspect
- DaemonClient integration over HTTP/JSON-RPC
"""

from __future__ import annotations

import pytest

from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {
        "agent_id": "claude",
        "work_order": "WO-021",
        "scope": {"allow": ["src/**", "tests/**"], "deny": ["secrets/**"]},
    }


def _request(method: str, params: dict[str, object] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_multi_role_dispatch_and_subagent_hierarchy(tmp_path):
    """Architecture agent dispatches Work Orders to specialized child roles."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    # Root architecture operation
    _, arch_op_id = manager.begin_operation(
        session_id,
        "architecture.orchestrate",
        work_order_id="WO-001",
        contract_scope={"allow": ["src/**", "tests/**"], "deny": ["secrets/**"]},
        role="architecture",
    )
    arch_op = manager.get_operation(arch_op_id)
    assert arch_op["role"] == "architecture"
    assert arch_op["agent_id"] == "architecture"
    assert arch_op["parent_operation_id"] is None
    assert arch_op["children"] == []

    # Dispatch to specialized roles
    roles = ["backend", "frontend", "qa", "gitops"]
    child_ops = {}
    for i, role in enumerate(roles, start=2):
        wo_id = f"WO-00{i}"
        child_op = manager.dispatch_subagent(
            session_id,
            parent_operation_id=arch_op_id,
            role=role,
            work_order_id=wo_id,
        )
        assert child_op["role"] == role
        assert child_op["agent_id"] == role
        assert child_op["parent_operation_id"] == arch_op_id
        assert child_op["work_order_id"] == wo_id
        # Inherited parent scope
        assert child_op["contract_scope"] == {"allow": ["src/**", "tests/**"], "deny": ["secrets/**"]}
        child_ops[role] = child_op["operation_id"]

    # Verify parent index has all children
    updated_arch = manager.get_operation(arch_op_id)
    assert len(updated_arch["children"]) == 4
    for role in roles:
        assert child_ops[role] in updated_arch["children"]

    # Verify event.agentSpawned events published
    spawn_events = [
        e for e in manager.events.events(session_id)
        if e.name == "event.agentSpawned"
    ]
    assert len(spawn_events) == 4
    spawned_roles = [e.payload["role"] for e in spawn_events]
    assert set(spawned_roles) == set(roles)


def test_invalid_role_dispatch_rejected(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, arch_op_id = manager.begin_operation(session_id, "arch_task", role="architecture")
    with pytest.raises(ValueError, match="Unknown role 'invalid_role'"):
        manager.dispatch_subagent(
            session_id,
            parent_operation_id=arch_op_id,
            role="invalid_role",
            work_order_id="WO-999",
        )


def test_contract_scope_inheritance_and_narrowing(tmp_path):
    """Child operations inherit parent scope by default, but explicit narrower scope is verified."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    parent_scope = {"allow": ["src/**", "tests/**"], "deny": ["secrets/**"]}
    _, parent_id = manager.begin_operation(
        session_id,
        "parent_task",
        contract_scope=parent_scope,
        role="architecture",
    )

    # 1. Scope inheritance via dispatch_subagent (contract_scope=None)
    child_backend = manager.dispatch_subagent(
        session_id,
        parent_operation_id=parent_id,
        role="backend",
        work_order_id="WO-002",
        contract_scope=None,
    )
    assert child_backend["contract_scope"] == parent_scope

    # 2. Scope inheritance via begin_operation with contract_scope="inherit"
    _, inherit_op_id = manager.begin_operation(
        session_id,
        "inherit_task",
        parent_operation_id=parent_id,
        contract_scope="inherit",
        role="frontend",
    )
    assert manager.get_operation(inherit_op_id)["contract_scope"] == parent_scope

    # 3. Explicit valid narrowing: subset of allow, superset of deny
    narrow_scope = {"allow": ["src/backend/**"], "deny": ["secrets/**", "src/backend/internal/**"]}
    child_narrow = manager.dispatch_subagent(
        session_id,
        parent_operation_id=parent_id,
        role="qa",
        work_order_id="WO-003",
        contract_scope=narrow_scope,
    )
    assert child_narrow["contract_scope"] == narrow_scope

    # 4. Scope expansion rejected: allow contains path outside parent allow
    expanded_scope = {"allow": ["src/**", "docs/**"], "deny": ["secrets/**"]}
    with pytest.raises(ValueError, match="out of parent allow scope"):
        manager.dispatch_subagent(
            session_id,
            parent_operation_id=parent_id,
            role="gitops",
            work_order_id="WO-004",
            contract_scope=expanded_scope,
        )


def test_result_aggregation_and_parent_completion_blocking(tmp_path):
    """Parent cannot complete while children are active or failed; child results aggregate into parent."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task", role="architecture")
    child_backend = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="backend", work_order_id="WO-002"
    )
    child_frontend = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="frontend", work_order_id="WO-003"
    )

    backend_id = child_backend["operation_id"]
    frontend_id = child_frontend["operation_id"]

    # 1. Cannot complete parent while children are active
    with pytest.raises(ValueError, match="child operation '.*' is active"):
        manager.complete_operation(parent_id, status="COMPLETED")

    # 2. Complete backend successfully
    backend_res = {"files_written": ["api.py"], "tests_passed": 12}
    manager.complete_operation(backend_id, result=backend_res, status="COMPLETED")

    # Parent still blocked by frontend child
    with pytest.raises(ValueError, match="child operation '.*' is active"):
        manager.complete_operation(parent_id, status="COMPLETED")

    # 3. Complete frontend successfully
    frontend_res = {"components_built": ["Dashboard.tsx"]}
    manager.complete_operation(frontend_id, result=frontend_res, status="COMPLETED")

    # 4. Now parent completes cleanly and aggregates child results
    parent_completed = manager.complete_operation(
        parent_id, result={"plan_status": "delivered"}, status="COMPLETED"
    )
    assert parent_completed["status"] == "COMPLETED"
    assert "child_results" in parent_completed["result"]
    assert backend_id in parent_completed["result"]["child_results"]
    assert frontend_id in parent_completed["result"]["child_results"]
    assert parent_completed["result"]["child_results"][backend_id]["result"] == backend_res
    assert parent_completed["result"]["child_results"][frontend_id]["result"] == frontend_res


def test_failed_child_prevents_parent_success(tmp_path):
    """Failed child blocks COMPLETED parent status; parent must be completed as FAILED."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task", role="architecture")
    child = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="backend", work_order_id="WO-002"
    )
    child_id = child["operation_id"]

    # Child fails
    manager.complete_operation(child_id, result={"error": "SyntaxError"}, status="FAILED")

    # Parent completion as COMPLETED is rejected
    with pytest.raises(ValueError, match="child operation '.*' failed"):
        manager.complete_operation(parent_id, status="COMPLETED")

    # Parent can complete as FAILED, preserving child failure record
    parent_failed = manager.complete_operation(parent_id, status="FAILED")
    assert parent_failed["status"] == "FAILED"
    assert parent_failed["result"]["child_results"][child_id]["status"] == "FAILED"


def test_targeted_child_cancellation_isolation(tmp_path):
    """Cancelling a specific child subagent cancels only its subtree; parent and sibling remain running."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task", role="architecture")
    child1 = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="backend", work_order_id="WO-002"
    )
    child2 = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="frontend", work_order_id="WO-003"
    )
    # Grandchild under child1
    _, grandchild_id = manager.begin_operation(
        session_id, "backend_subtask", parent_operation_id=child1["operation_id"], role="backend"
    )

    # Cancel backend agent via cancel_agent
    cancel_res = manager.cancel_agent("backend", session_id=session_id)
    assert cancel_res["canceled"] is True
    assert cancel_res["agentId"] == "backend"

    # Child 1 and grandchild are CANCEL_REQUESTED
    assert manager.get_operation(child1["operation_id"])["status"] == "CANCEL_REQUESTED"
    assert manager.get_operation(grandchild_id)["status"] == "CANCEL_REQUESTED"

    # Parent and child 2 (frontend) are untouched
    assert manager.get_operation(parent_id)["status"] == "RUNNING"
    assert manager.get_operation(child2["operation_id"])["status"] == "RUNNING"


def test_parent_cascade_cancellation(tmp_path):
    """Cancelling parent cascades to all non-terminal children; completed children remain completed."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task", role="architecture")
    child1 = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="backend", work_order_id="WO-002"
    )
    child2 = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="frontend", work_order_id="WO-003"
    )

    # Complete child1 early
    manager.complete_operation(child1["operation_id"], status="COMPLETED")

    # Cancel parent with cascade
    cancelled_parent = manager.cancel_operation(parent_id, cascade=True)
    assert cancelled_parent["status"] == "CANCEL_REQUESTED"

    # Completed child1 was not overwritten
    assert manager.get_operation(child1["operation_id"])["status"] == "COMPLETED"

    # Running child2 was cascaded to CANCEL_REQUESTED
    assert manager.get_operation(child2["operation_id"])["status"] == "CANCEL_REQUESTED"


def test_subagent_tree_durability_across_daemon_restart(tmp_path):
    """Hierarchy, roles, work orders, scopes, and aggregated child results persist and reload cleanly."""
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, p_id = manager.begin_operation(
        session_id,
        "parent_op",
        work_order_id="WO-001",
        contract_scope={"allow": ["src/**"]},
        role="architecture",
    )
    c1 = manager.dispatch_subagent(
        session_id, parent_operation_id=p_id, role="backend", work_order_id="WO-002"
    )
    c2 = manager.dispatch_subagent(
        session_id, parent_operation_id=p_id, role="frontend", work_order_id="WO-003"
    )

    manager.complete_operation(
        c1["operation_id"], result={"status": "backend_ready"}, status="COMPLETED"
    )

    # Simulate daemon crash & restart
    restarted_manager = SessionManager(storage)

    # Verify parent
    p_rec = restarted_manager.get_operation(p_id)
    assert p_rec["role"] == "architecture"
    assert p_rec["work_order_id"] == "WO-001"
    assert p_rec["children"] == [c1["operation_id"], c2["operation_id"]]
    assert c1["operation_id"] in p_rec["child_results"]
    assert p_rec["child_results"][c1["operation_id"]]["result"] == {"status": "backend_ready"}

    # Verify children
    c1_rec = restarted_manager.get_operation(c1["operation_id"])
    assert c1_rec["parent_operation_id"] == p_id
    assert c1_rec["role"] == "backend"
    assert c1_rec["status"] == "COMPLETED"

    c2_rec = restarted_manager.get_operation(c2["operation_id"])
    assert c2_rec["parent_operation_id"] == p_id
    assert c2_rec["role"] == "frontend"
    assert c2_rec["status"] == "RUNNING"


def test_active_subagent_blocks_role_backend_rebinding(tmp_path):
    """Strict Rebinding Guard: cannot rebind a role that has an active subagent operation."""
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task", role="architecture")
    child = manager.dispatch_subagent(
        session_id, parent_operation_id=parent_id, role="backend", work_order_id="WO-002"
    )

    # Attempt to rebind 'backend' while child operation is active -> rejected
    with pytest.raises(ValueError, match="Cannot rebind role 'backend'"):
        manager.configure_role_backend("backend", "codex-adapter", model="gpt-4o")

    # Complete the child
    manager.complete_operation(child["operation_id"], status="COMPLETED")

    # Now rebinding is allowed
    configured = manager.configure_role_backend("backend", "codex-adapter", model="gpt-4o")
    assert configured["backend"] == "codex-adapter"


def test_jsonrpc_agent_endpoints(tmp_path):
    """Validate agent.list, agent.cancel, and agent.inspect over JSON-RPC."""
    protocol = JsonRpcProtocol(SessionManager(DaemonStorage(tmp_path)))
    session_res = protocol.handle(
        _request("session.create", {"agent": "claude", "provider": "test", "contract": _contract(), "workspace": "workspace"})
    )["result"]
    session_id = session_res["session_id"]

    # Begin parent
    parent_op = protocol.handle(
        _request(
            "operation.begin",
            {
                "session_id": session_id,
                "operation": "architecture.plan",
                "work_order_id": "WO-001",
                "role": "architecture",
                "contract_scope": ["src/**"],
            },
        )
    )["result"]
    parent_id = parent_op["operation_id"]

    # Begin child backend
    child_op = protocol.handle(
        _request(
            "operation.begin",
            {
                "session_id": session_id,
                "operation": "backend.build",
                "parent_operation_id": parent_id,
                "work_order_id": "WO-002",
                "role": "backend",
                "contract_scope": ["src/**"],
            },
        )
    )["result"]
    child_id = child_op["operation_id"]

    # 1. agent.list
    list_res = protocol.handle(_request("agent.list", {"sessionId": session_id}))["result"]
    assert "agents" in list_res
    agent_roles = {a["role"] for a in list_res["agents"]}
    assert "architecture" in agent_roles
    assert "backend" in agent_roles

    # 2. agent.inspect
    inspect_res = protocol.handle(
        _request("agent.inspect", {"sessionId": session_id, "agentId": "backend"})
    )["result"]
    assert inspect_res["agentId"] == "backend"
    assert inspect_res["role"] == "backend"
    assert inspect_res["operationId"] == child_id
    assert inspect_res["workOrderId"] == "WO-002"
    assert inspect_res["state"] == "RUNNING"

    # 3. agent.cancel
    cancel_res = protocol.handle(
        _request("agent.cancel", {"sessionId": session_id, "agentId": "backend", "reason": "user_cancelled"})
    )["result"]
    assert cancel_res["canceled"] is True
    assert cancel_res["agentId"] == "backend"

    # Verify state via operation.get
    cancelled_op = protocol.handle(_request("operation.get", {"operation_id": child_id}))["result"]
    assert cancelled_op["status"] == "CANCEL_REQUESTED"


def test_daemon_client_agent_integration(tmp_path):
    """Validate list_agents, cancel_agent, and inspect_agent on DaemonClient."""
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="test",
            contract=_contract(),
            workspace="workspace",
        )
        session_id = session["session_id"]

        parent = client.operation_begin(
            session_id,
            "arch_task",
            work_order_id="WO-001",
            role="architecture",
            contract_scope=["src/**"],
        )
        parent_id = parent["operation_id"]

        child = client.operation_begin(
            session_id,
            "frontend_task",
            parent_operation_id=parent_id,
            work_order_id="WO-003",
            role="frontend",
            contract_scope=["src/**"],
        )
        child_id = child["operation_id"]

        # 1. list_agents
        agents = client.list_agents(session_id=session_id)
        assert isinstance(agents, list)
        roles = {a["role"] for a in agents}
        assert "architecture" in roles
        assert "frontend" in roles

        # 2. inspect_agent
        inspection = client.inspect_agent("frontend", session_id=session_id)
        assert inspection["agentId"] == "frontend"
        assert inspection["operationId"] == child_id
        assert inspection["state"] == "RUNNING"

        # 3. cancel_agent
        cancelled = client.cancel_agent("frontend", session_id=session_id)
        assert cancelled["canceled"] is True
        assert client.operation_get(child_id)["status"] == "CANCEL_REQUESTED"
