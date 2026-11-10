from pathlib import Path
from enum import Enum
import pytest

from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, RunState
from validators.kernel.daemon.supervisor.control_plane import (
    BlockerSubsystem,
    record_lifecycle_event,
)
from validators.kernel.daemon.supervisor.models import BudgetExhaustedError
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.daemon.storage import DaemonStorage


class DummyEnum(Enum):
    ALPHA = "alpha"


def test_save_run_state_handles_enums_without_representer_error(tmp_path: Path):
    """Saving run state to YAML shadow file must serialize all Enum instances cleanly."""
    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage=storage)
    state = mgr.supervisor.start_run("run-enum-test", "Test Goal", tmp_path, "sess-1")

    # Add a lifecycle event with an Enum actor
    record_lifecycle_event(
        state=state,
        event_type="WAIT_CREATED",
        entity_id="wait-1",
        actor=BlockerSubsystem.DEPENDENCY,
        previous_state=None,
        new_state="ACTIVE",
        reason_code="Wait condition",
    )

    # Embed dummy enum into delivery_blockers or dict
    state.delivery_blockers.append({"enum_key": DummyEnum.ALPHA, "subsystem": BlockerSubsystem.EXECUTION})

    # Saving must succeed and not raise yaml.representer.RepresenterError
    saved_path = mgr.supervisor.save_run_state(state, tmp_path)
    assert saved_path.is_file()
    content = saved_path.read_text(encoding="utf-8")
    assert "dependency" in content
    assert "alpha" in content


def test_protocol_handles_budget_exhausted_error_cleanly(tmp_path: Path):
    """Protocol boundary must translate BudgetExhaustedError to informative -32002 rather than masked Internal error."""
    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage=storage)
    proto = JsonRpcProtocol(mgr)

    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]

    state = mgr.supervisor.start_run("run-budget-test", "Goal", tmp_path, session_id)
    state.global_turn_budget = 0

    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session.turn",
        "params": {
            "session_id": session_id,
            "prompt": "done?",
            "role": "architecture",
            "agent_id": "claude",
        },
    }

    resp = proto.handle(req)
    assert "error" in resp
    assert resp["error"]["code"] == -32002
    assert "budget" in resp["error"]["message"].lower()
    assert "resume" in resp["error"]["message"].lower()
    assert "internal error" not in resp["error"]["message"].lower()


def test_hooked_start_turn_persists_state_and_budget_on_exhaustion(tmp_path: Path):
    """When budget is exhausted, hooked_start_turn must sync budget file and persist shadow state."""
    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage=storage)
    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]

    state = mgr.supervisor.start_run("run-sync-test", "Goal", tmp_path, session_id)
    state.global_turn_budget = 0

    with pytest.raises(BudgetExhaustedError):
        mgr.start_turn(session_id, "hello", role="architecture", agent_id="claude")

    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.budget_exhausted is True

    # Check budget.json was written
    budget_json = tmp_path / ".sync" / "runtime" / "supervisor" / "budget.json"
    assert budget_json.is_file()
    import json
    data = json.loads(budget_json.read_text(encoding="utf-8"))
    assert data["remaining_turn_budget"] == 0

    # Check shadow YAML was written
    shadow_yaml = tmp_path / ".sync" / "runtime" / "supervisor" / f"{state.run_id}.yaml"
    assert shadow_yaml.is_file()
