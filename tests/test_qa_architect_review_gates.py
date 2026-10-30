"""SDP-QA-ARCHITECT-REVIEW — mandatory QA and Architect final-acceptance gates.

Deterministic tests (no live LLM, no network).  External agent turns are
simulated through a fake manager that records dispatches and exposes terminal
operations, exactly like the existing SDP-FIX-006 harness tests do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli.init import ensure_canonical_tree, ensure_workspace_protocol_scaffolding
from validators.harness import review_gate
from validators.harness.authoring_policy import qa_verdict_channel_required
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.io import durable_write_yaml

# ─── Fixtures / helpers ──────────────────────────────────────────────

DELIVERABLE = "src/app.py"
GOOD_CODE = "def add(a, b):\n    return a + b\n"


class FakeManager:
    """Minimal execution substrate: records turns, exposes operation records."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.turns: list[dict[str, Any]] = []
        self._operations: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}

    def start_turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        op_id = f"op-{len(self.turns) + 1}"
        self.turns.append({"op_id": op_id, "prompt": prompt, **params})
        self._operations[op_id] = {
            "operation_id": op_id,
            "status": "RUNNING",
            "work_order_id": params.get("work_order_id"),
            "agent_id": params.get("agent_id"),
            "role": params.get("role"),
            "result": {},
        }
        return {"operation_id": op_id, "status": "RUNNING"}

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        return self._operations[operation_id]

    def list_operations(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._operations.values())

    def complete(self, op_id: str, status: str = "COMPLETED", result: dict | None = None) -> None:
        self._operations[op_id]["status"] = status
        self._operations[op_id]["result"] = result or {"status": "completed"}

    def last_turn_for_role(self, role: str) -> dict[str, Any] | None:
        for turn in reversed(self.turns):
            if turn.get("role") == role:
                return turn
        return None


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    ensure_workspace_protocol_scaffolding(ws)
    ensure_canonical_tree(ws)
    (ws / "PLAN.md").write_text(
        "# Approved Plan\n\n## Milestones\n- [ ] M1: Backend API\n\n"
        "## Acceptance Criteria\n- The API computes sums correctly.\n",
        encoding="utf-8",
    )
    src = ws / "src"
    src.mkdir(parents=True, exist_ok=True)
    (ws / DELIVERABLE).write_text(GOOD_CODE, encoding="utf-8")
    return ws


def write_wo(
    ws: Path,
    wo_id: str = "WO-001",
    *,
    agent: str = "codex",
    role: str = "backend",
    delim_type: str = "code",
    path: str = DELIVERABLE,
    dependencies: list[str] | None = None,
) -> None:
    data = {
        "id": wo_id,
        "title": "Implement backend API",
        "status": "ACTIVE",
        "assigned_agents": [agent],
        "role": role,
        "deliverable": {"type": delim_type, "path": path},
        "acceptance_criteria": ["API returns the sum of two integers"],
        "dependencies": dependencies or [],
    }
    durable_write_yaml(
        ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml", data
    )


def make_state(ws: Path, *, with_plan: bool = True) -> RunState:
    state = RunState(
        run_id="run-qa-001",
        product_goal="Sums API",
        workspace=str(ws),
        session_id="sess-qa",
    )
    state.plan_id = "PLAN-001" if with_plan else None
    state.worker_wo_ids = ["WO-001"]
    state.published_wo_ids = ["WO-001"]
    state.integration_evidence = {"passed": True, "issues_count": 0}
    state.integration_blockers = []
    return state


def developer_op(manager: FakeManager, wo_id: str = "WO-001") -> str:
    op = next(
        (
            o for o in reversed(list(manager._operations.values()))
            if o.get("work_order_id") == wo_id and o.get("role") != "qa"
        ),
        None,
    )
    if op is None:
        resp = manager.start_turn(
            "sess-qa", "implement", role="backend", agent_id="codex", work_order_id=wo_id
        )
        op = manager._operations[resp["operation_id"]]
    op["status"] = "COMPLETED"
    op["result"] = {"status": "completed"}
    return op["operation_id"]


