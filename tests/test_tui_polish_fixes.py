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
    assert "Run harness" in group.actions[0].description
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
