"""Tests for Phase 2: Contract Change Request (CCR) Protocol (Checklist C.1).

Verifies:
1. Worker agents (codex, gemini) can request contract changes via request_contract_change().
2. The request is assigned a sequential CCR-xxx ID and conforms to schemas/contract-change-request.schema.json.
3. Workers cannot evaluate or approve their own contract change requests (enforced by role policy).
4. The Lead Architect (claude) can evaluate and approve a request:
   - Updates contract allow rules and bumps contract revision.
   - Updates CCR status and stores architect response.
   - Emits an inbox notification to the requesting agent.
5. The Lead Architect can evaluate and reject a request:
   - Updates CCR status to REJECTED.
   - Emits an inbox notification to the requesting agent with the rejection reason.
   - Leaves contract unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract
from validators.kernel.identity import get_role_policy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


@pytest.fixture
def test_env(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(parents=True)
    workspace = ScratchWorkspace(ws_dir, scratch_dir, "att-1")

    contracts_dir = ws_dir / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True)
    initial_contract = {
        "work_order": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "allow": ["src/backend/**", "pyproject.toml"],
        "deny": ["src/frontend/**", ".env*"],
        "write_mode": "read-write",
        "version": 1,
        "revision": 1,
    }
    contract_file = contracts_dir / "WO-001.yaml"
    contract_file.write_text(yaml.safe_dump(initial_contract), encoding="utf-8")

    # Copy schema to workspace
    schema_src = Path(__file__).resolve().parent.parent / "schemas" / "contract-change-request.schema.json"
    schema_dst_dir = ws_dir / "schemas"
    schema_dst_dir.mkdir(parents=True)
    schema_dst = schema_dst_dir / "contract-change-request.schema.json"
    schema_dst.write_text(schema_src.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "workspace": workspace,
        "contract_file": contract_file,
    }


def make_gateway(workspace: ScratchWorkspace, actor_id: str, wo_id: str = "WO-001", allow: list[str] | None = None) -> ToolGateway:
    contract = AgentContract(
        agent_id=actor_id,
        work_order=wo_id,
        allow=tuple(allow or ["src/backend/**", "pyproject.toml", ".sync/**"]),
        deny=(".env*",),
        write_mode="read-write",
        version=1,
    )
    policy = get_role_policy(actor_id)
    boundary = RuntimeBoundary(OperationJournal())
    return ToolGateway(
        workspace,
        boundary,
        contract,
        policy,
        session_id="test-session",
        attempt_id="att-1",
        actor_id=actor_id,
        provider_id="test-provider",
    )


def test_worker_can_request_contract_change(test_env):
    ws = test_env["workspace"]
    gateway = make_gateway(ws, "codex", "WO-001")

    res = gateway.request_contract_change(
        work_order="WO-001",
        rationale="Need access to shared database schema in src/shared/db.py",
        requested_allow=["src/shared/db.py"],
    )

    assert res["success"] is True
    assert res["request_id"] == "CCR-001"
    assert res["status"] == "PENDING"

    ccr_file = ws.root / ".sync" / "inbox" / "claude" / "ccr" / "CCR-001.json"
    assert ccr_file.is_file()
    data = json.loads(ccr_file.read_text(encoding="utf-8"))
    assert data["request_id"] == "CCR-001"
    assert data["work_order_id"] == "WO-001"
    assert data["requesting_agent"] == "codex"
    assert data["requested_scope"]["allow"] == ["src/shared/db.py"]
    assert data["status"] == "PENDING"


def test_sequential_ccr_ids(test_env):
    ws = test_env["workspace"]
    gateway = make_gateway(ws, "codex", "WO-001")

    res1 = gateway.request_contract_change(
        work_order="WO-001",
        rationale="First amendment request for DB models",
        requested_allow=["src/db/models.py"],
    )
    res2 = gateway.request_contract_change(
        work_order="WO-001",
        rationale="Second amendment request for migrations",
        requested_allow=["src/db/migrations/**"],
    )

    assert res1["request_id"] == "CCR-001"
    assert res2["request_id"] == "CCR-002"


def test_worker_cannot_evaluate_ccr(test_env):
    ws = test_env["workspace"]
    worker_gateway = make_gateway(ws, "codex", "WO-001")

    res = worker_gateway.request_contract_change(
        work_order="WO-001",
        rationale="Need access to shared config file",
        requested_allow=["config.yaml"],
    )

    # Worker tries to approve its own CCR -> must be denied by role policy
    with pytest.raises(PermissionError):
        worker_gateway.evaluate_contract_change_request(
            request_id=res["request_id"],
            decision="APPROVED",
            reason="Self-approval attempt",
        )


def test_architect_can_approve_ccr_and_update_contract(test_env):
    ws = test_env["workspace"]
    worker_gateway = make_gateway(ws, "codex", "WO-001")
    architect_gateway = make_gateway(ws, "claude", "WO-000")

    # 1. Worker requests contract change
    req = worker_gateway.request_contract_change(
        work_order="WO-001",
        rationale="Need access to database schema to complete endpoints",
        requested_allow=["src/shared/schema.py"],
    )
    req_id = req["request_id"]

    # 2. Architect approves
    eval_res = architect_gateway.evaluate_contract_change_request(
        request_id=req_id,
        decision="APPROVED",
        reason="Approved: shared schema access is required for WO-001 backend endpoints.",
    )

    assert eval_res["evaluated"] is True
    assert eval_res["decision"] == "APPROVED"
    assert eval_res["contract_updated"] is True
    assert eval_res["contract_revision"] == 2

    # Verify contract file was updated
    contract_file = test_env["contract_file"]
    contract_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
    assert "src/shared/schema.py" in contract_data["allow"]
    assert contract_data["revision"] == 2

    # Verify CCR file updated with architect response
    ccr_file = ws.root / ".sync" / "inbox" / "claude" / "ccr" / f"{req_id}.json"
    ccr_data = json.loads(ccr_file.read_text(encoding="utf-8"))
    assert ccr_data["status"] == "APPROVED"
    assert ccr_data["architect_response"]["decision"] == "APPROVED"
    assert ccr_data["architect_response"]["contract_revision"] == 2

    # Verify worker received notification in inbox
    worker_inbox = ws.root / ".sync" / "inbox" / "codex"
    notices = list(worker_inbox.glob(f"*_{req_id}_approved.md"))
    assert len(notices) == 1
    notice_text = notices[0].read_text(encoding="utf-8")
    assert f"Contract Change Request {req_id}: APPROVED" in notice_text
    assert "WO-001" in notice_text


def test_architect_can_reject_ccr(test_env):
    ws = test_env["workspace"]
    worker_gateway = make_gateway(ws, "gemini", "WO-001")
    architect_gateway = make_gateway(ws, "claude", "WO-000")

    # 1. Worker requests disallowed scope
    req = worker_gateway.request_contract_change(
        work_order="WO-001",
        rationale="Need access to backend internal database credentials",
        requested_allow=["src/backend/secrets.py"],
    )
    req_id = req["request_id"]

    # 2. Architect rejects
    eval_res = architect_gateway.evaluate_contract_change_request(
        request_id=req_id,
        decision="REJECTED",
        reason="Denied: frontend must access backend only via public API, not direct secrets.",
    )

    assert eval_res["evaluated"] is True
    assert eval_res["decision"] == "REJECTED"
    assert eval_res["contract_updated"] is False

    # Contract file must remain unchanged
    contract_file = test_env["contract_file"]
    contract_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
    assert "src/backend/secrets.py" not in contract_data["allow"]
    assert contract_data["revision"] == 1

    # CCR status must be REJECTED
    ccr_file = ws.root / ".sync" / "inbox" / "claude" / "ccr" / f"{req_id}.json"
    ccr_data = json.loads(ccr_file.read_text(encoding="utf-8"))
    assert ccr_data["status"] == "REJECTED"
    assert ccr_data["architect_response"]["decision"] == "REJECTED"

    # Worker received rejection notification
    worker_inbox = ws.root / ".sync" / "inbox" / "gemini"
    notices = list(worker_inbox.glob(f"*_{req_id}_rejected.md"))
    assert len(notices) == 1
    notice_text = notices[0].read_text(encoding="utf-8")
    assert f"Contract Change Request {req_id}: REJECTED" in notice_text
