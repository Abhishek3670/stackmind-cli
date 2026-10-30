"""Test that Architect delegate_task authors governed Work Orders and Contracts."""

import pytest
import yaml
from pathlib import Path
from tempfile import TemporaryDirectory

from validators.kernel.contract import AgentContract
from validators.kernel.identity import AuthorizationPolicy, get_role_policy
from validators.kernel.operations import OperationJournal
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace
from validators.harness.authoring_gate import AuthoringGate


def test_delegate_task_authors_work_order_and_contract():
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ws = ScratchWorkspace.create(tmp_path, "test-wo-delegation")
        contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=("workspace/**",),
            deny=(),
            write_mode="read-write",
        ).freeze()
        policy = get_role_policy("claude")
        boundary = RuntimeBoundary(OperationJournal())

        gw = ToolGateway(
            workspace=ws,
            boundary=boundary,
            contract=contract,
            policy=policy,
            session_id="test-session",
            attempt_id="test-attempt",
            actor_id="claude",
            provider_id="anthropic",
        )

        # 1. Delegate to backend worker
        res1 = gw.delegate_task(
            target_agent="backend",
            instructions="Implement YOLO training entrypoint CLI in app.py",
            target_files=["app.py"],
        )
        assert res1["status"] == "delegated"
        assert res1["agent"] == "codex"
        assert res1["role"] == "backend"
        assert res1["work_order_id"] == "WO-001"
        assert res1["work_order_file"] == ".sync/work-orders/ACTIVE/WO-001.yaml"

        # Check WO file on disk
        wo_file = ws.root / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
        assert wo_file.is_file()
        wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
        assert wo_data["id"] == "WO-001"
        assert wo_data["status"] == "ACTIVE"
        assert wo_data["type"] == "FEATURE"
        assert wo_data["assigned_agents"] == ["codex"]
        assert wo_data["deliverable"]["path"] == "app.py"

        # Check Contract file on disk
        contract_file = ws.root / ".sync" / "contracts" / "WO-001.yaml"
        assert contract_file.is_file()
        contract_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
        assert contract_data["work_order"] == "WO-001"
        assert contract_data["agent_id"] == "codex"
        assert contract_data["identity"]["role"] == "backend"

        # Check INDEX.yaml
        index_file = ws.root / ".sync" / "work-orders" / "INDEX.yaml"
        assert index_file.is_file()
        index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
        assert index_data["next_id"] == 2
        assert len(index_data["orders"]) == 1
        assert index_data["orders"][0]["id"] == "WO-001"
        assert index_data["orders"][0]["assigned_agents"] == ["codex"]

        # Validate gate compliance
        gate = AuthoringGate(project_root=ws.root)
        wo_dec = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", wo_file.read_text("utf-8"), agent="claude")
        assert wo_dec.passed is True
        contract_dec = gate.validate_artifact_content(".sync/contracts/WO-001.yaml", contract_file.read_text("utf-8"), agent="claude")
        assert contract_dec.passed is True

        # 2. Delegate second task to QA worker
        res2 = gw.delegate_task(
            target_agent="qa",
            instructions="Write pytest suite for app.py with mocks in tests/test_app.py",
            target_files=["tests/test_app.py"],
        )
        assert res2["status"] == "delegated"
        assert res2["agent"] == "gemma"
        assert res2["role"] == "qa"
        assert res2["work_order_id"] == "WO-002"

        wo2_file = ws.root / ".sync" / "work-orders" / "ACTIVE" / "WO-002.yaml"
        assert wo2_file.is_file()
        wo2_data = yaml.safe_load(wo2_file.read_text(encoding="utf-8"))
        assert wo2_data["id"] == "WO-002"
        assert wo2_data["type"] == "QA"
        assert wo2_data["assigned_agents"] == ["gemma"]

        # Check INDEX.yaml updated monotonically
        index_data2 = yaml.safe_load(index_file.read_text(encoding="utf-8"))
        assert index_data2["next_id"] == 3
        assert len(index_data2["orders"]) == 2
        assert index_data2["orders"][1]["id"] == "WO-002"


def test_tui_state_reflects_delegated_work_order():
    from cli.tui.state import AutonomousDeliveryState
    from cli.tui.runtime_panel import format_work_orders

    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ws = ScratchWorkspace.create(tmp_path, "test-tui-wo")
        contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=("workspace/**",),
            deny=(),
            write_mode="read-write",
        ).freeze()
        gw = ToolGateway(
            workspace=ws,
            boundary=RuntimeBoundary(OperationJournal()),
            contract=contract,
            policy=get_role_policy("claude"),
            session_id="test-session",
            attempt_id="test-attempt",
            actor_id="claude",
            provider_id="anthropic",
        )

        res = gw.delegate_task(
            target_agent="backend",
            instructions="Implement backend endpoint in app.py",
            target_files=["app.py"],
        )
        assert res["work_order_id"] == "WO-001"

        state = AutonomousDeliveryState(session_id="test-sess", workspace=ws.root, work_orders=[])
        assert len(state.work_orders) == 0

        # Simulate receiving task.auto_delegated event
        state.process_event({
            "name": "task.auto_delegated",
            "sequence": 1,
            "session_id": "test-sess",
            "payload": {
                "agent_id": "codex",
                "role": "backend",
                "instructions": "Implement backend endpoint in app.py",
                "target_files": ["app.py"],
                "work_order_id": "WO-001",
            },
        })

        assert len(state.work_orders) == 1
        assert state.work_orders[0].id == "WO-001"
        assert "Backend" in state.work_orders[0].title
        assert state.roles["Backend"].work_order_id == "WO-001"

        lines = format_work_orders(state.work_orders)
        plain_text = " ".join(line.plain for line in lines)
        assert "WO-001" in plain_text
        assert "No active orders" not in plain_text

