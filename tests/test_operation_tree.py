"""Comprehensive tests for hierarchical operation tree and governed agent roles (P7-0)."""

from __future__ import annotations

import pytest

from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-018", "scope": {"allow": ["tests/**"]}}


def _request(method: str, params: dict[str, object] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_operation_tree_child_creation_and_parent_indexing(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(
        session_id,
        "parent_op",
        work_order_id="WO-018",
        contract_scope=["tests/**"],
    )
    parent = manager.get_operation(parent_id)
    assert parent["parent_operation_id"] is None
    assert parent["children"] == []
    assert parent["work_order_id"] == "WO-018"

    _, child1_id = manager.begin_operation(
        session_id,
        "child_op_1",
        parent_operation_id=parent_id,
        contract_scope=["tests/unit/**"],
    )
    _, child2_id = manager.begin_operation(
        session_id,
        "child_op_2",
        parent_operation_id=parent_id,
        contract_scope=["tests/e2e/**"],
    )

    child1 = manager.get_operation(child1_id)
    assert child1["parent_operation_id"] == parent_id
    assert child1["children"] == []

    parent_updated = manager.get_operation(parent_id)
    assert parent_updated["children"] == [child1_id, child2_id]

    children = manager.list_children(parent_id)
    assert len(children) == 2
    assert [c["operation_id"] for c in children] == [child1_id, child2_id]

    # Grandchild creation
    _, grandchild_id = manager.begin_operation(
        session_id,
        "grandchild_op",
        parent_operation_id=child1_id,
        contract_scope=["tests/unit/core/**"],
    )
    assert manager.get_operation(child1_id)["children"] == [grandchild_id]
    assert manager.get_operation(grandchild_id)["parent_operation_id"] == child1_id


def test_child_operation_requires_valid_non_terminal_parent(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    # Non-existent parent
    with pytest.raises(KeyError, match="parent operation 'missing' not found"):
        manager.begin_operation(session_id, "orphan_op", parent_operation_id="missing")

    # Completed parent cannot spawn children
    _, parent_id = manager.begin_operation(session_id, "terminal_parent")
    manager.complete_operation(parent_id, status="COMPLETED")
    with pytest.raises(ValueError, match="is terminal"):
        manager.begin_operation(session_id, "child_of_dead_parent", parent_operation_id=parent_id)


def test_parent_cascade_cancellation_recursively(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "root_op")
    _, child1_id = manager.begin_operation(session_id, "child_1", parent_operation_id=parent_id)
    _, grandchild_id = manager.begin_operation(session_id, "grandchild_1", parent_operation_id=child1_id)
    _, child2_id = manager.begin_operation(session_id, "child_2", parent_operation_id=parent_id)

    # Complete child2 early
    manager.complete_operation(child2_id, status="COMPLETED")

    # Cancel root operation with cascade=True
    cancelled_parent = manager.cancel_operation(parent_id, cascade=True)
    assert cancelled_parent["status"] == "CANCEL_REQUESTED"
    assert manager.get_operation(child1_id)["status"] == "CANCEL_REQUESTED"
    assert manager.get_operation(grandchild_id)["status"] == "CANCEL_REQUESTED"
    # child2 was already COMPLETED; state shouldn't be overridden
    assert manager.get_operation(child2_id)["status"] == "COMPLETED"

    # Completing cancel-requested operations transitions them to CANCELLED
    manager.complete_operation(child1_id)
    manager.complete_operation(grandchild_id)
    manager.complete_operation(parent_id)
    assert manager.get_operation(child1_id)["status"] == "CANCELLED"
    assert manager.get_operation(grandchild_id)["status"] == "CANCELLED"
    assert manager.get_operation(parent_id)["status"] == "CANCELLED"


def test_child_cancellation_isolation(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "root_op")
    _, child1_id = manager.begin_operation(session_id, "child_1", parent_operation_id=parent_id)
    _, child2_id = manager.begin_operation(session_id, "child_2", parent_operation_id=parent_id)

    # Cancel child1 independently
    cancelled_child = manager.cancel_operation(child1_id, cascade=True)
    assert cancelled_child["status"] == "CANCEL_REQUESTED"

    # Parent and child2 remain running
    assert manager.get_operation(parent_id)["status"] == "RUNNING"
    assert manager.get_operation(child2_id)["status"] == "RUNNING"


def test_contract_scope_narrowing(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    # 1. List-based parent scope
    _, p1_id = manager.begin_operation(
        session_id, "p1", contract_scope=["validators/kernel/**"]
    )
    # Valid narrowing
    manager.begin_operation(
        session_id, "c1_valid", parent_operation_id=p1_id, contract_scope=["validators/kernel/daemon/**"]
    )
    # Scope expansion rejected
    with pytest.raises(ValueError, match="out of parent allow scope"):
        manager.begin_operation(
            session_id, "c1_invalid_allow", parent_operation_id=p1_id, contract_scope=["cli/**"]
        )
    # Missing/None scope rejected when parent has scope
    with pytest.raises(ValueError, match="Child contract_scope cannot be None"):
        manager.begin_operation(session_id, "c1_missing_scope", parent_operation_id=p1_id, contract_scope=None)

    # Clean up session 1
    manager.cancel_operation(p1_id, cascade=True)
    for op in manager.list_operations(session_id):
        if op["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
            manager.complete_operation(op["operation_id"])

    # 2. Dict-based parent scope with allow and deny in a fresh session
    session2 = manager.create_session("codex", "test", _contract(), "workspace")
    session2_id = session2["session_id"]
    _, p2_id = manager.begin_operation(
        session2_id,
        "p2",
        contract_scope={"allow": ["src/**"], "deny": ["src/private/**"]},
    )
    # Valid child dict scope: subsets allow, supersets deny
    manager.begin_operation(
        session2_id,
        "c2_valid",
        parent_operation_id=p2_id,
        contract_scope={"allow": ["src/public/**"], "deny": ["src/private/**", "src/public/secret/**"]},
    )
    # Invalid: child drops parent deny pattern
    with pytest.raises(ValueError, match="Child contract_scope must include all parent deny patterns"):
        manager.begin_operation(
            session2_id,
            "c2_dropped_deny",
            parent_operation_id=p2_id,
            contract_scope={"allow": ["src/public/**"], "deny": []},
        )
    # Invalid: child expands allow
    with pytest.raises(ValueError, match="out of parent allow scope"):
        manager.begin_operation(
            session2_id,
            "c2_expanded_allow",
            parent_operation_id=p2_id,
            contract_scope={"allow": ["src/**", "other/**"], "deny": ["src/private/**"]},
        )


def test_parent_completion_blocked_by_active_children(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task")
    _, child_id = manager.begin_operation(session_id, "child_task", parent_operation_id=parent_id)

    # Attempt to complete parent while child is RUNNING
    with pytest.raises(ValueError, match="child operation '.*' is active"):
        manager.complete_operation(parent_id, status="COMPLETED")

    # Complete child
    manager.complete_operation(child_id, status="COMPLETED")

    # Now parent completes cleanly
    completed = manager.complete_operation(parent_id, status="COMPLETED")
    assert completed["status"] == "COMPLETED"


def test_parent_completion_blocked_by_failed_child(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, parent_id = manager.begin_operation(session_id, "parent_task")
    _, child_id = manager.begin_operation(session_id, "child_task", parent_operation_id=parent_id)

    # Child fails
    manager.complete_operation(child_id, status="FAILED")

    # Parent completion as COMPLETED is rejected
    with pytest.raises(ValueError, match="cannot complete operation '.*' as COMPLETED: child operation '.*' failed"):
        manager.complete_operation(parent_id, status="COMPLETED")

    # But parent can complete as FAILED
    failed_parent = manager.complete_operation(parent_id, status="FAILED")
    assert failed_parent["status"] == "FAILED"


def test_operation_tree_journal_recovery(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    _, p_id = manager.begin_operation(session_id, "parent_op", work_order_id="WO-018")
    _, c1_id = manager.begin_operation(session_id, "child_1", parent_operation_id=p_id)
    _, c2_id = manager.begin_operation(session_id, "child_2", parent_operation_id=p_id)
    _, gc_id = manager.begin_operation(session_id, "grandchild_1", parent_operation_id=c1_id)

    manager.complete_operation(gc_id, status="COMPLETED")

    # Reconstruct manager from same storage
    recovered_manager = SessionManager(storage)
    recovered_parent = recovered_manager.get_operation(p_id)
    assert recovered_parent["children"] == [c1_id, c2_id]
    assert recovered_parent["work_order_id"] == "WO-018"

    recovered_c1 = recovered_manager.get_operation(c1_id)
    assert recovered_c1["parent_operation_id"] == p_id
    assert recovered_c1["children"] == [gc_id]

    children = recovered_manager.list_children(p_id)
    assert len(children) == 2
    assert [c["operation_id"] for c in children] == [c1_id, c2_id]

    gc_record = recovered_manager.get_operation(gc_id)
    assert gc_record["parent_operation_id"] == c1_id
    assert gc_record["status"] == "COMPLETED"


def test_retry_operation_creates_distinct_id_maintains_work_order(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), "workspace")
    session_id = session["session_id"]

    # First attempt
    _, op1_id = manager.begin_operation(session_id, "attempt_1", work_order_id="WO-018")
    manager.complete_operation(op1_id, status="FAILED")

    # Retry attempt
    _, op2_id = manager.begin_operation(session_id, "attempt_2_retry", work_order_id="WO-018")
    manager.complete_operation(op2_id, status="COMPLETED")

    assert op1_id != op2_id
    op1 = manager.get_operation(op1_id)
    op2 = manager.get_operation(op2_id)
    assert op1["work_order_id"] == "WO-018"
    assert op2["work_order_id"] == "WO-018"
    assert op1["status"] == "FAILED"
    assert op2["status"] == "COMPLETED"


def test_jsonrpc_and_client_operation_tree_methods(tmp_path):
    protocol = JsonRpcProtocol(SessionManager(DaemonStorage(tmp_path)))
    created = protocol.handle(
        _request(
            "session.create",
            {"agent": "codex", "provider": "test", "contract": _contract(), "workspace": "workspace"},
        )
    )["result"]
    session_id = created["session_id"]

    parent_op = protocol.handle(
        _request(
            "operation.begin",
            {
                "session_id": session_id,
                "operation": "parent_op",
                "work_order_id": "WO-018",
                "contract_scope": ["tests/**"],
            },
        )
    )["result"]
    parent_id = parent_op["operation_id"]

    child_op = protocol.handle(
        _request(
            "operation.begin",
            {
                "session_id": session_id,
                "operation": "child_op",
                "parent_operation_id": parent_id,
                "contract_scope": ["tests/unit/**"],
            },
        )
    )["result"]
    child_id = child_op["operation_id"]

    # Test operation.children
    children = protocol.handle(_request("operation.children", {"operation_id": parent_id}))["result"]
    assert len(children) == 1
    assert children[0]["operation_id"] == child_id
    assert children[0]["parent_operation_id"] == parent_id

    # Test operation.cancel with default cascade=True
    cancelled = protocol.handle(_request("operation.cancel", {"operation_id": parent_id}))["result"]
    assert cancelled["status"] == "CANCEL_REQUESTED"
    child_state = protocol.handle(_request("operation.get", {"operation_id": child_id}))["result"]
    assert child_state["status"] == "CANCEL_REQUESTED"


def test_daemon_client_operation_children_and_cascade(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="codex",
            provider="test",
            contract=_contract(),
            workspace="workspace",
        )
        session_id = session["session_id"]

        parent = client.operation_begin(
            session_id,
            "parent_task",
            work_order_id="WO-018",
            contract_scope=["tests/**"],
        )
        parent_id = parent["operation_id"]

        child = client.operation_begin(
            session_id,
            "child_task",
            parent_operation_id=parent_id,
            contract_scope=["tests/unit/**"],
        )
        child_id = child["operation_id"]

        children = client.operation_children(parent_id)
        assert len(children) == 1
        assert children[0]["operation_id"] == child_id

        # Cascade cancel via client
        cancelled = client.operation_cancel(parent_id)
        assert cancelled["status"] == "CANCEL_REQUESTED"
        assert client.operation_get(child_id)["status"] == "CANCEL_REQUESTED"
