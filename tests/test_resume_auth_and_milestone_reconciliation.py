"""Tests for TUI :resume authentication and uncovered milestone synthesis in plan reconciliation."""

import yaml
from pathlib import Path
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.tui.client import DaemonClient
from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan
from validators.harness.authoring_readiness import validate_authoring_readiness, load_plan_milestones


def test_daemon_client_injects_auth_token_for_admin_methods():
    client = DaemonClient(base_url="http://127.0.0.1:8765", auth_token="test-secret-token")
    assert client.auth_token == "test-secret-token"

    # Verify admin methods set auth_token
    called_params = {}

    def mock_call(method: str, **params):
        called_params["method"] = method
        called_params["params"] = params
        return {"status": "ok"}

    client.call = mock_call

    # Test run_resume
    client.run_resume("sess-123", run_id="run-456")
    assert called_params["method"] == "run.resume"
    assert called_params["params"]["auth_token"] == "test-secret-token"
    assert called_params["params"]["session_id"] == "sess-123"
    assert called_params["params"]["run_id"] == "run-456"

    # Test run_approve
    client.run_approve("sess-123", reason="looks good")
    assert called_params["method"] == "run.approve"
    assert called_params["params"]["auth_token"] == "test-secret-token"

    # Test run_reject
    client.run_reject("sess-123", reason="needs fix")
    assert called_params["method"] == "run.reject"
    assert called_params["params"]["auth_token"] == "test-secret-token"


def test_protocol_accepts_daemon_client_admin_auth(tmp_path: Path):
    admin_token = "secure-daemon-token-998877"
    storage = DaemonStorage(tmp_path / "daemon_storage")
    manager = SessionManager(storage)
    protocol = JsonRpcProtocol(manager, auth_token=admin_token)

    # 1. Without auth_token -> rejected
    unauth_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "run.resume",
        "params": {"session_id": "nonexistent-sess"},
    }
    resp = protocol.handle(unauth_req)
    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert "Policy denied" in resp["error"]["message"]

    # 2. With auth_token -> accepted through protocol auth check
    auth_req = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "run.resume",
        "params": {"session_id": "nonexistent-sess", "auth_token": admin_token},
    }
    resp_auth = protocol.handle(auth_req)
    # Auth passed, so it reached SessionManager (unknown session error)
    assert resp_auth["error"]["code"] == -32003
    assert "unknown session" in resp_auth["error"]["message"].lower()


def test_reconcile_active_work_orders_synthesizes_uncovered_milestones(tmp_path: Path):
    ws = tmp_path
    plan_content = """# Project Plan: test-service

## Current Architecture
Initial architecture.

Stack & Dependencies:
- Backend: Python, FastAPI
- Dependency: requirements.txt

## Milestones & Roadmap
- [ ] Milestone 1: Core Service Setup (Agent: codex)
  - [ ] Task 1.1: Initialize project
- [ ] Milestone 2: Frontend Dashboard (Agent: gemini)
  - [ ] Task 2.1: Implement dashboard
- [ ] Milestone 3: Production Deployment (Agent: local-llm)
  - [ ] Task 3.1: Configure release and deployment
"""
    (ws / "PLAN.md").write_text(plan_content, encoding="utf-8")
    act_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"
    act_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Only Milestone 1 and Milestone 2 exist on disk; Milestone 3 is missing
    wo1 = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Core Service Setup",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "milestone_id": "Milestone 1",
        "deliverable": {"type": "code", "path": "src/service.py", "description": "Core service logic"},
    }
    wo2 = {
        "id": "WO-002",
        "type": "FEATURE",
        "title": "Frontend Dashboard",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemini"],
        "dependencies": ["WO-001"],
        "milestone_id": "Milestone 2",
        "deliverable": {"type": "code", "path": "src/Dashboard.jsx", "description": "Dashboard component"},
    }
    (act_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")
    (act_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

    c1 = {"agent_id": "codex", "work_order": "WO-001", "identity": {"role": "backend"}}
    c2 = {"agent_id": "gemini", "work_order": "WO-002", "identity": {"role": "frontend"}}
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(c1), encoding="utf-8")
    (contracts_dir / "WO-002.yaml").write_text(yaml.safe_dump(c2), encoding="utf-8")

    # Before reconciliation: Milestone 3 is uncovered -> readiness reports MILESTONE_UNCOVERED
    plan_milestones = load_plan_milestones(ws)
    r_before = validate_authoring_readiness(ws, expected_milestones=plan_milestones)
    assert not r_before.ready
    assert any(i.code == "MILESTONE_UNCOVERED" and "Production Deployment" in i.message for i in r_before.issues)

    # Run reconciliation
    reconciled = reconcile_active_work_orders_with_plan(ws)
    assert "WO-003" in reconciled

    # Check WO-003 was created on disk
    wo3_file = act_dir / "WO-003.yaml"
    c3_file = contracts_dir / "WO-003.yaml"
    assert wo3_file.is_file()
    assert c3_file.is_file()

    wo3_data = yaml.safe_load(wo3_file.read_text(encoding="utf-8"))
    assert wo3_data["id"] == "WO-003"
    assert wo3_data["assigned_agents"] == ["local-llm"]
    assert wo3_data["milestone_id"] == "Milestone 3"

    # After reconciliation: Milestone 3 is covered, no MILESTONE_UNCOVERED issue remains
    r_after = validate_authoring_readiness(ws, expected_milestones=plan_milestones)
    assert not any(i.code == "MILESTONE_UNCOVERED" for i in r_after.issues)


def test_start_turn_resolves_work_order_agent_when_omitted(tmp_path: Path):
    storage = DaemonStorage(tmp_path / "daemon_storage")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]

    contracts_dir = tmp_path / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    c1 = {"agent_id": "codex", "work_order": "WO-001", "identity": {"role": "backend"}}
    (contracts_dir / "WO-001.yaml").write_text(yaml.safe_dump(c1), encoding="utf-8")

    # Start turn targeting WO-001 without role or agent_id (resolved from contract)
    op = manager.start_turn(session_id, "Execute WO-001", work_order_id="WO-001")
    op_rec = manager.get_operation(op["operation_id"])
    assert op_rec["role"] == "backend"
    assert op_rec["agent_id"] == "codex"
    manager.complete_operation(session_id, op["operation_id"], {"status": "completed"})

    # Test WO-002 resolved from ACTIVE work order file (no contract file)
    act_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    act_dir.mkdir(parents=True, exist_ok=True)
    wo2 = {"id": "WO-002", "assigned_agents": ["gemini"]}
    (act_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

    op2 = manager.start_turn(session_id, "Execute WO-002", work_order_id="WO-002")
    op_rec2 = manager.get_operation(op2["operation_id"])
    assert op_rec2["role"] == "frontend"
    assert op_rec2["agent_id"] == "gemini"
    manager.complete_operation(session_id, op2["operation_id"], {"status": "completed"})
