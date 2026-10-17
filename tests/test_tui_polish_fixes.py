"""Tests for screenshot polish fixes: contract payload formatting, action lifecycle, and work order status resolution."""

from __future__ import annotations

from pathlib import Path
import yaml

from cli.tui.chat import format_contract_payload, render_actions_group_str
from cli.tui.events import extract_assistant_response
from cli.tui.runtime_panel import format_work_orders
from cli.tui.state import ActionsGroup, AutonomousDeliveryState, RoleStatus, WorkOrderItem


def test_format_contract_payload_pure_json():
    """Verify raw task contract completion JSON is formatted into a clean turn summary."""
    raw = '{"status": "completed", "modified_files": ["PLAN.md"]}'
    res = format_contract_payload(raw)
    assert "Created implementation plan in `PLAN.md`" in res
    assert "{" not in res
    assert "}" not in res

    # Other modified files
    raw_files = '{"status": "completed", "modified_files": ["src/game.py", "src/ui.py"]}'
    res_files = format_contract_payload(raw_files)
    assert "Completed turn. Modified files: `src/game.py`, `src/ui.py`." in res_files

    # Blocked contract payload
    raw_blocked = '{"status": "blocked", "blockers": ["Missing dependencies"]}'
    res_blocked = format_contract_payload(raw_blocked)
    assert "Turn blocked: Missing dependencies" in res_blocked


def test_format_contract_payload_with_conversational_text():
    """Verify trailing technical contract JSON block is stripped when conversational text exists."""
    content = (
        "I have drafted the plan for the snake game.\n\n"
        "```json\n"
        '{\n  "status": "completed",\n  "modified_files": ["PLAN.md"]\n}\n'
        "```"
    )
    res = format_contract_payload(content)
    assert res == "I have drafted the plan for the snake game."


def test_extract_assistant_response_handles_raw_contract():
    """Verify extract_assistant_response handles direct result contract dict and JSON strings."""
    payload_dict = {
        "result": {
            "status": "completed",
            "modified_files": ["PLAN.md"],
        }
    }
    extracted = extract_assistant_response(payload_dict)
    assert extracted is not None
    assert "Created implementation plan in `PLAN.md`" in extracted

    payload_str = {
        "result": {
            "response": '{"status": "completed", "modified_files": ["PLAN.md"]}'
        }
    }
    extracted_str = extract_assistant_response(payload_str)
    assert extracted_str is not None
    assert "Created implementation plan in `PLAN.md`" in extracted_str


def test_actions_group_completion_without_call_id():
    """Verify toolResult matches by tool_name and completes running action when call_id is missing."""
    state = AutonomousDeliveryState(session_id="session-1")

    # 1. toolCall arrives with generated call_id
    state.process_event({
        "name": "event.toolCall",
        "payload": {
            "tool_name": "run_harness",
            "arguments": {"path": ".sync/harness"},
        },
    })
    group = state.current_turn_actions
    assert len(group.actions) == 1
    assert ("Harnessing" in group.actions[0].description or "Agent" in group.actions[0].description or "Run harness" in group.actions[0].description)
    assert group.actions[0].status == "RUNNING"
    assert group.actions[0].symbol == "●"

    # 2. toolResult arrives without call_id
    state.process_event({
        "name": "event.toolResult",
        "payload": {
            "tool_name": "run_harness",
            "status": "completed",
        },
    })
    assert len(group.actions) == 1
    assert group.actions[0].status == "COMPLETED"
    assert group.actions[0].symbol == "✓"


def test_actions_group_does_not_leak_to_next_turn():
    """Verify current_turn_actions is cleanly detached when assistant message is finalized."""
    state = AutonomousDeliveryState(session_id="session-1")

    # Turn 1: tool call
    state.process_event({
        "name": "event.toolCall",
        "payload": {
            "tool_name": "run_harness",
            "arguments": {},
        },
    })
    # Finalize Turn 1
    state.add_message("assistant", "Plan created.")
    assert len(state.messages) == 1
    assert state.messages[0].actions is not None
    assert len(state.messages[0].actions.actions) == 1
    assert state.messages[0].actions.actions[0].status == "COMPLETED"

    # Turn 2: User approves
    state.add_message("user", ":approve Proceed with implementation")
    state.add_message("assistant", "✓ HITL Approval recorded: Proceed with implementation")

    # Turn 2 must NOT have Turn 1's actions group
    assert state.messages[-1].actions is None