def qa_verdict(
    ws: Path,
    wo_id: str = "WO-001",
    *,
    verdict: str = "PASS",
    criteria_outcome: str = "PASS",
    evidence: list[dict[str, Any]] | None = None,
    findings: list[dict[str, Any]] | None = None,
    required_actions: list[str] | None = None,
    subject_hash: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": review_gate.QA_VERDICT_SCHEMA,
        "work_order_id": wo_id,
        "revision": 1,
        "reviewer": "gemma",
        "reviewer_role": "qa",
        "timestamp": "2026-10-30T00:00:00+00:00",
        "verdict": verdict,
        "subject_hash": subject_hash or review_gate.subject_hash(ws, wo_id),
        "criteria": [
            {
                "id": "AC-1",
                "description": "API returns the sum of two integers",
                "outcome": criteria_outcome,
                "evidence": "pytest -q",
            }
        ],
        "evidence": evidence
        if evidence is not None
        else [{"kind": "test", "command": "pytest -q", "result": "1 passed", "passed": True}],
        "findings": findings or [],
        "required_actions": required_actions or [],
        "rework_attempt": 0,
        "rework_budget": 2,
    }
    return payload


def final_verdict(
    ws: Path,
    *,
    status: str = "PASS",
    plan_id: str = "PLAN-001",
    required: list[str] | None = None,
    plan_hash_value: str | None = None,
) -> dict[str, Any]:
    required = required or ["WO-001"]
    return {
        "schema": review_gate.FINAL_REVIEW_SCHEMA,
        "run_id": "run-qa-001",
        "plan_id": plan_id,
        "plan_hash": plan_hash_value or review_gate.plan_hash(ws),
        "status": status,
        "reviewer": "claude",
        "reviewer_role": "architecture",
        "timestamp": "2026-10-30T00:00:00+00:00",
        "reviewed_work_orders": [
            {"id": wo_id, "subject_hash": review_gate.subject_hash(ws, wo_id)}
            for wo_id in required
        ],
        "acceptance_criteria": [
            {"id": "AC-1", "description": "API sums", "outcome": "PASS", "evidence": "pytest -q"}
        ],
        "evidence": [
            {"kind": "test", "command": "pytest -q", "result": "1 passed", "passed": True}
        ],
        "findings": [],
        "required_actions": [],
        "attempt": 1,
    }


# ─── A. Gate unit semantics ──────────────────────────────────────────

def test_requires_qa_scope_follows_existing_roles(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws, "WO-001", agent="codex", role="backend")
    write_wo(ws, "WO-002", agent="gemma", role="qa", path="tests/test_app.py")
    write_wo(
        ws, "WO-003", agent="claude", role="architecture", delim_type="doc",
        path="docs/notes.md",
    )
    write_wo(ws, "WO-004", agent="gemini", role="frontend", path="web/index.html")

    assert review_gate.requires_qa(ws, "WO-001") is True
    assert review_gate.requires_qa(ws, "WO-002") is False  # QA produces verdicts
    assert review_gate.requires_qa(ws, "WO-003") is False  # docs-only / architecture
    assert review_gate.requires_qa(ws, "WO-004") is True


def test_worker_completion_claim_alone_cannot_satisfy_qa(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)

    decision = review_gate.evaluate_qa_gate(ws, "WO-001")
    assert decision.passed is False
    assert decision.pending is True
    assert "no QA verdict" in (decision.detail or "")


def test_qa_pass_is_revision_bound_and_staleness_is_detected(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))

    assert review_gate.evaluate_qa_gate(ws, "WO-001").passed is True

    # Any deliverable change invalidates the reviewed revision.
    (ws / DELIVERABLE).write_text(GOOD_CODE + "\n# changed after review\n", encoding="utf-8")
    decision = review_gate.evaluate_qa_gate(ws, "WO-001")
    assert decision.passed is False
    assert "stale" in (decision.detail or "")


def test_qa_pass_without_executed_evidence_or_with_unmet_criteria_is_rejected(
    tmp_path: Path,
) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)

    no_evidence = qa_verdict(ws, evidence=[])
    review_gate.persist_qa_verdict(ws, no_evidence)
    assert review_gate.evaluate_qa_gate(ws, "WO-001").passed is False

    unmet = qa_verdict(ws, criteria_outcome="FAIL")
    review_gate.persist_qa_verdict(ws, unmet)
    decision = review_gate.evaluate_qa_gate(ws, "WO-001")
    assert decision.passed is False
    assert "criteria" in (decision.detail or "")


