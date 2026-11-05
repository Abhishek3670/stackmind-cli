"""QA review budget: one dispatch is one attempt, and an authorized recovery renews it."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase
from validators.kernel.daemon.supervisor.attempts import BudgetKey, evaluate_retry_budget, RetryContext, FailureClass


class _Mgr:
    """Records QA dispatches; every dispatched review later ends without a verdict."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.dispatched: list[str] = []

    def start_turn(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        op_id = f"qa-op-{len(self.dispatched) + 1}"
        self.dispatched.append(op_id)
        return {"operation_id": op_id}

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return {"operation_id": operation_id, "status": "COMPLETED", "result": {"summary": "[Tool Use: write_file]"}}


def _workspace(tmp_path: Path) -> Path:
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True)
    (wo_dir / "WO-001.yaml").write_text(
        "id: WO-001\ntype: FEATURE\nstatus: ACTIVE\nassigned_agents: [gemini]\n"
        "deliverable:\n  type: code\n  path: index.html\n",
        encoding="utf-8",
    )
    (tmp_path / "index.html").write_text("<!DOCTYPE html><title>x</title>", encoding="utf-8")
    return tmp_path


def _supervisor(tmp_path: Path) -> tuple[LifecycleSupervisor, Any, _Mgr]:
    mgr = _Mgr(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = sup.start_run("run-qa-budget", "Goal", tmp_path, "sess-qa-budget")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.published_wo_ids = ["WO-001"]
    state.global_turn_budget = 50
    sup._synthesize_authoritative_qa_verdict = lambda *a, **k: None  # the QA turn left no verdict
    sup._qa_review_prompt = lambda *a, **k: "review"
    sup.escalate_wo_to_recovery = lambda *a, **k: None
    return sup, state, mgr


def _tick(sup: LifecycleSupervisor, state: Any, ws: Path) -> Any:
    from validators.harness import review_gate

    return sup._handle_qa_gate_pending(state, ws, "WO-001", review_gate.evaluate_qa_gate(ws, "WO-001"))


def test_one_review_turn_without_verdict_is_one_attempt_not_two(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    sup, state, mgr = _supervisor(tmp_path)

    _tick(sup, state, ws)  # dispatches review #1
    assert mgr.dispatched == ["qa-op-1"]
    assert state.qa_review_attempts["WO-001"] == 1

    _tick(sup, state, ws)  # review #1 ended with no verdict -> must dispatch review #2, not escalate
    assert state.phase is not Phase.ARCHITECT_RECOVERY_DECISION
    assert mgr.dispatched == ["qa-op-1", "qa-op-2"]
    assert state.qa_review_attempts["WO-001"] == 2


def test_budget_trips_only_after_two_real_dispatches(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    sup, state, mgr = _supervisor(tmp_path)

    for _ in range(3):
        _tick(sup, state, ws)

    assert len(mgr.dispatched) == 2
    assert state.phase is Phase.ARCHITECT_RECOVERY_DECISION
    assert "after 2 attempt(s)" in state.error


def test_authorized_recovery_renews_the_exhausted_qa_review_budget(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    sup, state, _mgr = _supervisor(tmp_path)
    state.qa_review_attempts["WO-001"] = 2  # exhausted by an earlier malformed review

    exhausted = evaluate_retry_budget(
        state, RetryContext(budget=BudgetKey.QA_REVIEW, scope="WO-001", failure_class=FailureClass.TRANSIENT,
                            failure_code="QA_REVIEW_DISPATCH"))
    assert exhausted.outcome.name == "BUDGET_EXHAUSTED"

    sup._clear_worker_blocker(state, ws, "WO-001", redispatch=False)

    renewed = evaluate_retry_budget(
        state, RetryContext(budget=BudgetKey.QA_REVIEW, scope="WO-001", failure_class=FailureClass.TRANSIENT,
                            failure_code="QA_REVIEW_DISPATCH"))
    assert renewed.outcome.name != "BUDGET_EXHAUSTED"
    assert state.qa_review_attempts.get("WO-001", 0) == 0
    kinds = {(r["budget"], r["scope"]) for r in state.budget_renewals}
    assert ("QA_REVIEW", "WO-001") in kinds and ("WORKER_TASK", "WO-001") in kinds


def test_recovery_prompt_does_not_recommend_what_the_role_policy_forbids() -> None:
    src = Path("validators/kernel/daemon/supervisor/recovery.py").read_text(encoding="utf-8")
    assert "the Architect directly repairs the deliverable files" not in src
    assert "can NOT write application source or test files" in src