def test_work_orders_dependency_aware_status(tmp_path: Path):
    """Verify on-disk ACTIVE work orders are properly resolved to WAITING if dependencies are unmet."""
    state = AutonomousDeliveryState(session_id="session-1")
    state.workspace = tmp_path
    sync_wos = tmp_path / ".sync" / "work-orders"
    active_dir = sync_wos / "ACTIVE"
    active_dir.mkdir(parents=True)

    # WO-001 has no dependencies
    wo1 = {
        "id": "WO-001",
        "title": "Core Architecture",
        "status": "ACTIVE",
        "dependencies": [],
        "assigned_agents": ["codex"],
    }
    (active_dir / "WO-001.yaml").write_text(yaml.dump(wo1), encoding="utf-8")

    # WO-002 depends on WO-001
    wo2 = {
        "id": "WO-002",
        "title": "Backend Logic",
        "status": "ACTIVE",
        "dependencies": ["WO-001"],
        "assigned_agents": ["codex"],
    }
    (active_dir / "WO-002.yaml").write_text(yaml.dump(wo2), encoding="utf-8")

    # WO-003 depends on WO-002
    wo3 = {
        "id": "WO-003",
        "title": "Frontend UI",
        "status": "ACTIVE",
        "dependencies": ["WO-002"],
        "assigned_agents": ["gemini"],
    }
    (active_dir / "WO-003.yaml").write_text(yaml.dump(wo3), encoding="utf-8")

    # INDEX.yaml
    idx = {
        "orders": [wo1, wo2, wo3],
    }
    (sync_wos / "INDEX.yaml").write_text(yaml.dump(idx), encoding="utf-8")

    state.sync_work_orders_from_disk(tmp_path)

    assert len(state.work_orders) == 3
    # WO-001 is runnable -> RUNNING
    assert state.work_orders[0].id == "WO-001"
    assert state.work_orders[0].status == "RUNNING"

    # WO-002 depends on WO-001 (unmet) -> WAITING
    assert state.work_orders[1].id == "WO-002"
    assert state.work_orders[1].status == "WAITING"

    # WO-003 depends on WO-002 (unmet) -> WAITING
    assert state.work_orders[2].id == "WO-003"
    assert state.work_orders[2].status == "WAITING"

    # Verify runtime panel rendering symbols
    rendered_lines = format_work_orders(state.work_orders)
    # WO-001 should render with ●
    assert "● WO-001" in rendered_lines[0].plain
    # WO-002 and WO-003 should render with ○ (waiting)
    assert "○ WO-002" in rendered_lines[1].plain
    assert "○ WO-003" in rendered_lines[2].plain


def test_orphaned_active_operation_auto_reclaimed_on_new_turn(tmp_path: Path):
    """Verify dead or orphaned active_operations are cleanly reclaimed without blocking new turns."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "test", {}, str(tmp_path))
    sid = session["session_id"]

    # Simulate an operation that became orphaned in CANCEL_REQUESTED without alive thread
    cancel_ev, op_id = manager.begin_operation(sid, "turn", {"prompt": "First prompt"})
    assert manager._sessions[sid].get("active_operation") == op_id
    manager.cancel_operation(op_id)
    _, rec = manager._operation(op_id)
    # Force cancel_requested status to simulate hanging worker
    rec["status"] = "CANCEL_REQUESTED"
    manager._sessions[sid]["active_operation"] = op_id

    # Submitting a new turn must NOT crash with "session already has an active operation" or "Invalid params"
    new_cancel_ev, new_op_id = manager.begin_operation(sid, "turn", {"prompt": "Second prompt"})
    assert new_op_id != op_id
    assert manager._sessions[sid].get("active_operation") == new_op_id

    # The previous operation must have been marked CANCELLED
    _, old_rec = manager._operation(op_id)
    assert old_rec["status"] == "CANCELLED"


def test_daemon_recovery_clears_dangling_active_operation(tmp_path: Path):
    """Verify daemon restart resets dangling active_operation and marks orphaned operations CANCELLED."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "test", {}, str(tmp_path))
    sid = session["session_id"]

    _, op_id = manager.begin_operation(sid, "turn", {"prompt": "Initial turn"})
    assert manager._sessions[sid].get("active_operation") == op_id

    # Simulate daemon process restart: create a new SessionManager pointing to same storage
    recovered_manager = SessionManager(storage)
    assert recovered_manager._sessions[sid].get("active_operation") is None
    _, old_rec = recovered_manager._operation(op_id)
    assert old_rec["status"] == "CANCELLED"