def test_failing_evidence_cannot_be_reported_as_pass(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(
        ws,
        qa_verdict(
            ws,
            evidence=[
                {"kind": "test", "command": "pytest -q", "result": "2 failed", "passed": False}
            ],
        ),
    )
    assert review_gate.evaluate_qa_gate(ws, "WO-001").passed is False


def test_worker_contracts_do_not_authorize_the_qa_verdict_channel() -> None:
    """Backing invariant for 'a worker cannot self-approve'."""
    assert qa_verdict_channel_required("gemma") is True
    for worker in ("codex", "gemini", "claude", "local-llm"):
        assert qa_verdict_channel_required(worker) is False


# ─── B. Supervisor QA gate ───────────────────────────────────────────

def test_completion_dispatches_independent_qa_and_blocks_acceptance(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = make_state(ws)
    developer_op(manager)

    result = sup._advance_executing(state)

    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" not in state.completed_wo_ids
    qa_turn = manager.last_turn_for_role("qa")
    assert qa_turn is not None
    assert qa_turn["agent_id"] == "gemma"
    assert "Mandatory QA review" in qa_turn["prompt"]
    assert state.qa_review_operations.get("WO-001") == qa_turn["op_id"]


def test_qa_pass_allows_progression_and_completion(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = make_state(ws)
    developer_op(manager)
    sup._advance_executing(state)  # dispatch QA
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))

    sup._advance_executing(state)

    assert "WO-001" in state.completed_wo_ids
    assert (ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml").is_file()


def test_qa_failure_prevents_acceptance_and_routes_bounded_rework(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = make_state(ws)
    developer_op(manager)
    review_gate.persist_qa_verdict(
        ws,
        qa_verdict(
            ws,
            verdict="FAIL",
            criteria_outcome="FAIL",
            findings=[
                {
                    "severity": "critical",
                    "detail": "sum() returns a string",
                    "reproduce": "pytest tests/test_app.py::test_sum",
                }
            ],
            required_actions=["Return an int from add()"],
        ),
    )

    result = sup._advance_executing(state)

    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert "WO-001" not in state.completed_wo_ids
    assert state.qa_reworks["WO-001"] == 1
    rework_turn = manager.last_turn_for_role("backend")
    assert rework_turn is not None
    assert "mandatory QA review" in rework_turn["prompt"]
    # The failed verdict is archived so repair must earn a fresh review.
    assert review_gate.load_qa_verdict(ws, "WO-001") is None
    assert list((ws / ".sync" / "reviews" / "qa").glob("WO-001-rejected-*.json"))


def test_exhausted_qa_rework_budget_escalates_safely(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = make_state(ws)
    developer_op(manager)
    review_gate.persist_qa_verdict(
        ws, qa_verdict(ws, verdict="FAIL", criteria_outcome="FAIL",
                       findings=[{"severity": "critical", "detail": "unmet"}])
    )
    state.qa_reworks["WO-001"] = state.max_qa_reworks

    result = sup._advance_executing(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert "exceeded QA rework limit" in str(state.error)


# ─── C. Architect final review ───────────────────────────────────────

def accept_ready_state(ws: Path) -> RunState:
    state = make_state(ws)
    state.completed_wo_ids = ["WO-000", "WO-001"]
    state.integration_wo_id = "WO-900"
    return state


def test_final_review_does_not_start_before_prerequisites(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)
    state.completed_wo_ids = ["WO-000"]  # WO-001 still unfinished

    sup._advance_final_review(state)

    assert manager.last_turn_for_role("architecture") is None
    assert state.phase in (Phase.EXECUTING, Phase.BLOCKED)


def test_final_review_requires_qa_pass(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)

    report = review_gate.evaluate_final_acceptance(state, ws)
    assert report.passed is False
    assert any("QA gate" in r for r in report.reasons)

    result = sup._advance_final_review(state)
    assert result in (AdvanceResult.TRANSITIONED, AdvanceResult.BLOCKED)
    assert manager.last_turn_for_role("architecture") is None


def test_architect_accepts_fully_compliant_implementation(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)

    state.phase = Phase.FINAL_REVIEW
    result = sup._advance_final_review(state)
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    review_turn = manager.last_turn_for_role("architecture")
    assert review_turn is not None
    assert "Architect final acceptance review" in review_turn["prompt"]
    assert state.final_review_operation_id == review_turn["op_id"]
    # No acceptance until the Architect verdict is persisted.
    assert state.phase == Phase.FINAL_REVIEW

    review_gate.persist_final_verdict(ws, final_verdict(ws))
    sup._advance_final_review(state)
    assert state.phase == Phase.PRODUCT_READY


def test_architect_rejects_missing_deliverable_or_unmet_criteria(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)

    rejected = final_verdict(ws, status="FAIL")
    rejected["findings"] = [
        {"severity": "critical", "detail": f"{DELIVERABLE} is missing the sum endpoint"}
    ]
    rejected["required_actions"] = [f"Implement the sum endpoint in {DELIVERABLE}"]
    review_gate.persist_final_verdict(ws, rejected)

    result = sup._advance_final_review(state)

    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.final_review_rounds == 1
    assert "WO-001" in state.worker_wo_ids
    assert "WO-001" not in state.completed_wo_ids
    # Corrective work invalidates the prior QA verdict.
    assert review_gate.load_qa_verdict(ws, "WO-001") is None
    assert review_gate.load_final_verdict(ws, "run-qa-001") is None
    assert manager.last_turn_for_role("backend") is not None


def test_repeated_final_rejection_is_bounded(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)
    state.final_review_rounds = state.max_final_review_rounds
    review_gate.persist_final_verdict(ws, final_verdict(ws, status="FAIL"))

    result = sup._advance_final_review(state)

    assert result == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert "FINAL_REVIEW_BUDGET_EXHAUSTED" in str(state.error)


def test_changed_plan_revision_invalidates_prior_acceptance(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    stale = final_verdict(ws)
    review_gate.persist_final_verdict(ws, stale)

    # Plan revised after the Architect review.
    (ws / "PLAN.md").write_text("# Approved Plan\n\nRevised scope.\n", encoding="utf-8")
    state = accept_ready_state(ws)

    ok, why = review_gate.validate_final_verdict(
        review_gate.load_final_verdict(ws, "run-qa-001"),
        ws,
        run_id="run-qa-001",
        plan_id="PLAN-001",
        required_wo_ids=["WO-001"],
    )
    assert ok is False
    assert "plan has changed" in why

    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    result = sup._advance_final_review(state)
    # Stale acceptance is retired and corrective work is required before a
    # fresh Architect review can authorise the run.
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert review_gate.load_final_verdict(ws, "run-qa-001") is None


def test_completed_work_orders_are_insufficient_without_final_verdict(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)
    state.phase = Phase.COMPLETE

    result = sup.advance(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert "FINAL_ACCEPTANCE_INVARIANT_VIOLATION" in str(state.error)


def test_gitops_cannot_complete_run_without_final_review_pass(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)
    state.phase = Phase.GITOPS
    resp = manager.start_turn("sess-qa", "release", role="gitops", agent_id="local-llm")
    manager.complete(resp["operation_id"])
    state.gitops_operation_id = resp["operation_id"]

    result = sup.advance(state)

    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
    assert state.phase != Phase.COMPLETE
    assert "FINAL_ACCEPTANCE_INVARIANT_VIOLATION" in str(state.error)


# ─── D. Restart safety ───────────────────────────────────────────────

def test_review_state_survives_daemon_restart(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    write_wo(ws)
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = accept_ready_state(ws)
    state.phase = Phase.FINAL_REVIEW
    state.qa_review_attempts = {"WO-001": 2}
    state.final_review_rounds = 1
    state.final_review_history = [{"event": "rejected"}]

    sup.save_run_state(state, ws)
    loaded = sup.load_run_state("run-qa-001", ws)

    assert loaded is not None
    assert loaded.phase == Phase.FINAL_REVIEW
    assert loaded.qa_review_attempts == {"WO-001": 2}
    assert loaded.final_review_rounds == 1
    assert loaded.final_review_history == [{"event": "rejected"}]

    snapshot = review_gate.review_status(loaded, ws)
    assert snapshot["acceptable"] is False
    assert snapshot["qa_reviews"][0]["passed"] is True
    assert snapshot["final_review"]["pending"] is True

    # Resume must still refuse acceptance until the Architect verdict is persisted.
    result = sup.advance(loaded)
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert loaded.phase == Phase.FINAL_REVIEW


def test_tui_adapter_reads_authoritative_review_state(tmp_path: Path) -> None:
    from validators.kernel.tui.adapter import StackMindTuiAdapter

    ws = make_workspace(tmp_path)
    write_wo(ws)

    class Client:
        def __init__(self) -> None:
            self.calls = []

        def run_reviews(self, session_id: str, run_id: str | None = None) -> dict[str, Any]:
            self.calls.append((session_id, run_id))
            return {
                "run_id": "run-qa-001",
                "qa_reviews": [],
                "final_review": {},
                "acceptable": False,
            }

    client = Client()
    adapter = StackMindTuiAdapter(client)  # type: ignore[arg-type]
    result = adapter.command(":reviews", session_id="sess-qa")

    assert client.calls == [("sess-qa", None)]
    assert result["acceptable"] is False


# ─── E. Deterministic end-to-end acceptance ─────────────────────────

def test_end_to_end_mandatory_gates(tmp_path: Path) -> None:
    """Plan → work order → QA fail → repair → QA pass → final reject → fix → accept."""
    ws = make_workspace(tmp_path)
    write_wo(ws)
    manager = FakeManager(ws)
    sup = LifecycleSupervisor(manager)
    state = make_state(ws)
    developer_op(manager)

    # 1. Worker completion does NOT complete the work order.
    sup._advance_executing(state)
    assert "WO-001" not in state.completed_wo_ids
    qa_turn = manager.last_turn_for_role("qa")
    assert qa_turn is not None

    # 2. QA fails → authorized bounded repair, acceptance blocked.
    manager.complete(qa_turn["op_id"])
    review_gate.persist_qa_verdict(
        ws,
        qa_verdict(ws, verdict="FAIL", criteria_outcome="FAIL",
                   findings=[{"severity": "critical", "detail": "add() returns a str"}],
                   required_actions=["Return int"]),
    )
    sup._advance_executing(state)
    assert "WO-001" not in state.completed_wo_ids
    assert state.qa_reworks["WO-001"] == 1

    # 3. Worker repairs the deliverable and resubmits.
    (ws / DELIVERABLE).write_text(GOOD_CODE + "# repaired\n", encoding="utf-8")
    developer_op(manager)
    sup._advance_executing(state)  # stale/failed verdict was archived → QA re-dispatched
    qa_round_two = manager.last_turn_for_role("qa")
    assert qa_round_two is not None and qa_round_two["op_id"] != qa_turn["op_id"]

    # 4. QA passes on the repaired revision → work order completes.
    manager.complete(qa_round_two["op_id"])
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    sup._advance_executing(state)
    assert "WO-001" in state.completed_wo_ids

    # 5. Integration review is deterministic and already recorded as passed.
    state.phase = Phase.FINAL_REVIEW
    sup._advance_final_review(state)
    assert manager.last_turn_for_role("architecture") is not None
    manager.complete(state.final_review_operation_id)

    # 6. Architect rejects an unmet acceptance criterion → corrective work.
    rejected = final_verdict(ws, status="FAIL")
    rejected["findings"] = [{"severity": "critical", "detail": f"{DELIVERABLE} lacks validation"}]
    rejected["required_actions"] = ["Add input validation"]
    review_gate.persist_final_verdict(ws, rejected)
    sup._advance_final_review(state)
    assert state.phase == Phase.EXECUTING
    assert "WO-001" not in state.completed_wo_ids
    assert review_gate.load_qa_verdict(ws, "WO-001") is None

    # 7. Correct the defect, re-verify deterministically, re-run QA.
    (ws / DELIVERABLE).write_text(
        GOOD_CODE + "def validate(x):\n    return isinstance(x, int)\n", encoding="utf-8"
    )
    developer_op(manager)
    sup._advance_executing(state)
    qa_round_three = manager.last_turn_for_role("qa")
    manager.complete(qa_round_three["op_id"])
    review_gate.persist_qa_verdict(ws, qa_verdict(ws))
    sup._advance_executing(state)
    assert "WO-001" in state.completed_wo_ids

    # 8. Fresh Architect final review passes against the current revisions.
    state.phase = Phase.FINAL_REVIEW
    state.final_review_operation_id = None
    sup._advance_final_review(state)
    review_gate.persist_final_verdict(ws, final_verdict(ws))
    sup._advance_final_review(state)
    assert state.phase == Phase.PRODUCT_READY

    # 9. Completion invariant holds only with the persisted, fresh verdict.
    state.phase = Phase.COMPLETE
    assert sup._advance_complete(state) == AdvanceResult.COMPLETE

    # 10. Stale evidence after acceptance revokes it (deliverable changed).
    (ws / DELIVERABLE).write_text(GOOD_CODE + "# post-acceptance edit\n", encoding="utf-8")
    state.phase = Phase.COMPLETE
    assert sup._advance_complete(state) == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.FINAL_REVIEW
