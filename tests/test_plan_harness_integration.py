"""Comprehensive tests for Plan Lifecycle, Harness Integration, and Work Order Execution (P7-1)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from cli.init import init
from validators.harness.runner import (
    AgentRunner,
    CompletionRecord,
    EchoLLMProvider,
    LLMRequest,
)
from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-019", "scope": {"allow": ["tests/**"]}}


def _request(method: str, params: dict[str, object] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _init_test_project(tmp_path: Path) -> Path:
    project = tmp_path / "test-project"
    init(project, name="TestProject", no_git=True)
    return project


def test_plan_proposal_enters_awaiting_approval_and_emits_event(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    plan = manager.propose_plan(
        session_id,
        "plan-alpha",
        "Alpha Architectural Plan",
        content="Architecture specification for P7 runtime",
        metadata={"work_orders": [{"id": "WO-101", "title": "Implement core"}]},
    )

    assert plan["plan_id"] == "plan-alpha"
    assert plan["state"] == "AWAITING_APPROVAL"
    assert plan["title"] == "Alpha Architectural Plan"
    assert plan["content"] == "Architecture specification for P7 runtime"
    assert plan["feedback"] is None

    # Verify retrieval
    retrieved = manager.get_plan(session_id, "plan-alpha")
    assert retrieved["state"] == "AWAITING_APPROVAL"

    latest = manager.get_plan(session_id)
    assert latest["plan_id"] == "plan-alpha"

    # Verify event emission
    event_names = [e.name for e in manager.events.events(session_id)]
    assert "plan.proposed" in event_names


def test_plan_approve_creates_work_orders_and_emits_event(tmp_path):
    project = _init_test_project(tmp_path)
    manager = SessionManager(DaemonStorage(tmp_path / "daemon-state"))
    session = manager.create_session("claude", "test", _contract(), str(project))
    session_id = session["session_id"]

    manager.propose_plan(
        session_id,
        "plan-beta",
        "Beta Engineering Plan",
        metadata={
            "work_orders": [
                {
                    "id": "WO-201",
                    "title": "Backend Service",
                    "type": "FEATURE",
                    "description": "Implement service",
                },
                "WO-202",
            ]
        },
    )

    created_wos = manager.approve_plan(session_id, "plan-beta", reason="Architect approved")

    assert len(created_wos) == 2
    assert created_wos[0]["id"] == "WO-201"
    assert created_wos[0]["status"] == "ACTIVE"
    assert created_wos[0]["title"] == "Backend Service"
    assert created_wos[1]["id"] == "WO-202"
    assert created_wos[1]["status"] == "ACTIVE"

    # Verify plan state transition
    plan = manager.get_plan(session_id, "plan-beta")
    assert plan["state"] == "APPROVED"
    assert plan["approval_reason"] == "Architect approved"
    assert len(plan["created_work_orders"]) == 2

    # Verify files created on disk
    wo201_file = project / ".sync" / "work-orders" / "ACTIVE" / "WO-201.yaml"
    wo202_file = project / ".sync" / "work-orders" / "ACTIVE" / "WO-202.yaml"
    assert wo201_file.exists()
    assert wo202_file.exists()
    data201 = yaml.safe_load(wo201_file.read_text(encoding="utf-8"))
    assert data201["title"] == "Backend Service"

    # Verify event
    events = manager.events.events(session_id)
    approved_events = [e for e in events if e.name == "plan.approved"]
    assert len(approved_events) == 1
    assert approved_events[0].payload["plan_id"] == "plan-beta"
    assert approved_events[0].payload["work_orders"] == ["WO-201", "WO-202"]


def test_plan_reject_stores_feedback_emits_event_creates_zero_work_orders(tmp_path):
    project = _init_test_project(tmp_path)
    manager = SessionManager(DaemonStorage(tmp_path / "daemon-state"))
    session = manager.create_session("claude", "test", _contract(), str(project))
    session_id = session["session_id"]

    manager.propose_plan(
        session_id,
        "plan-gamma",
        "Gamma Engineering Plan",
        metadata={"work_orders": [{"id": "WO-301", "title": "Unwanted Work"}]},
    )

    rejected_plan = manager.reject_plan(
        session_id, "plan-gamma", reason="Security review failed: missing sandbox"
    )

    assert rejected_plan["state"] == "REJECTED"
    assert rejected_plan["feedback"] == "Security review failed: missing sandbox"
    assert rejected_plan["created_work_orders"] == []

    # Verify ZERO work orders created on disk
    wo301_file = project / ".sync" / "work-orders" / "ACTIVE" / "WO-301.yaml"
    assert not wo301_file.exists()

    # Verify event
    events = manager.events.events(session_id)
    rejected_events = [e for e in events if e.name == "plan.rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0].payload["plan_id"] == "plan-gamma"
    assert rejected_events[0].payload["reason"] == "Security review failed: missing sandbox"


def test_invalid_plan_state_raises_value_error_and_rpc_policy_denied(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    protocol = JsonRpcProtocol(manager)
    session = manager.create_session("claude", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    manager.propose_plan(session_id, "p-test", "Test Plan")

    # Reject plan
    manager.reject_plan(session_id, "p-test", reason="Not ready")

    # Calling approve on a REJECTED plan raises ValueError
    with pytest.raises(ValueError, match="Plan is not in AWAITING_APPROVAL state"):
        manager.approve_plan(session_id, "p-test")

    # Over JSON-RPC: returns error -32003
    rpc_res = protocol.handle(_request("plan.approve", {"session_id": session_id, "plan_id": "p-test"}))
    assert "error" in rpc_res
    assert rpc_res["error"]["code"] == -32003
    assert "AWAITING_APPROVAL" in rpc_res["error"]["message"]

    # Calling reject on an already REJECTED plan raises ValueError
    with pytest.raises(ValueError, match="Plan is not in AWAITING_APPROVAL state"):
        manager.reject_plan(session_id, "p-test")

    rpc_res_reject = protocol.handle(_request("plan.reject", {"session_id": session_id, "plan_id": "p-test"}))
    assert rpc_res_reject["error"]["code"] == -32003

    # Revise back to AWAITING_APPROVAL, approve, then test error on APPROVED plan
    manager.propose_plan(session_id, "p-test", "Revised Plan")
    manager.approve_plan(session_id, "p-test")

    with pytest.raises(ValueError, match="Plan is not in AWAITING_APPROVAL state"):
        manager.approve_plan(session_id, "p-test")

    rpc_res_approved = protocol.handle(_request("plan.approve", {"session_id": session_id, "plan_id": "p-test"}))
    assert rpc_res_approved["error"]["code"] == -32003


def test_rejection_revision_resubmission_loop(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("claude", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    # 1. Initial Proposal
    manager.propose_plan(
        session_id,
        "plan-loop",
        "Initial Title",
        content="v1 draft",
        metadata={"work_orders": ["WO-001"]},
    )
    p1 = manager.get_plan(session_id, "plan-loop")
    assert p1["state"] == "AWAITING_APPROVAL"

    # 2. Rejection
    manager.reject_plan(session_id, "plan-loop", reason="Needs more detail on schema")
    p2 = manager.get_plan(session_id, "plan-loop")
    assert p2["state"] == "REJECTED"
    assert p2["feedback"] == "Needs more detail on schema"

    # 3. Revision
    manager.propose_plan(
        session_id,
        "plan-loop",
        "Revised Title",
        content="v2 complete specification",
        metadata={"work_orders": [{"id": "WO-001", "title": "Schema migration"}]},
    )
    p3 = manager.get_plan(session_id, "plan-loop")
    assert p3["state"] == "AWAITING_APPROVAL"
    assert p3["title"] == "Revised Title"
    assert p3["feedback"] is None
    assert len(p3.get("revisions", [])) == 1

    # 4. Approval
    created_wos = manager.approve_plan(session_id, "plan-loop", reason="Approved v2")
    p4 = manager.get_plan(session_id, "plan-loop")
    assert p4["state"] == "APPROVED"
    assert len(created_wos) == 1


def test_plan_persistence_across_daemon_restart(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("claude", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    manager.propose_plan(
        session_id,
        "durable-plan",
        "Persistent Plan",
        content="Must survive restart",
        metadata={"work_orders": [{"id": "WO-999", "title": "Durable task"}]},
    )
    manager.approve_plan(session_id, "durable-plan", reason="Permanent approval")

    # Restart daemon / reload manager from storage
    recovered_manager = SessionManager(storage)
    recovered_plan = recovered_manager.get_plan(session_id, "durable-plan")

    assert recovered_plan["plan_id"] == "durable-plan"
    assert recovered_plan["state"] == "APPROVED"
    assert recovered_plan["title"] == "Persistent Plan"
    assert recovered_plan["approval_reason"] == "Permanent approval"
    assert len(recovered_plan["created_work_orders"]) == 1
    assert recovered_plan["created_work_orders"][0]["id"] == "WO-999"


def test_daemon_client_plan_lifecycle_methods(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)
        session = client.create_session(
            agent="claude",
            provider="test",
            contract=_contract(),
            workspace=str(tmp_path),
        )
        session_id = session["session_id"]

        # 1. Propose plan via client
        proposed = client.plan_propose(
            session_id,
            "client-plan",
            "Client Proposed Plan",
            content="Client body",
            metadata={"work_orders": [{"id": "WO-CLIENT-1", "title": "Client WO"}]},
        )
        assert proposed["state"] == "AWAITING_APPROVAL"

        # 2. Get plan via client
        retrieved = client.plan_get(session_id, "client-plan")
        assert retrieved["plan_id"] == "client-plan"

        # 3. Reject plan via client
        rejected = client.plan_reject(session_id, "client-plan", reason="Not aligned")
        assert rejected["state"] == "REJECTED"

        # 4. Attempt approve on rejected raises RuntimeError (-32003)
        with pytest.raises(RuntimeError, match="Plan is not in AWAITING_APPROVAL state"):
            client.plan_approve(session_id, "client-plan")

        # 5. Revise and approve via client
        client.plan_propose(
            session_id,
            "client-plan",
            "Client Revised Plan",
            metadata={"work_orders": [{"id": "WO-CLIENT-1", "title": "Client WO Updated"}]},
        )
        approved_wos = client.plan_approve(session_id, "client-plan", reason="Good now")
        assert len(approved_wos) == 1
        assert approved_wos[0]["id"] == "WO-CLIENT-1"


def test_governed_work_order_execution_through_agent_runner(tmp_path):
    project = _init_test_project(tmp_path)
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-500"]
    tree["agents"]["codex"]["status"] = "assigned"
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    # Author work order
    wo_dir = project / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_payload = {
        "id": "WO-500",
        "title": "Governed Harness Work Order",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "description": "Task to execute through AgentRunner",
        "deliverable": {
            "type": "module",
            "path": "src/feature.py",
            "description": "Feature implementation",
        },
    }
    (wo_dir / "WO-500.yaml").write_text(yaml.safe_dump(wo_payload, sort_keys=False), encoding="utf-8")

    index_path = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    index_data["orders"].append({
        "id": "WO-500",
        "type": "FEATURE",
        "title": "Governed Harness Work Order",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {
            "type": "module",
            "path": "src/feature.py",
            "description": "Feature implementation",
        },
        "file": "work-orders/ACTIVE/WO-500.yaml",
    })
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    # Author agent contract for WO-500
    contract_dir = project / ".sync" / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    contract_payload = {
        "id": "WO-500",
        "agent": "codex",
        "role": "Backend Lead",
        "session_id": "codex-session-test",
        "task": {"wo_id": "WO-500", "title": "Governed Harness Work Order"},
        "scope": {"allow": [{"path": "src/feature.py", "ops": ["read", "write"]}]},
        "budget": {"max_tokens": 10000, "max_files_touched": 2},
    }
    (contract_dir / "WO-500.yaml").write_text(yaml.safe_dump(contract_payload, sort_keys=False), encoding="utf-8")

    # Create dummy src/feature.py for deliverable
    src_dir = project / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "feature.py").write_text("# Feature\n", encoding="utf-8")

    class CustomLLMProvider:
        provider_name = "custom"
        model_name = "custom-v1"

        def complete(self, request: LLMRequest) -> CompletionRecord:
            return CompletionRecord(
                provider=self.provider_name,
                model=self.model_name,
                payload={
                    "status": "completed",
                    "summary": f"Completed {request.task.identifier}",
                    "report_markdown": "# Done",
                    "blockers": [],
                    "modified_files": ["src/feature.py"],
                    "release_target": "v1.0.0",
                    "retrieval_queries": [],
                    "uncertainty": [],
                    "commands": [],
                },
            )

    runner = AgentRunner(
        project,
        "codex",
        llm_provider=CustomLLMProvider(),
        now_fn=lambda: datetime.fromisoformat("2026-09-11T22:30:00+05:30"),
    )
    result = runner.run_once()

    assert result.status == "completed", f"Runner failed with reason: {result.reason}"
    assert result.persisted is True
    assert result.task_id == "WO-500"