def test_protocol_reports_descriptive_contention_error(tmp_path: Path):
    """Verify JSON-RPC protocol reports actual contention message instead of generic 'Invalid params'."""
    from validators.kernel.daemon.protocol import JsonRpcProtocol
    from unittest.mock import MagicMock
    from validators.kernel.daemon.supervisor import OperationContentionError

    mock_mgr = MagicMock()
    mock_mgr.start_turn.side_effect = OperationContentionError("session already has an active operation")
    protocol = JsonRpcProtocol(mock_mgr)

    res = protocol.handle({
        "jsonrpc": "2.0",
        "id": "1",
        "method": "session.turn",
        "params": {"session_id": "s1", "prompt": "hello"},
    })

    assert "error" in res
    assert res["error"]["code"] == -32002
    assert "already has an active operation" in res["error"]["message"]
    assert res["error"]["message"] != "Invalid params"


def test_provider_singular_and_plural_dispatch(tmp_path: Path):
    """Verify :provider (singular) and :providers (plural) dispatch identically."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState
    from unittest.mock import MagicMock

    state1 = AutonomousDeliveryState(session_id="s1")
    session1 = {"session_id": "s1", "workspace": str(tmp_path)}
    mock_client = MagicMock()
    mock_adapter = MagicMock()

    # 1. Dispatch :provider (singular)
    _, _ = dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session1,
        text=":provider",
        state=state1,
    )
    out1 = state1.messages[-1].content
    assert "Cloud Providers & Models Configuration" in out1
    assert "Configured Providers & Endpoints:" in out1
    assert "anthropic" in out1.lower()

    # 2. Dispatch :providers (plural alias)
    state2 = AutonomousDeliveryState(session_id="s2")
    session2 = {"session_id": "s2", "workspace": str(tmp_path)}
    _, _ = dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session2,
        text=":providers",
        state=state2,
    )
    out2 = state2.messages[-1].content
    assert "Cloud Providers & Models Configuration" in out2
    assert "Configured Providers & Endpoints:" in out2
    assert "anthropic" in out2.lower()


def test_models_unified_into_provider(tmp_path: Path):
    """Verify :models command informs user it is unified under :provider."""
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState
    from unittest.mock import MagicMock

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}
    mock_client = MagicMock()
    mock_adapter = MagicMock()

    _, _ = dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":models",
        state=state,
    )
    out = state.messages[-1].content
    assert "[INFO]" in out
    assert "unified into ':provider'" in out
    assert "Cloud Providers & Models Configuration" in out


def test_roles_arrow_and_escape_auto_persists(tmp_path: Path):
    """Verify cycling provider in :roles and pressing ESC guarantees persistence to disk and daemon."""
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_interactive_roles_panel
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings
    from unittest.mock import MagicMock

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}
    mock_client = MagicMock()

    # Sequence in :roles:
    # Row 0 is Architecture (default anthropic/ollama)
    # 1. RIGHT -> cycle provider to next
    # 2. ESCAPE -> close and save immediately (no need to navigate to Save row)
    key_seq = [
        Key.RIGHT,
        Key.ESCAPE,
    ]

    result = run_interactive_roles_panel(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_seq),
        workspace=tmp_path,
        width=80,
    )
    assert "[SUCCESS]" in result
    assert "saved" in result.lower()

    # Check persistence in settings.json
    s = load_settings(tmp_path)
    arch_prov = s.roles["architecture"]["provider"]
    assert arch_prov is not None
    # Client settings_update was called
    assert mock_client.settings_update.called
    # Runtime state was updated immediately
    assert state.roles["Architecture"].backend == arch_prov


def test_daemon_runner_prioritizes_workspace_role_settings(tmp_path: Path):
    """Verify _default_runner prioritizes role provider configured in workspace settings.json."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.settings import ProviderConfig, load_settings, save_settings
    from validators.kernel.providers.adapter import OpenAICompatibleAdapter

    # Workspace settings.json specifies custom provider for architecture
    settings = load_settings(tmp_path)
    settings.providers["custom"] = ProviderConfig(
        name="custom",
        display_name="Custom (OpenAI-compat)",
        enabled=True,
        api_key="sk-custom-test",
        base_url="http://localhost:20128/v1",
        models=["my-local-model"],
        default_model="my-local-model",
    )
    settings.roles["architecture"] = {
        "provider": "custom",
        "model": "my-local-model",
    }
    save_settings(settings, tmp_path)

    storage = DaemonStorage(tmp_path / "daemon.json")
    mgr = SessionManager(storage)
    # Even if mgr._roles had another default (e.g. echo-agent or ollama),
    # _default_runner MUST use the workspace's configured role provider!
    runner = mgr._default_runner(str(tmp_path), "architecture")
    assert isinstance(runner.provider_adapter, OpenAICompatibleAdapter)
    assert runner.provider_adapter.base_url == "http://localhost:20128/v1"
    assert runner.provider_adapter.model_name == "my-local-model"
    # And daemon memory is synced
    assert mgr._roles["architecture"]["backend"] == "custom"
    assert mgr._roles["architecture"]["model"] == "my-local-model"


