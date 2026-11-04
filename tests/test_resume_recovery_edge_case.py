import pytest
from pathlib import Path
import yaml
from unittest.mock import MagicMock

from validators.kernel.daemon.supervisor import Phase, RunState
from validators.kernel.daemon.supervisor.controls import _ControlsMixin
from validators.kernel.daemon.supervisor.recovery import _RecoveryMixin
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.work_order_ops import cancel_work_order


class DummySupervisor(_ControlsMixin, _RecoveryMixin):
    def __init__(self, manager):
        self.manager = manager
        self.transitions = []

    def _transition(self, state, new_phase, authorized_override=False):
        state.phase = new_phase
        self.transitions.append(new_phase)

    def _discover_worker_wos(self, state):
        pass

    def _role_for_agent(self, agent):
        return "frontend" if agent == "gemini" else "gitops"

    def _agent_for_wo(self, wo_id, ws):
        return "local-llm" if wo_id == "WO-008" else "gemini"

    def _unarchive_work_order(self, ws, wo_id):
        pass


def test_cannot_cancel_completed_work_order(tmp_path: Path):
    """cancel_work_order must refuse to cancel an already completed work order."""
    comp_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    comp_dir.mkdir(parents=True, exist_ok=True)
    wo_file = comp_dir / "WO-005.yaml"
    wo_file.write_text(yaml.safe_dump({
        "id": "WO-005",
        "status": "COMPLETED",
        "title": "Scroll-Driven Animation Logic",
    }), encoding="utf-8")

    res = cancel_work_order(tmp_path, "WO-005", reason="Contention cleanup")
    assert res is None, "Should not cancel a completed work order"
    data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETED"


def test_unblock_in_flight_preserves_completed_work_orders(tmp_path: Path):
    """unblock_in_flight_work_orders must not move COMPLETED work orders back to ACTIVE."""
    ws = tmp_path
    comp_dir = ws / ".sync" / "work-orders" / "COMPLETED"
    comp_dir.mkdir(parents=True, exist_ok=True)
    act_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    act_dir.mkdir(parents=True, exist_ok=True)

    (comp_dir / "WO-003.yaml").write_text("id: WO-003\nstatus: COMPLETED\n", encoding="utf-8")
    (act_dir / "WO-006.yaml").write_text("id: WO-006\nstatus: BLOCKED\n", encoding="utf-8")

    mgr = MagicMock()
    sup = DummySupervisor(mgr)

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        product_goal="Goal",
        workspace=str(ws),
        worker_wo_ids=["WO-003", "WO-006"],
        completed_wo_ids=[],
        blocked_wo_ids=["WO-006"],
    )

    sup.unblock_in_flight_work_orders(state)

    # WO-003 must remain in COMPLETED and be added to state.completed_wo_ids
    assert (comp_dir / "WO-003.yaml").is_file()
    assert not (act_dir / "WO-003.yaml").is_file()
    assert "WO-003" in state.completed_wo_ids


def test_advance_recovery_decision_when_no_wo_blocked(tmp_path: Path):
    """_advance_recovery_decision must transition cleanly to execution/review when no WO is blocked."""
    ws = tmp_path
    (ws / ".sync" / "work-orders" / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (ws / ".sync" / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)

    mgr = MagicMock()
    sup = DummySupervisor(mgr)

    state = RunState(
        run_id="run-test",
        session_id="sess-test",
        product_goal="Goal",
        workspace=str(ws),
        phase=Phase.ARCHITECT_RECOVERY_DECISION,
        worker_wo_ids=["WO-001", "WO-002"],
        completed_wo_ids=["WO-001", "WO-002"],
        recovery_wo_id=None,
    )

    from validators.kernel.daemon.supervisor import AdvanceResult
    res = sup._advance_recovery_decision(state)

    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.INTEGRATION_REVIEW
    assert state.error is None
