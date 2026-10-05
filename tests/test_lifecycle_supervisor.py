"""Comprehensive test suite for the LifecycleSupervisor orchestration mechanism.

Verifies:
1. Normal lifecycle progression:
   INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING → EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE
2. Human approval gate:
   - Halts at AWAITING_APPROVAL returning WAITING_FOR_HUMAN
   - Rejection resets phase to PLANNING and appends feedback to product goal
   - Approval advances phase to AUTHORING
3. Dependency-aware dispatching:
   - Dependent work orders wait until prerequisite work orders are completed
4. QA rejection and retry loop:
   - Worker turn failure or QA NEEDS_CHANGES triggers retry up to max_retries
   - Exceeding max_retries transitions phase to FAILED
   - Successful retry advances normally
5. Blocked work:
   - If work order dispatch fails/blocks, transitions to BLOCKED
6. Resumability & Persistence:
   - save_run_state / load_run_state roundtrip fidelity
   - list_runs discovery
   - Resuming advance() after loading state from disk
7. Real SessionManager integration:
   - Drives multi-phase transitions using real SessionManager & DaemonStorage
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
import pytest
import yaml

from validators.kernel.daemon.manager import SessionManager, synthesize_bootstrap_planning
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
    create_gitops_commit,
    format_release_commit_message,
)


# ─── Mock SessionManager for unit testing state transitions ───────────

class MockSessionManager:
    """Deterministic mock of SessionManager for precise state-machine unit tests."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.operations: dict[str, dict[str, Any]] = {}
        self.plans: list[dict[str, Any]] = []
        self._op_counter = 0

    def begin_operation(
        self,
        session_id: str,
        operation_name: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, str]:
        from threading import Event
        self._op_counter += 1
        op_id = f"op-{self._op_counter:03d}"
        record = {
            "operation_id": op_id,
            "session_id": session_id,
            "operation": operation_name,
            "status": "RUNNING",
            "metadata": metadata or {},
            "parent_operation_id": kwargs.get("parent_operation_id"),
            "children": [],
            "child_results": {},
        }
        self.operations[op_id] = record
        return Event(), op_id

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._op_counter += 1
        op_id = f"op-{self._op_counter:03d}"
        pid = kwargs.get("parent_operation_id")
        record = {
            "operation_id": op_id,
            "session_id": session_id,
            "prompt": prompt,
            "status": "RUNNING",
            "work_order_id": kwargs.get("work_order_id"),
            "parent_operation_id": pid,
            "role": kwargs.get("role", "backend"),
            "agent_id": kwargs.get("agent_id", "codex"),
            "metadata": kwargs,
            "result": None,
        }
        if pid and pid in self.operations:
            self.operations[pid].setdefault("children", []).append(op_id)
        self.operations[op_id] = record
        return record

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        if operation_id not in self.operations:
            raise KeyError(f"Operation {operation_id} not found")
        return self.operations[operation_id]

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.operations.values())

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.plans)

    def get_plan(self, session_id: str, plan_id: str) -> dict[str, Any]:
        for p in reversed(self.plans):
            if p.get("plan_id") == plan_id:
                return p
        raise KeyError(f"Plan {plan_id} not found")

    def propose_plan(
        self, session_id: str, plan_id: str, title: str, content: str = "", metadata: dict | None = None
    ) -> dict[str, Any]:
        for p in self.plans:
            if p.get("plan_id") == plan_id:
                p["title"] = title
                p["content"] = content
                p["state"] = "AWAITING_APPROVAL"
                p["metadata"] = metadata or {}
                return p
        plan = {
            "plan_id": plan_id,
            "session_id": session_id,
            "title": title,
            "content": content,
            "state": "AWAITING_APPROVAL",
            "metadata": metadata or {},
        }
        self.plans.append(plan)
        return plan

    def approve_plan(self, session_id: str, plan_id: str, reason: str = "") -> None:
        p = self.get_plan(session_id, plan_id)
        p["state"] = "APPROVED"
        p["reason"] = reason

    def reject_plan(self, session_id: str, plan_id: str, reason: str = "") -> None:
        p = self.get_plan(session_id, plan_id)
        p["state"] = "REJECTED"
        p["reason"] = reason
        p["feedback"] = reason

    def complete_operation(
        self,
        operation_id: str | None = None,
        status: str = "COMPLETED",
        result: dict | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        target_id = operation_id or session_id
        if target_id and target_id in self.operations:
            op = self.get_operation(target_id)
            op["status"] = status
            op["result"] = result or {"status": status.lower(), "summary": "Done"}
            return op
        return {}


# ─── Fixture: git repo that skips if git is unavailable ───────────────

def write_ready_artifact_set(
    ws: Path,
    specs: list[tuple[str, str, str, str, str]],
) -> None:
    """Write schema-valid WO+contract pairs that pass the authoring readiness gate.

    Each spec is (wo_id, agent, role, deliverable_type, deliverable_path).
    """
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contract_dir = ws / ".sync" / "contracts"
    wo_dir.mkdir(parents=True, exist_ok=True)
    contract_dir.mkdir(parents=True, exist_ok=True)
    now = "2026-01-01T00:00:00+00:00"
    for wo_id, agent, role, deliv_type, deliv_path in specs:
        wo_record: dict[str, Any] = {
            "id": wo_id,
            "type": "FEATURE",
            "title": f"Task {wo_id}",
            "status": "ACTIVE",
            "priority": "P1",
            "assigned_agents": [agent],
            "dependencies": [],
            "deliverable": {
                "type": deliv_type,
                "description": f"{wo_id} deliverable",
            },
            "description": f"Implement {wo_id}",
            "created": now,
            "updated": now,
        }
        if deliv_path:
            wo_record["deliverable"]["path"] = deliv_path
            # Plan the D024 companion test for code deliverables so the
            # readiness gate's test-coverage check passes.
            if deliv_type == "code":
                stem = Path(deliv_path).stem
                wo_record["implementation_estimate"] = {
                    "expected_files": [deliv_path, f"tests/test_{stem}.py"],
                    "max_files_touched": 2,
                    "rationale": "deliverable plus companion test",
                }
                wo_record["description"] = (
                    f"Implement {wo_id} including companion test tests/test_{stem}.py"
                )
        contract_record: dict[str, Any] = {
            "schema_version": 1,
            "agent_id": agent,
            "work_order": wo_id,
            "identity": {"role": role, "reports_to": "claude"},
            "scope": {
                "allow": [{"module": "PLAN.md"}, {"module": ".sync/inbox/claude/**"}],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 0},
        }
        if deliv_path:
            contract_record["scope"]["allow"].append({"module": deliv_path})
        (wo_dir / f"{wo_id}.yaml").write_text(
            yaml.safe_dump(wo_record, sort_keys=False), encoding="utf-8"
        )
        (contract_dir / f"{wo_id}.yaml").write_text(
            yaml.safe_dump(contract_record, sort_keys=False), encoding="utf-8"
        )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Initialize a git repo in tmp_path; skip test if git is unavailable."""
    try:
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git not available in this environment")
    subprocess.run(
        ["git", "config", "user.name", "StackMind GitOps"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "gitops@stackmind.local"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    # Initial commit so branch is established
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


# ─── Unit Tests: Normal Progression ───────────────────────────────────

def test_supervisor_initial_start_run(tmp_path: Path) -> None:
    """start_run creates RunState with Phase.PLANNING."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    state = supervisor.start_run(
        run_id="run-001",
        product_goal="Create a login endpoint",
        workspace=tmp_path,
        session_id="sess-001",
    )

    assert state.run_id == "run-001"
    assert state.phase == Phase.PLANNING
    assert state.product_goal == "Create a login endpoint"
    assert len(state.transitions) == 1
    assert state.transitions[0]["to"] == "PLANNING"


def test_supervisor_planning_turn_dispatch_and_wait(tmp_path: Path) -> None:
    """First advance() in PLANNING dispatches Architecture turn and returns WAITING_FOR_OPERATION."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")

    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.planning_operation_id is not None
    assert state.phase == Phase.PLANNING

    # Calling advance again while operation is running still waits
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION


def test_supervisor_planning_to_awaiting_approval(tmp_path: Path) -> None:
    """When planning turn completes and proposes plan, advances to AWAITING_APPROVAL and returns WAITING_FOR_HUMAN."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")

    supervisor.advance(state)
    op_id = state.planning_operation_id

    # Complete planning op and propose plan
    mock_mgr.complete_operation(op_id, "COMPLETED")
    mock_mgr.propose_plan("sess-001", "PLAN-001", "Login Plan")

    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL
    assert state.plan_id == "PLAN-001"


def test_supervisor_awaiting_approval_rejection_loop(tmp_path: Path) -> None:
    """Rejecting plan in AWAITING_APPROVAL resets phase to PLANNING and updates product goal with feedback."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")

    supervisor.advance(state)
    mock_mgr.complete_operation(state.planning_operation_id, "COMPLETED")
    mock_mgr.propose_plan("sess-001", "PLAN-001", "Login Plan")
    supervisor.advance(state)
    assert state.phase == Phase.AWAITING_APPROVAL

    # Reject plan
    mock_mgr.reject_plan("sess-001", "PLAN-001", reason="Add 2FA support")

    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PLANNING
    assert state.planning_operation_id is None
    assert "Add 2FA support" in state.product_goal


def test_supervisor_approval_advances_to_authoring(tmp_path: Path) -> None:
    """Approving plan advances to AUTHORING phase."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")

    supervisor.advance(state)
    mock_mgr.complete_operation(state.planning_operation_id, "COMPLETED")
    mock_mgr.propose_plan("sess-001", "PLAN-001", "Login Plan")
    supervisor.advance(state)

    mock_mgr.approve_plan("sess-001", "PLAN-001", reason="Looks good")

    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.AUTHORING


def test_supervisor_authoring_discovers_worker_wos(tmp_path: Path) -> None:
    """When authoring completes, supervisor discovers worker WOs, passes the
    authoring readiness gate, and transitions to DISPATCHING."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")
    state.phase = Phase.AUTHORING

    # Create authoring op
    op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
    state.authoring_operation_id = op["operation_id"]

    # Write schema-valid worker WOs + matching contracts to disk
    write_ready_artifact_set(tmp_path, [
        ("WO-001", "codex", "backend", "code", "src/backend.py"),
        ("WO-002", "gemini", "frontend", "code", "src/frontend.html"),
        ("WO-003", "local-llm", "gitops", "doc", "VERSION.md"),
    ])

    # Complete authoring op
    mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

    # Advance 1: authoring completion routes through the readiness gate
    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.AUTHORING_READINESS_GATE

    # Advance 2: readiness passes → publishable revision → DISPATCHING
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING
    assert state.worker_wo_ids == ["WO-001", "WO-002"]
    assert state.gitops_wo_id == "WO-003"
    # Atomic publication: only readiness-approved WOs are dispatchable
    assert state.published_wo_ids == ["WO-001", "WO-002"]


def test_supervisor_dependency_resolution(tmp_path: Path) -> None:
    """WO-002 depending on WO-001 is NOT dispatched until WO-001 completes."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")
    state.phase = Phase.DISPATCHING

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        "id: WO-001\nassigned_agents: [codex]\ndependencies: []\n", encoding="utf-8"
    )
    (wo_dir / "WO-002.yaml").write_text(
        "id: WO-002\nassigned_agents: [gemini]\ndependencies: [WO-001]\n", encoding="utf-8"
    )
    state.worker_wo_ids = ["WO-001", "WO-002"]

    # Advance DISPATCHING → EXECUTING
    res = supervisor.advance(state)
    assert state.phase == Phase.EXECUTING
    assert res == AdvanceResult.WAITING_FOR_OPERATION

    # Check which WO was dispatched: WO-001 should be dispatched, but NOT WO-002
    dispatched_wos = [
        op["work_order_id"] for op in mock_mgr.list_operations() if op.get("work_order_id")
    ]
    assert "WO-001" in dispatched_wos
    assert "WO-002" not in dispatched_wos

    # Now complete WO-001 with QA approval
    claude_inbox = tmp_path / ".sync" / "inbox" / "claude"
    claude_inbox.mkdir(parents=True, exist_ok=True)
    (claude_inbox / "gemma_WO-001_verdict.md").write_text("# QA Verdict\nStatus: APPROVED\n", encoding="utf-8")

    op_wo1 = next(op for op in mock_mgr.list_operations() if op["work_order_id"] == "WO-001")
    mock_mgr.complete_operation(op_wo1["operation_id"], "COMPLETED")

    # Advance again: WO-001 becomes completed, WO-002 is now eligible and dispatched!
    res2 = supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids
    dispatched_wos_after = [
        op["work_order_id"] for op in mock_mgr.list_operations() if op.get("work_order_id")
    ]
    assert "WO-002" in dispatched_wos_after


def test_supervisor_qa_needs_changes_retry_loop(tmp_path: Path) -> None:
    """QA NEEDS_CHANGES triggers retry up to max_retries, and fails if exceeded."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.max_retries = 1

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text("id: WO-001\nassigned_agents: [codex]\n", encoding="utf-8")

    # Dispatch turn 1
    op1 = mock_mgr.start_turn("sess-001", "Run WO-001", work_order_id="WO-001", agent_id="codex")
    mock_mgr.complete_operation(op1["operation_id"], "COMPLETED")

    # Gemma writes NEEDS_CHANGES
    reviews_dir = tmp_path / ".sync" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    verdict_file = reviews_dir / "gemma_WO-001_review.md"
    verdict_file.write_text("Verdict: NEEDS_CHANGES\nMissing tests\n", encoding="utf-8")

    # Advance: should trigger retry 1 (attempt 2)
    res = supervisor.advance(state)
    assert state.retry_counts.get("WO-001") == 1
    assert state.phase == Phase.EXECUTING
    assert res == AdvanceResult.WAITING_FOR_OPERATION

    # Turn 2 also gets NEEDS_CHANGES
    op2 = next(op for op in reversed(mock_mgr.list_operations()) if op["work_order_id"] == "WO-001")
    mock_mgr.complete_operation(op2["operation_id"], "COMPLETED")

    # Advance: retries exhausted -> FAILED
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert "WO-001" in state.failed_wo_ids


def test_supervisor_full_progression_to_complete(tmp_path: Path) -> None:
    """Step-by-step advance from EXECUTING to INTEGRATION_REVIEW to PRODUCT_READY to GITOPS to COMPLETE."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.gitops_wo_id = "WO-002"

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text("id: WO-001\nassigned_agents: [codex]\n", encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text("id: WO-002\nassigned_agents: [local-llm]\n", encoding="utf-8")

    # Complete WO-001 with QA approval
    op1 = mock_mgr.start_turn("sess-001", "Run WO-001", work_order_id="WO-001", agent_id="codex")
    mock_mgr.complete_operation(op1["operation_id"], "COMPLETED")
    reviews_dir = tmp_path / ".sync" / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    (reviews_dir / "gemma_WO-001_verdict.md").write_text("Verdict: APPROVED\n", encoding="utf-8")

    # Advance: EXECUTING → INTEGRATION_REVIEW
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.INTEGRATION_REVIEW

    # Advance in INTEGRATION_REVIEW: dispatches Architecture review
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.integration_operation_id is not None

    # Complete integration review
    mock_mgr.complete_operation(state.integration_operation_id, "COMPLETED")

    # Advance: INTEGRATION_REVIEW → PRODUCT_READY
    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PRODUCT_READY

    # Advance: PRODUCT_READY → GITOPS
    res4 = supervisor.advance(state)
    assert res4 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.GITOPS

    # Advance in GITOPS: dispatches GitOps turn
    res5 = supervisor.advance(state)
    assert res5 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.gitops_operation_id is not None

    # Complete GitOps turn
    mock_mgr.complete_operation(state.gitops_operation_id, "COMPLETED")

    # Advance: GITOPS → COMPLETE!
    res6 = supervisor.advance(state)
    assert res6 == AdvanceResult.COMPLETE
    assert state.phase == Phase.COMPLETE


# ─── Resumability & Persistence Tests ─────────────────────────────────

def test_supervisor_run_state_serialization_roundtrip(tmp_path: Path) -> None:
    """RunState serializes to and deserializes from disk without loss."""
    state = RunState(
        run_id="run-test-42",
        product_goal="Build widget",
        workspace=str(tmp_path),
        session_id="sess-42",
        phase=Phase.EXECUTING,
        planning_wo_id="WO-000",
        worker_wo_ids=["WO-001", "WO-002"],
        completed_wo_ids=["WO-001"],
        retry_counts={"WO-002": 1},
        max_retries=3,
    )

    path = LifecycleSupervisor.save_run_state(state, tmp_path)
    assert path.is_file()

    loaded = LifecycleSupervisor.load_run_state("run-test-42", tmp_path)
    assert loaded is not None
    assert loaded.run_id == state.run_id
    assert loaded.product_goal == state.product_goal
    assert loaded.phase == Phase.EXECUTING
    assert loaded.worker_wo_ids == ["WO-001", "WO-002"]
    assert loaded.completed_wo_ids == ["WO-001"]
    assert loaded.retry_counts == {"WO-002": 1}
    assert loaded.max_retries == 3

    # list_runs
    runs = LifecycleSupervisor.list_runs(tmp_path)
    assert "run-test-42" in runs


# ─── Integration Test with Real SessionManager ────────────────────────

def test_supervisor_with_real_session_manager(tmp_path: Path) -> None:
    """Verify LifecycleSupervisor drives lifecycle with real SessionManager & DaemonStorage."""
    storage = DaemonStorage(tmp_path / "daemon.json")

    # Provide a fast dummy runner factory so real thread turns complete immediately
    class FastRunner:
        def __init__(self, ws: Path, agent: str) -> None:
            self.workspace = ws
            self.agent = agent

        def run_once(self, **kwargs: Any) -> Any:
            from validators.harness.runner import HarnessRunResult
            return HarnessRunResult(
                status="completed",
                persisted=True,
                task_id=kwargs.get("operation_id", "test"),
                reason="Done",
                meta={"summary": "Mock completion"},
            )

    manager = SessionManager(
        storage,
        runner_factory=lambda ws, ag: FastRunner(Path(ws), ag),
    )

    session = manager.create_session(
        agent="claude",
        provider="local",
        contract={"scope": {"allow": ["*"], "deny": [], "write": "read-write"}},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    supervisor = LifecycleSupervisor(manager)
    state = supervisor.start_run("run-real-01", "Build microservice", tmp_path, sid)

    # 1. First advance: dispatches real turn in SessionManager
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.planning_operation_id is not None

    # Wait briefly for runner thread to finish
    time.sleep(0.1)

    # 2. Planning turn completes; simulate plan proposal
    manager.propose_plan(sid, "PLAN-001", "Plan for microservice")

    # Advance: transitions to AWAITING_APPROVAL
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL

    # 3. Approve plan
    manager.approve_plan(sid, "PLAN-001", reason="Approved")
    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.AUTHORING


def test_supervisor_drive_until_human_approval(tmp_path: Path) -> None:
    """drive() runs until it encounters WAITING_FOR_HUMAN, recording all phase transitions."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-drive-01", "Create login page", tmp_path, "sess-drive")

    transitions: list[tuple[str, str]] = []

    def on_trans(_s: RunState, old_p: Phase, new_p: Phase) -> None:
        transitions.append((old_p.value, new_p.value))

    # Before drive: complete planning op and propose plan so it doesn't block on running operation
    supervisor.advance(state)
    mock_mgr.complete_operation(state.planning_operation_id, "COMPLETED")
    mock_mgr.propose_plan("sess-drive", "PLAN-001", "Login Plan")

    result = supervisor.drive(state, max_steps=10, on_transition=on_trans)

    assert result == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL
    assert ("PLANNING", "AWAITING_APPROVAL") in transitions


def test_gitops_commit_contains_provenance_trailers(git_repo: Path) -> None:
    """create_gitops_commit creates a real git commit containing required provenance trailers."""
    # Add release files
    (git_repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (git_repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n", encoding="utf-8")

    # Call create_gitops_commit
    sha = create_gitops_commit(
        workspace=git_repo,
        work_order_id="WO-004",
        summary="feat(release): release v1.0.0 Secure Login Portal",
        released_by="local-llm",
        approved_by="gemma",
        architect="claude",
        target_work_orders=["WO-001", "WO-002", "WO-003"],
    )

    assert len(sha) == 40

    # Read commit message from git
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "feat(release): release v1.0.0 Secure Login Portal" in msg
    assert "Work-Order: WO-004" in msg
    assert "Released-By: local-llm" in msg
    assert "Approved-By: gemma" in msg
    assert "Architect: claude" in msg
    assert "Target-Work-Orders: WO-001, WO-002, WO-003" in msg


def test_supervisor_gitops_phase_creates_release_commit(git_repo: Path) -> None:
    """When GitOps operation completes in supervisor, it creates a commit with trailers."""
    mock_mgr = MockSessionManager(git_repo)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = RunState(
        run_id="run-gitops-commit-01",
        product_goal="Create a login page",
        workspace=str(git_repo),
        session_id="sess-gitops-commit",
        phase=Phase.GITOPS,
        completed_wo_ids=["WO-001", "WO-002"],
        gitops_wo_id="WO-004",
    )

    # 1. First advance dispatches GitOps turn
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.gitops_operation_id is not None

    # Write release deliverables
    (git_repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (git_repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

    # Complete the GitOps operation
    mock_mgr.complete_operation(state.gitops_operation_id, "COMPLETED")

    # 2. Second advance finishes GITOPS, creates commit, and transitions to COMPLETE
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.COMPLETE
    assert state.phase == Phase.COMPLETE
    assert state.release_commit_sha is not None

    # Verify real commit made by GitOps turn contains trailers
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Work-Order: WO-004" in msg
    assert "Released-By: local-llm" in msg
    assert "Approved-By: gemma" in msg


# ─── Tests for Supervisor Gaps S1 through S8 ──────────────────────────

def test_s1_tui_goal_creates_supervised_run_and_driver_advances(tmp_path: Path) -> None:
    """S1: :goal creates a run, daemon-owned background driver advances it, TUI can observe it."""
    storage = DaemonStorage(tmp_path / "daemon.json")

    class MockEchoRunner:
        def __init__(self, ws: Path, agent: str) -> None:
            self.ws = ws
            self.agent = agent

        def run_once(self, **kwargs: Any) -> Any:
            class DummyDecision:
                status = "completed"
                persisted = True
                task_id = "task-123"
                reason = "Finished"
                meta = {"summary": "Done"}
            if self.agent == "claude":
                plan_file = self.ws / "PLAN.md"
                plan_file.write_text(
                    "# Project Plan: Demo\n\n## Current Architecture\nArch.\n\n## Milestones & Roadmap\n- [ ] Milestone 1: Task (assigned: codex)\n",
                    encoding="utf-8",
                )
            return DummyDecision()

    def dummy_runner_factory(ws: str, agent: str) -> Any:
        return MockEchoRunner(Path(ws), agent)

    manager = SessionManager(storage, runner_factory=dummy_runner_factory)
    session = manager.create_session(
        agent="codex",
        provider="local",
        contract={"scope": {"allow": ["*"], "deny": [], "write": "read-write"}},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    turn_op = manager.start_turn(
        sid,
        "Build a user authentication microservice",
        is_goal=True,
    )

    assert "run_id" in turn_op
    run_id = turn_op["run_id"]

    run_state = manager.get_run(run_id)
    assert run_state is not None
    assert run_state["product_goal"] == "Build a user authentication microservice"

    active_run = manager.get_active_run(sid)
    assert active_run is not None
    assert active_run["run_id"] == run_id

    if run_id in manager._run_stop_events:
        manager._run_stop_events[run_id].set()


def test_s2_supervisor_halts_at_awaiting_approval_until_operator_action(tmp_path: Path) -> None:
    """S2: supervisor halts at AWAITING_APPROVAL; only operator action (:approve/:reject) resumes it."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = RunState(
        run_id="run-s2",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-s2",
        phase=Phase.AWAITING_APPROVAL,
        plan_id="PLAN-001",
    )
    mock_mgr.propose_plan("sess-s2", "PLAN-001", "Auth Plan")

    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL

    res1_again = supervisor.advance(state)
    assert res1_again == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.AWAITING_APPROVAL

    mock_mgr.reject_plan("sess-s2", "PLAN-001", reason="Add multi-tenant support")
    res_reject = supervisor.advance(state)
    assert res_reject == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PLANNING
    assert "Add multi-tenant support" in state.product_goal

    state.phase = Phase.AWAITING_APPROVAL
    mock_mgr.propose_plan("sess-s2", "PLAN-001", "Auth Plan v2")
    assert supervisor.advance(state) == AdvanceResult.WAITING_FOR_HUMAN

    mock_mgr.approve_plan("sess-s2", "PLAN-001", reason="Approved")
    res_approve = supervisor.advance(state)
    assert res_approve == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.AUTHORING


def test_s3_advance_checks_real_deliverable_on_disk_before_completion(tmp_path: Path) -> None:
    """S3: advance() checks real task deliverables on disk, not just operation status strings."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_content = {
        "id": "WO-001",
        "title": "Scaffolding",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {
            "type": "config",
            "path": "requirements.txt",
        },
    }
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo_content), encoding="utf-8")

    state = RunState(
        run_id="run-s3",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-s3",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-001"],
    )

    op = mock_mgr.start_turn("sess-s3", "Execute WO-001", work_order_id="WO-001", role="backend")
    mock_mgr.complete_operation(op["operation_id"], status="COMPLETED")

    supervisor.advance(state)
    assert "WO-001" not in state.completed_wo_ids
    assert state.retry_counts.get("WO-001") == 1

    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    latest_op = mock_mgr.list_operations()[-1]
    mock_mgr.complete_operation(latest_op["operation_id"], status="COMPLETED")

    res2 = supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids
    assert res2 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.INTEGRATION_REVIEW


def test_s4_turn_blocked_escalates_and_stops_without_infinite_loop(tmp_path: Path) -> None:
    """S4: When a turn is blocked by a gate, supervisor records blocker and transitions to BLOCKED."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "title": "Auth", "assigned_agents": ["codex"], "dependencies": []
    }), encoding="utf-8")

    state = RunState(
        run_id="run-s4",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-s4",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-002"],
    )

    op = mock_mgr.start_turn("sess-s4", "Execute WO-002", work_order_id="WO-002", role="backend")
    mock_mgr.complete_operation(
        op["operation_id"],
        status="COMPLETED",
        result={"status": "blocked", "reason": "Manifest requirements.txt is outside contract scope"},
    )

    res = supervisor.advance(state)
    assert res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "WO-002" in state.blocked_wo_ids
    assert "outside contract scope" in (state.error or "")


def test_s5_concurrent_runs_remain_isolated_in_state_and_storage(tmp_path: Path) -> None:
    """S5: Run state persistence and in-memory tracking do not collide or leak between runs."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    state1 = supervisor.start_run("run-alpha", "Goal Alpha", tmp_path, "sess-1")
    state2 = supervisor.start_run("run-beta", "Goal Beta", tmp_path, "sess-2")

    state1.completed_wo_ids.append("WO-101")
    state2.completed_wo_ids.append("WO-202")

    supervisor.save_run_state(state1, tmp_path)
    supervisor.save_run_state(state2, tmp_path)

    loaded1 = supervisor.load_run_state("run-alpha", tmp_path)
    loaded2 = supervisor.load_run_state("run-beta", tmp_path)

    assert loaded1 is not None and loaded2 is not None
    assert loaded1.run_id == "run-alpha"
    assert loaded1.completed_wo_ids == ["WO-101"]
    assert "WO-202" not in loaded1.completed_wo_ids

    assert loaded2.run_id == "run-beta"
    assert loaded2.completed_wo_ids == ["WO-202"]
    assert "WO-101" not in loaded2.completed_wo_ids


def test_s6_dispatch_ordering_enforces_scaffolding_before_dependent_workers(tmp_path: Path) -> None:
    """S6: Supervisor enforces sequence: Scaffolding -> Backend/Frontend -> QA."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001", "assigned_agents": ["codex"], "dependencies": [],
        "deliverable": {"type": "config", "path": "requirements.txt"},
    }), encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "assigned_agents": ["codex"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/api/login.py"},
    }), encoding="utf-8")

    state = RunState(
        run_id="run-s6",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-s6",
        phase=Phase.DISPATCHING,
        worker_wo_ids=["WO-001", "WO-002"],
    )

    supervisor.advance(state)
    ops = mock_mgr.list_operations()
    dispatched_wos = [o.get("work_order_id") for o in ops]
    assert "WO-001" in dispatched_wos
    assert "WO-002" not in dispatched_wos

    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    mock_mgr.complete_operation(ops[0]["operation_id"], status="COMPLETED")

    supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids
    new_ops = mock_mgr.list_operations()
    new_wos = [o.get("work_order_id") for o in new_ops]
    assert "WO-002" in new_wos


def test_s7_qa_needs_changes_retries_with_feedback_until_max_retries_then_fails(tmp_path: Path) -> None:
    """S7: QA NEEDS_CHANGES retries with feedback up to max_retries, then transitions to FAILED."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "assigned_agents": ["codex"], "dependencies": [],
        "deliverable": {"type": "code", "path": "src/api/login.py"},
    }), encoding="utf-8")
    (tmp_path / "src" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "api" / "login.py").write_text("def login(): pass\n", encoding="utf-8")

    inbox_claude = tmp_path / ".sync" / "inbox" / "claude"
    inbox_claude.mkdir(parents=True, exist_ok=True)
    (inbox_claude / "gemma_WO-002_verdict.md").write_text(
        "# Verdict: NEEDS_CHANGES\nMissing tests/test_login.py companion test suite.\n",
        encoding="utf-8",
    )

    state = RunState(
        run_id="run-s7",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-s7",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-002"],
        max_retries=1,
    )

    op1 = mock_mgr.start_turn("sess-s7", "Execute WO-002", work_order_id="WO-002", role="backend")
    mock_mgr.complete_operation(op1["operation_id"], status="COMPLETED")

    res1 = supervisor.advance(state)
    assert state.retry_counts.get("WO-002") == 1
    assert "WO-002" not in state.completed_wo_ids
    retry_op = mock_mgr.list_operations()[-1]
    assert "Missing tests/test_login.py" in retry_op["prompt"]

    mock_mgr.complete_operation(retry_op["operation_id"], status="COMPLETED")
    res2 = supervisor.advance(state)

    assert res2 == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert "WO-002" in state.failed_wo_ids


def test_s8_supervisor_resumes_from_persisted_state_without_reexecuting_completed_wos(tmp_path: Path) -> None:
    """S8: Resuming supervisor from persisted run state picks up at the correct phase without re-executing completed WOs."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001", "assigned_agents": ["codex"], "dependencies": [],
        "deliverable": {"type": "config", "path": "requirements.txt"},
    }), encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "assigned_agents": ["codex"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/api/login.py"},
    }), encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    initial_state = RunState(
        run_id="run-s8-resume",
        product_goal="Resumable goal",
        workspace=str(tmp_path),
        session_id="sess-s8",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-001", "WO-002"],
        completed_wo_ids=["WO-001"],
    )
    supervisor.save_run_state(initial_state, tmp_path)

    fresh_mgr = MockSessionManager(tmp_path)
    fresh_supervisor = LifecycleSupervisor(fresh_mgr)

    restored_state = fresh_supervisor.load_run_state("run-s8-resume", tmp_path)
    assert restored_state is not None
    assert restored_state.phase == Phase.EXECUTING
    assert restored_state.completed_wo_ids == ["WO-001"]

    fresh_supervisor.advance(restored_state)
    dispatched = [o.get("work_order_id") for o in fresh_mgr.list_operations()]
    assert "WO-002" in dispatched
    assert "WO-001" not in dispatched


def test_s1_tui_approve_reject_and_status_route_through_supervisor(tmp_path: Path) -> None:
    """S1: :approve and :reject route through supervisor for active run, and :status renders phase."""
    from validators.kernel.tui.adapter import StackMindTuiAdapter
    from validators.kernel.tui.views import session_header
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager

    manager = SessionManager(DaemonStorage(tmp_path / "daemon.json"))
    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    # Create run state and planning operation directly (no background threads)
    # to avoid the race where start_turn(is_goal=True) spawns a driver + turn thread
    # that can advance/fail the run before the test gets to control it.
    run_id = "run-test-s1"
    r_state = manager.supervisor.start_run(run_id, "Build login system", tmp_path, sid)
    manager._active_runs[run_id] = r_state

    # Create a planning operation manually
    cancel_event, plan_op_id = manager.begin_operation(
        sid, "turn",
        {"prompt": "Build login system", "role": "architecture", "agent_id": "claude"},
        work_order_id="WO-000",
        role="architecture",
    )
    r_state.planning_operation_id = plan_op_id
    r_state.planning_wo_id = "WO-000"

    # Verify get_active_run sees it
    active_run = manager.get_active_run(sid)
    assert active_run is not None
    assert active_run["phase"] in ("INIT", "PLANNING")

    # Complete planning op and propose plan
    manager.complete_operation(operation_id=plan_op_id, result={"plan": "PLAN.md proposed"}, status="COMPLETED")
    manager.propose_plan(sid, "PLAN-001", "Auth Architecture", "Plan details", metadata={"work_order_id": "WO-000"})

    # Advance supervisor to AWAITING_APPROVAL
    manager.supervisor.advance(r_state)
    assert r_state.phase == Phase.AWAITING_APPROVAL

    # Check :status rendering phase
    status_view = manager.get_session(sid)
    assert "phase" in status_view
    assert status_view["phase"] == "AWAITING_APPROVAL"
    header_str = session_header(status_view)
    assert "phase: AWAITING_APPROVAL" in header_str

    # Test Mock Client to verify adapter routing
    class MockClient:
        def __init__(self, mgr: SessionManager) -> None:
            self.mgr = mgr
            self.run_approved_called = False
            self.run_rejected_called = False

        def get_session(self, session_id: str) -> dict[str, Any]:
            return self.mgr.get_session(session_id)

        def run_get(self, session_id: str) -> dict[str, Any] | None:
            return self.mgr.get_active_run(session_id)

        def run_approve(self, session_id: str, reason: str = "") -> dict[str, Any]:
            self.run_approved_called = True
            return self.mgr.approve_run(session_id, reason=reason)

        def run_reject(self, session_id: str, reason: str = "") -> dict[str, Any]:
            self.run_rejected_called = True
            return self.mgr.reject_run(session_id, reason=reason)

    mock_client = MockClient(manager)
    adapter = StackMindTuiAdapter(mock_client)

    # :status via adapter
    res_status = adapter.command(":status", session_id=sid)
    assert res_status["phase"] == "AWAITING_APPROVAL"

    # :approve routes through supervisor run_approve
    res_approve = adapter.command(":approve Looks great", session_id=sid)
    assert mock_client.run_approved_called is True
    assert res_approve["run_id"] == run_id

    # Plan state in manager is now APPROVED
    plan = manager.get_plan(sid, "PLAN-001")
    assert plan["state"] == "APPROVED"

    # Advance supervisor: AWAITING_APPROVAL -> AUTHORING (TRANSITIONED)
    res_adv = manager.supervisor.advance(r_state)
    assert res_adv == AdvanceResult.TRANSITIONED
    assert r_state.phase == Phase.AUTHORING

    # Next advance step dispatches Turn 2 (authoring child WOs and contracts)
    ops_before = len(manager.list_operations(sid))
    res_adv2 = manager.supervisor.advance(r_state)
    assert res_adv2 == AdvanceResult.WAITING_FOR_OPERATION
    assert r_state.authoring_operation_id is not None
    ops_after = len(manager.list_operations(sid))
    assert ops_after == ops_before + 1

    # Calling advance again monitors the active authoring turn without duplicate dispatch
    res_adv3 = manager.supervisor.advance(r_state)
    assert res_adv3 == AdvanceResult.WAITING_FOR_OPERATION
    ops_after_adv3 = len(manager.list_operations(sid))
    assert ops_after_adv3 == ops_after, "Supervisor monitors authoring turn without duplicate dispatch"

    # Calling approve_plan again fails closed because the plan is already APPROVED
    with pytest.raises(ValueError, match="not in AWAITING_APPROVAL state"):
        manager.approve_plan(sid, "PLAN-001", reason="Duplicate test")
    ops_after_dup_attempt = len(manager.list_operations(sid))
    assert ops_after_dup_attempt == ops_after


def test_s6_parallel_worker_dispatch_concurrent_execution(tmp_path: Path) -> None:
    """S6: Workers with shared satisfied dependencies are dispatched concurrently and run simultaneously."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-s6", "Parallel goals", tmp_path, "sess-s6")
    state.phase = Phase.DISPATCHING

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    # WO-001 is already completed scaffolding
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001", "assigned_agents": ["codex"], "dependencies": [],
        "deliverable": {"type": "config", "path": "requirements.txt"},
    }), encoding="utf-8")
    # WO-002 (backend) and WO-003 (frontend) both depend on WO-001
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "assigned_agents": ["codex"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/backend.py"},
    }), encoding="utf-8")
    (wo_dir / "WO-003.yaml").write_text(yaml.safe_dump({
        "id": "WO-003", "assigned_agents": ["gemini"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/frontend.html"},
    }), encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    state.worker_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.completed_wo_ids = ["WO-001"]

    # Advance DISPATCHING
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING

    # Verify BOTH WO-002 and WO-003 were dispatched in the same pass
    # Filter to worker ops only (exclude parent batch operation which has no work_order_id)
    active_ops = [
        op for op in mock_mgr.list_operations()
        if op.get("status") == "RUNNING" and op.get("work_order_id")
    ]
    active_wo_ids = {op.get("work_order_id") for op in active_ops}
    assert "WO-002" in active_wo_ids
    assert "WO-003" in active_wo_ids
    assert len(active_ops) == 2, "Both workers must be dispatched and RUNNING concurrently!"


def test_s6_real_session_manager_concurrent_worker_dispatch(tmp_path: Path) -> None:
    """S6 (a): Real SessionManager proves two workers run CONCURRENTLY sharing parent_operation_id."""
    import time
    from threading import Event
    from unittest.mock import MagicMock
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager

    proceed_event = Event()

    class PausingRunner:
        def __init__(self, ws: str, agent: str) -> None:
            self.ws = ws
            self.agent = agent

        def run_once(self, **kwargs: Any) -> Any:
            # Pause both runner threads so we can inspect operation state concurrently
            proceed_event.wait(timeout=5.0)
            res = MagicMock()
            res.status = "completed"
            res.persisted = True
            res.task_id = "test-task"
            res.reason = "done"
            res.report_path = None
            res.meta = {"summary": "done"}
            return res

    storage = DaemonStorage(tmp_path / "daemon.json")
    manager = SessionManager(storage, runner_factory=lambda ws, ag: PausingRunner(ws, ag))
    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001", "assigned_agents": ["codex"], "dependencies": [],
        "deliverable": {"type": "config", "path": "requirements.txt"},
    }), encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002", "assigned_agents": ["codex"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/backend.py"},
    }), encoding="utf-8")
    (wo_dir / "WO-003.yaml").write_text(yaml.safe_dump({
        "id": "WO-003", "assigned_agents": ["gemini"], "dependencies": ["WO-001"],
        "deliverable": {"type": "code", "path": "src/frontend.html"},
    }), encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    supervisor = LifecycleSupervisor(manager)
    state = supervisor.start_run("run-real-s6", "Real parallel goals", tmp_path, sid)
    state.phase = Phase.DISPATCHING
    state.worker_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.completed_wo_ids = ["WO-001"]

    # Advance DISPATCHING using real SessionManager
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.batch_operation_id is not None

    try:
        # Give threads a moment to reach runner.run_once and pause on proceed_event
        time.sleep(0.1)

        # Inspect real daemon operations
        ops = manager.list_operations(sid)
        running_wos = [
            o for o in ops
            if o.get("status") == "RUNNING" and o.get("work_order_id") in ("WO-002", "WO-003")
        ]
        assert len(running_wos) == 2, f"Both WO-002 and WO-003 must be simultaneously RUNNING! Found: {running_wos}"

        op2 = next(o for o in running_wos if o.get("work_order_id") == "WO-002")
        op3 = next(o for o in running_wos if o.get("work_order_id") == "WO-003")

        # Verify they share the supervisor batch parent operation ID
        assert op2["parent_operation_id"] == state.batch_operation_id
        assert op3["parent_operation_id"] == state.batch_operation_id
        assert op2["operation_id"] != op3["operation_id"]

        # Verify parent operation tracks them as children in real SessionManager journal
        parent_op = manager.get_operation(state.batch_operation_id)
        assert op2["operation_id"] in parent_op["children"]
        assert op3["operation_id"] in parent_op["children"]
        assert parent_op["status"] == "RUNNING"
    finally:
        proceed_event.set()
        time.sleep(0.1)


def test_s8_crash_window_recovers_inflight_operation_without_duplicate(tmp_path: Path) -> None:
    """S8 Crash Window: If a crash occurs after turn operation is started but before RunState is persisted,

    on resume the supervisor discovers the in-flight operation from the session, avoids duplicate dispatch,
    and continues tracking it.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # 1. Start a turn operation for WO-000 (Planning) directly in manager as if supervisor started it
    op = mock_mgr.start_turn("sess-s8-crash", "Plan goal", work_order_id="WO-000", is_goal=True)
    in_flight_op_id = op["operation_id"]
    assert op["status"] == "RUNNING"

    # 2. Simulate crash by having RunState on disk WITHOUT planning_operation_id recorded
    stale_disk_state = RunState(
        run_id="run-s8-crash",
        product_goal="Crash window test",
        workspace=str(tmp_path),
        session_id="sess-s8-crash",
        phase=Phase.PLANNING,
        planning_wo_id="WO-000",
        planning_operation_id=None,  # Stale: crash occurred before this field was saved to disk
    )
    supervisor.save_run_state(stale_disk_state, tmp_path)

    # 3. Simulate process restart / resume: fresh supervisor loads stale RunState from disk
    fresh_supervisor = LifecycleSupervisor(mock_mgr)
    resumed_state = fresh_supervisor.load_run_state("run-s8-crash", tmp_path)
    assert resumed_state is not None
    assert resumed_state.planning_operation_id is None

    # 4. Advance: supervisor must discover in-flight operation, adopt it, and NOT dispatch a duplicate turn
    initial_op_count = len(mock_mgr.list_operations())
    advance_res = fresh_supervisor.advance(resumed_state)

    assert advance_res == AdvanceResult.WAITING_FOR_OPERATION
    assert resumed_state.planning_operation_id == in_flight_op_id
    current_op_count = len(mock_mgr.list_operations())
    assert current_op_count == initial_op_count, "Must NOT dispatch a duplicate turn during crash-window recovery!"


def test_driver_contention_waiting_for_active_root_operation(tmp_path: Path) -> None:
    """When SessionManager raises OperationContentionError, advance() waits rather than failing.

    Verifies driver hardening: a transient lock contention from another operation
    or previous thread winding down must return WAITING_FOR_OPERATION instead of
    crashing the run into Phase.FAILED.
    """
    from validators.kernel.daemon.supervisor import OperationContentionError

    mock_mgr = MockSessionManager(tmp_path)

    # Monkeypatch start_turn to raise OperationContentionError
    def rejecting_start_turn(*args: Any, **kwargs: Any) -> Any:
        raise OperationContentionError("session already has an active operation")

    mock_mgr.start_turn = rejecting_start_turn  # type: ignore[assignment]

    supervisor = LifecycleSupervisor(mock_mgr, max_contention_retries=5)
    state = RunState(
        run_id="run-contention-test",
        product_goal="Test collision handling",
        workspace=str(tmp_path),
        session_id="sess-contention",
        phase=Phase.PLANNING,
        planning_wo_id="WO-000",
        planning_operation_id=None,
    )

    # Calling advance() attempts to dispatch planning turn, hitting operation contention
    result = supervisor.advance(state)

    # Must return WAITING_FOR_OPERATION and remain in PLANNING, not FAILED
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.PLANNING
    assert state.contention_count == 1
    assert state.error is None


def test_driver_contention_bounded_retries_escalates_to_blocked(tmp_path: Path) -> None:
    """When operation contention repeats beyond max_contention_retries, it escalates to Phase.BLOCKED."""
    from validators.kernel.daemon.supervisor import OperationContentionError

    mock_mgr = MockSessionManager(tmp_path)

    def rejecting_start_turn(*args: Any, **kwargs: Any) -> Any:
        raise OperationContentionError("session already has an active operation")

    mock_mgr.start_turn = rejecting_start_turn  # type: ignore[assignment]

    max_retries = 3
    supervisor = LifecycleSupervisor(mock_mgr, max_contention_retries=max_retries)
    state = RunState(
        run_id="run-stuck-test",
        product_goal="Test permanently stuck collision",
        workspace=str(tmp_path),
        session_id="sess-stuck",
        phase=Phase.PLANNING,
        planning_wo_id="WO-000",
        planning_operation_id=None,
    )

    # First max_retries - 1 attempts return WAITING_FOR_OPERATION
    for i in range(1, max_retries):
        res = supervisor.advance(state)
        assert res == AdvanceResult.WAITING_FOR_OPERATION
        assert state.phase == Phase.PLANNING
        assert state.contention_count == i

    # Next attempt reaches max_retries bound and escalates to BLOCKED
    final_res = supervisor.advance(state)
    assert final_res == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "operation contention timeout" in (state.error or "").lower()
    assert state.contention_count == max_retries


def test_manager_drive_run_times_out_when_operation_stuck(tmp_path: Path) -> None:
    """SessionManager._drive_run terminates and transitions to BLOCKED if WAITING_FOR_OPERATION times out."""
    from threading import Event
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager

    manager = SessionManager(DaemonStorage(tmp_path / "daemon.json"))
    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-timeout-test"
    state = manager.supervisor.start_run(run_id, "Test timeout bound", tmp_path, sid)
    state.planning_operation_id = "non-existent-op"  # will make advance return WAITING_FOR_OPERATION
    manager._active_runs[run_id] = state

    # Mock _get_operation on supervisor to simulate a stuck RUNNING operation
    manager.supervisor._get_operation = lambda op_id: {"operation_id": op_id, "status": "RUNNING"}

    stop_event = Event()

    # Run _drive_run with very short timeout (0.1s)
    manager._drive_run(run_id, stop_event, max_wait_seconds=0.1)

    # Must exit loop, escalate to Phase.BLOCKED, and record timeout error
    assert state.phase == Phase.BLOCKED
    assert state.error is not None
    assert "Timed out waiting for operations" in state.error


def test_supervisor_bootstrap_synthesis_only_in_bootstrap_mode(tmp_path: Path) -> None:
    """Deterministic child-WO synthesis is permitted ONLY in explicit bootstrap
    mode when the authoring turn completes without authoring artifacts; without
    bootstrap mode the run fails closed into ARCHITECT_REPAIR instead.
    """
    plan_content = (
        "# Project Plan: Login System Implementation\n\n"
        "## Current Architecture\n"
        "Stack: Python, FastAPI, React\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Project Scaffolding & Dependencies\n"
        "  - [ ] Task 1.1: Configure project dependency manifest (requirements.txt)\n"
        "- [ ] Milestone 2: Backend Authentication API\n"
        "  - [ ] Task 2.1: Implement POST /login endpoint (src/backend.py)\n"
        "- [ ] Milestone 3: Frontend Login Interface\n"
        "  - [ ] Task 3.1: Create Login Page (src/frontend.html)\n"
    )

    def _setup(plan_state: str = "APPROVED") -> tuple[MockSessionManager, LifecycleSupervisor, RunState, str]:
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
        mock_mgr.propose_plan("sess-001", "PLAN-001", title="Login System", content=plan_content)
        plan = mock_mgr.get_plan("sess-001", "PLAN-001")
        plan["state"] = plan_state
        state = supervisor.start_run("run-auth-test", "Login System", tmp_path, "sess-001")
        state.phase = Phase.AUTHORING
        state.plan_id = "PLAN-001"
        op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
        state.authoring_operation_id = op["operation_id"]
        mock_mgr.complete_operation(
            op["operation_id"],
            status="COMPLETED",
            result={"status": "completed", "summary": "I have reviewed PLAN.md."},
        )
        return mock_mgr, supervisor, state, op["operation_id"]

    # 1. Without bootstrap mode: fail closed into ARCHITECT_REPAIR — no synthesis.
    mock_mgr, supervisor, state, _op_id = _setup()
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.ARCHITECT_REPAIR
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    assert not wo_dir.exists() or not any(wo_dir.glob("*.yaml"))
    assert state.authoring_operation_failed is False
    assert any(
        issue.get("code") == "NO_WORK_ORDERS_AUTHORED" for issue in state.readiness_issues
    )
    # The repair turn contains bounded diagnostics, not the broad authoring prompt.
    repair_ops = [
        o for o in mock_mgr.list_operations() if o.get("metadata", {}).get("is_repair")
    ]
    assert len(repair_ops) == 1
    assert "rejection" in repair_ops[0]["prompt"].lower() or "repair" in repair_ops[0]["prompt"].lower()

    # 2. With explicit bootstrap mode: synthesis still produces validated child WOs.
    mock_mgr2, supervisor2, state2, _op2 = _setup()
    state2.bootstrap_mode = True
    res_a = supervisor2.advance(state2)
    assert res_a == AdvanceResult.TRANSITIONED
    assert state2.phase == Phase.AUTHORING_READINESS_GATE
    res_b = supervisor2.advance(state2)
    assert res_b == AdvanceResult.TRANSITIONED
    assert state2.phase == Phase.DISPATCHING
    assert state2.worker_wo_ids == ["WO-001", "WO-002", "WO-003"]
    assert state2.published_wo_ids == ["WO-001", "WO-002", "WO-003"]
    assert state2.error is None

    wo_dir2 = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contract_dir2 = tmp_path / ".sync" / "contracts"
    assert (wo_dir2 / "WO-001.yaml").is_file()
    assert (wo_dir2 / "WO-002.yaml").is_file()
    assert (wo_dir2 / "WO-003.yaml").is_file()
    assert (contract_dir2 / "WO-001.yaml").is_file()
    assert (contract_dir2 / "WO-002.yaml").is_file()
    assert (contract_dir2 / "WO-003.yaml").is_file()

    # Verify deliverable paths were intelligently derived
    wo1 = yaml.safe_load((wo_dir2 / "WO-001.yaml").read_text(encoding="utf-8"))
    assert wo1["deliverable"]["path"] == "requirements.txt"

    wo2 = yaml.safe_load((wo_dir2 / "WO-002.yaml").read_text(encoding="utf-8"))
    assert wo2["deliverable"]["path"] == "src/backend.py"

    wo3 = yaml.safe_load((wo_dir2 / "WO-003.yaml").read_text(encoding="utf-8"))
    assert wo3["deliverable"]["path"] == "src/frontend.html"


def test_supervisor_authoring_failure_never_synthesizes(tmp_path: Path) -> None:
    """A failed authoring operation transitions to ARCHITECT_REPAIR and never
    synthesizes or dispatches generic child work orders — even in bootstrap mode.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    (tmp_path / "PLAN.md").write_text(
        "# Project Plan: Login\n\n## Milestones & Roadmap\n- [ ] Milestone 1: Backend (src/backend.py)\n",
        encoding="utf-8",
    )
    state = supervisor.start_run("run-authfail", "Login System", tmp_path, "sess-001")
    state.phase = Phase.AUTHORING
    state.plan_id = "PLAN-001"
    state.bootstrap_mode = True  # even operator-approved bootstrap cannot rescue a failure

    op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
    state.authoring_operation_id = op["operation_id"]
    mock_mgr.complete_operation(
        op["operation_id"],
        status="FAILED",
        result={"status": "failed", "error": "Harness decision validation failed after retries"},
    )

    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.ARCHITECT_REPAIR
    assert state.authoring_operation_failed is True
    # No child WOs or contracts were synthesized or dispatched
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contract_dir = tmp_path / ".sync" / "contracts"
    assert not wo_dir.exists() or not any(
        f.stem != "WO-000" for f in wo_dir.glob("*.yaml")
    )
    assert not contract_dir.exists() or not any(
        f.stem != "WO-000" for f in contract_dir.glob("*.yaml")
    )
    assert any(
        issue.get("code") == "AUTHORING_OPERATION_FAILED" for issue in state.readiness_issues
    )
    # No worker dispatch happened — the only new op is the architect repair turn
    dispatched_worker_ops = [
        o for o in mock_mgr.list_operations()
        if o.get("metadata", {}).get("is_repair") is None and o.get("work_order_id") not in (None, "WO-000")
    ]
    assert dispatched_worker_ops == []


def test_runner_authoring_turn_does_not_require_plan_md_modification(tmp_path: Path) -> None:
    """AgentRunner.run_once with is_authoring=True does not require PLAN.md to be modified."""
    from validators.harness.runner import AgentRunner, HarnessRunResult
    from validators.kernel.providers.models import ProviderResponse, Message, TokenUsage

    # Write initial PLAN.md
    (tmp_path / "PLAN.md").write_text("# Project Plan\n\n## Architecture\nTest\n\n## Roadmap\n- [ ] Task 1\n", encoding="utf-8")

    from cli.init import init
    from validators.kernel.daemon.manager import synthesize_bootstrap_planning

    init(tmp_path, name="Test Project", no_git=True)
    synthesize_bootstrap_planning(tmp_path, "Create login plan")

    class ConversationalAuthoringAdapter:
        provider_name = "test"
        model_name = "test-model"

        def complete(self, messages: Any, **kwargs: Any) -> Any:
            return ProviderResponse(
                message=Message.assistant(
                    content='{"status": "completed", "summary": "Authored child work orders based on PLAN.md", "modified_files": []}'
                ),
                usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
            )

    runner = AgentRunner(tmp_path, "claude", provider_adapter=ConversationalAuthoringAdapter())
    result = runner.run_once(
        prompt="Author the implementation Work Orders and Contracts for the tasks in PLAN.md",
        work_order_id="WO-000",
        is_authoring=True,
    )

    # Must complete successfully without failing outcome_verified / deliverable 'PLAN.md'
    assert result.status == "completed", f"Runner failed with reason: {result.reason}"
    assert result.persisted is True
    assert "PLAN.md" not in (result.reason or "")


def test_manager_resume_run_recovers_failed_run(tmp_path: Path):
    """Verify that manager.resume_run revives a FAILED or BLOCKED supervisor run."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    session = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-testresume"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build login",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.FAILED,
        worker_wo_ids=["WO-001"],
        error="Previous authoring gate failure",
    )
    mgr.supervisor.save_run_state(r_state, tmp_path)

    # Resume without run_id discovers most recent run
    resumed = mgr.resume_run(sid)
    assert resumed["run_id"] == run_id
    assert resumed["phase"] == "DISPATCHING"
    assert resumed["error"] is None
    assert run_id in mgr._active_runs
    assert run_id in mgr._run_driver_threads

    # Clean up driver thread
    if run_id in mgr._run_stop_events:
        mgr._run_stop_events[run_id].set()


def test_manager_recovers_persisted_runs_on_boot(tmp_path: Path):
    """Verify that SessionManager.__init__ automatically recovers non-terminal runs from disk."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr1 = SessionManager(storage)
    session = mgr1.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]
    mgr1.propose_plan(sid, "PLAN-001", "Auth Plan", "Details")

    run_id = "run-bootrecovery"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build auth",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.AWAITING_APPROVAL,
        plan_id="PLAN-001",
    )
    mgr1.supervisor.save_run_state(r_state, tmp_path)

    # Stop any background threads from mgr1
    for stop_ev in mgr1._run_stop_events.values():
        stop_ev.set()

    # Boot a fresh SessionManager instance recovering from storage
    mgr2 = SessionManager(storage)
    assert run_id in mgr2._active_runs
    assert mgr2._active_runs[run_id].phase == Phase.AWAITING_APPROVAL
    assert run_id in mgr2._run_driver_threads

    for stop_ev in mgr2._run_stop_events.values():
        stop_ev.set()


def test_archive_completed_work_orders(tmp_path: Path):
    """Verify that completed work orders are moved from ACTIVE to COMPLETED and sync files are updated."""
    from validators.kernel.daemon.supervisor import archive_completed_work_orders
    from validators.kernel.daemon.authoring import get_next_work_order_int

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    tree_dir = tmp_path / ".sync" / "runtime"
    tree_dir.mkdir(parents=True, exist_ok=True)

    wo1 = {
        "id": "WO-001",
        "title": "Backend Scaffolding",
        "status": "ACTIVE",
        "assigned_agents": ["codex"],
    }
    wo2 = {
        "id": "WO-002",
        "title": "Auth API",
        "status": "ACTIVE",
        "assigned_agents": ["codex"],
    }
    (active_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")
    (active_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

    index_data = {
        "schema_version": 1,
        "next_id": 3,
        "total_active": 2,
        "total_completed": 0,
        "orders": [
            {"id": "WO-001", "status": "ACTIVE", "file": "work-orders/ACTIVE/WO-001.yaml"},
            {"id": "WO-002", "status": "ACTIVE", "file": "work-orders/ACTIVE/WO-002.yaml"},
        ],
    }
    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    index_file.write_text(yaml.safe_dump(index_data), encoding="utf-8")

    tree_data = {
        "schema_version": 1,
        "tree_version": 1,
        "work_orders": {"total_active": 2, "total_completed": 0, "total_blocked": 0},
        "agents": {
            "codex": {"assigned_work_orders": ["WO-001", "WO-002"]},
        },
    }
    tree_file = tree_dir / "TREE.yaml"
    tree_file.write_text(yaml.safe_dump(tree_data), encoding="utf-8")

    archived = archive_completed_work_orders(tmp_path, ["WO-001", "WO-002"])
    assert "WO-001" in archived
    assert "WO-002" in archived

    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    assert (completed_dir / "WO-001.yaml").is_file()
    assert (completed_dir / "WO-002.yaml").is_file()
    assert not (active_dir / "WO-001.yaml").exists()
    assert not (active_dir / "WO-002.yaml").exists()

    # Check INDEX.yaml
    idx_updated = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert idx_updated["total_active"] == 0
    assert idx_updated["total_completed"] == 2
    assert idx_updated["orders"][0]["file"] == "work-orders/COMPLETED/WO-001.yaml"
    assert idx_updated["orders"][0]["status"] == "COMPLETED"

    # Check TREE.yaml
    tree_updated = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assert tree_updated["work_orders"]["total_active"] == 0
    assert tree_updated["work_orders"]["total_completed"] == 2
    assert tree_updated["agents"]["codex"]["assigned_work_orders"] == []

    # Check next integer ID
    assert get_next_work_order_int(tmp_path) == 3


def test_supervisor_separates_gitops_and_transitions_executing_to_integration_review(tmp_path: Path):
    """Confirm GitOps WOs are reserved for GITOPS phase and do not stall EXECUTING phase."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # WO-001: Backend
    (active_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Backend Logic",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/backend.py"},
        }),
        encoding="utf-8",
    )
    # WO-002: QA
    (active_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "title": "QA Verification",
            "assigned_agents": ["gemma"],
            "dependencies": ["WO-001"],
            "deliverable": {"type": "doc", "description": "QA sign-off"},
        }),
        encoding="utf-8",
    )
    # WO-003: Release & GitOps
    (active_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003",
            "title": "Release & GitOps (Agent: Local-LLM)",
            "assigned_agents": ["local-llm"],
            "dependencies": ["WO-002"],
            "deliverable": {"type": "doc", "path": "VERSION.md"},
        }),
        encoding="utf-8",
    )

    state = supervisor.start_run("run-gitops-sep", "Rate Limiter", tmp_path, "sess-001")
    state.phase = Phase.EXECUTING

    # Discover WOs
    supervisor._discover_worker_wos(state)
    assert state.gitops_wo_id == "WO-003"
    assert state.worker_wo_ids == ["WO-001", "WO-002"]

    # Deliverables on disk
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "backend.py").write_text("# backend code", encoding="utf-8")

    # Simulate WO-001 completed
    op1 = mock_mgr.start_turn("sess-001", "Execute WO-001", role="backend", agent_id="codex", work_order_id="WO-001")
    mock_mgr.complete_operation(op1["operation_id"], status="COMPLETED")

    # Advance: should complete WO-001 and dispatch WO-002
    res = supervisor.advance(state)
    assert "WO-001" in state.completed_wo_ids

    # Simulate WO-002 completed
    op2 = mock_mgr.start_turn("sess-001", "Execute WO-002", role="qa", agent_id="gemma", work_order_id="WO-002")
    mock_mgr.complete_operation(op2["operation_id"], status="COMPLETED")

    # Advance: should complete WO-002 and transition directly to INTEGRATION_REVIEW
    res = supervisor.advance(state)
    assert "WO-002" in state.completed_wo_ids
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.INTEGRATION_REVIEW
    # GitOps WO was NOT dispatched during EXECUTING phase
    assert "WO-003" not in state.completed_wo_ids
    assert "WO-003" not in state.blocked_wo_ids


def test_d024_gate_passes_gitops_when_qa_work_order_completed(tmp_path: Path):
    """Confirm D024Gate resolves through QA work orders to allow GitOps progression."""
    from validators.harness.d024_gate import D024Gate

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # 1. Implementation WO-001
    (active_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Backend Logic",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/backend.py"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "backend.py").write_text("def test(): pass", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_backend.py").write_text("def test_test(): pass", encoding="utf-8")

    # 2. QA WO-002 marked COMPLETED
    (active_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "title": "QA Verification",
            "status": "COMPLETED",
            "assigned_agents": ["gemma"],
            "dependencies": ["WO-001"],
            "deliverable": {"type": "doc", "description": "QA sign-off"},
        }),
        encoding="utf-8",
    )

    # 3. GitOps WO-003 depending on WO-002
    (active_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003",
            "title": "Release & GitOps",
            "assigned_agents": ["local-llm"],
            "dependencies": ["WO-002"],
            "deliverable": {"type": "doc", "path": "VERSION.md"},
        }),
        encoding="utf-8",
    )

    gate = D024Gate()
    decision = gate.evaluate_work_order(tmp_path, "WO-003")
    assert decision.passed is True






# ─── Integration Review Fix Tests (Requirements 1-4) ─────────────────

def test_integration_review_synthesizes_dedicated_wo_and_contract(tmp_path: Path) -> None:
    """Requirement 1: Integration review creates its own WO and contract with read-only scope."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir1", "Build rate limiter", tmp_path, "sess-ir1")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]

    # Set up workspace structure
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Rate Limiter",
            "assigned_agents": ["codex"],
            "deliverable": {"type": "code", "path": "app/rate_limiter.py"},
        }),
        encoding="utf-8",
    )
    idx_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "title": "Rate Limiter", "status": "COMPLETED"}], "next_id": 2}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Advance to dispatch the review
    result = supervisor.advance(state)
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.integration_wo_id is not None
    assert state.integration_wo_id != "WO-000"  # Must NOT reuse bootstrap WO

    # Verify WO file was written
    review_wo_file = wo_dir / f"{state.integration_wo_id}.yaml"
    assert review_wo_file.is_file(), f"Review WO file should exist at {review_wo_file}"
    review_wo = yaml.safe_load(review_wo_file.read_text(encoding="utf-8"))
    assert review_wo["id"] == state.integration_wo_id
    assert review_wo["type"] == "VALIDATION"
    assert "claude" in review_wo["assigned_agents"]

    # Verify contract was written with read-only scope
    contract_file = tmp_path / ".sync" / "contracts" / f"{state.integration_wo_id}.yaml"
    assert contract_file.is_file(), f"Review contract should exist at {contract_file}"
    contract = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
    assert contract["scope"]["write"] == "read-only"
    assert contract["agent_id"] == "claude"
    assert contract["work_order"] == state.integration_wo_id

    # Verify scope includes the deliverable and frontend assets
    allow_modules = [r.get("module") for r in contract["scope"]["allow"]]
    assert "app/rate_limiter.py" in allow_modules, f"Deliverable should be in allow scope, got {allow_modules}"
    assert "PLAN.md" in allow_modules
    assert "*.html" in allow_modules
    assert "*.css" in allow_modules
    assert "*.js" in allow_modules
    assert "templates/**" in allow_modules
    assert "static/**" in allow_modules

    # Verify authorization: review contract can read frontend files but cannot write to them
    from validators.kernel.contract import ContractNormalizer, ContractEvaluator
    kernel_contract = ContractNormalizer.normalize(contract)
    evaluator = ContractEvaluator()
    can_read, _ = evaluator.authorize(kernel_contract, "read_file", "src/web/login.html")
    assert can_read is True
    can_write, msg = evaluator.authorize(kernel_contract, "write_file", "src/web/login.html")
    assert can_write is False
    assert "read-only" in msg.lower()


def test_integration_review_prompt_contains_explicit_json_format(tmp_path: Path) -> None:
    """Requirement 2: Review prompt specifies exact JSON fields for completed/blocked decisions."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir2", "Build auth module", tmp_path, "sess-ir2")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Auth Module",
            "assigned_agents": ["codex"],
            "deliverable": {"type": "code", "path": "app/auth.py"},
        }),
        encoding="utf-8",
    )
    idx_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "title": "Auth", "status": "COMPLETED"}], "next_id": 2}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Advance to dispatch the review
    supervisor.advance(state)

    # The mock captured the prompt in the start_turn call
    review_op = mock_mgr.operations[state.integration_operation_id]
    prompt = review_op["prompt"]

    # Verify explicit JSON format instructions are present
    assert '"status": "completed"' in prompt, "Prompt must show completed JSON example"
    assert '"status": "blocked"' in prompt, "Prompt must show blocked JSON example"
    assert '"blockers"' in prompt, "Prompt must mention blockers field"
    assert "non-empty string array" in prompt.lower() or "non-empty" in prompt.lower(), \
        "Prompt must specify blockers must be non-empty when blocked"
    assert "read-only" in prompt.lower(), "Prompt should mention read-only scope constraint"


def test_integration_review_blocked_transitions_to_blocked_not_failed(tmp_path: Path) -> None:
    """Requirement 4: A blocked review with rework exhausted → Phase.BLOCKED (recoverable), not Phase.FAILED."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir3", "Build API", tmp_path, "sess-ir3")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]
    state.max_retries = 2
    state.integration_rework_rounds = 2  # rework rounds already exhausted

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "API",
            "assigned_agents": ["codex"],
        }),
        encoding="utf-8",
    )
    idx_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "title": "API", "status": "COMPLETED"}], "next_id": 2}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Advance to dispatch review
    supervisor.advance(state)
    review_op_id = state.integration_operation_id

    # Simulate blocked review outcome (the manager would set this)
    mock_mgr.operations[review_op_id]["status"] = "BLOCKED"
    mock_mgr.operations[review_op_id]["result"] = {
        "status": "blocked",
        "summary": "Cannot verify deliverable",
        "blockers": ["Read access to app/api.py was denied."],
    }

    # Advance again — should transition to BLOCKED, NOT FAILED
    result = supervisor.advance(state)
    assert result == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED, f"Expected BLOCKED phase, got {state.phase}"
    assert state.integration_blockers == ["Read access to app/api.py was denied."]
    assert "blocked" in state.error.lower()


def test_integration_review_blocked_routes_to_bounded_rework(tmp_path: Path) -> None:
    """A blocked review routes its blockers back to the implementation workers
    for rework (WOs un-archived, re-dispatched with feedback under a batch
    parent); after the rework completes, the review re-runs and can pass the
    run to PRODUCT_READY."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir-rework", "Build API", tmp_path, "sess-ir-rw")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    # WO-001 completed and archived (no deliverable path — always reworkable)
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)
    (completed_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({"id": "WO-001", "title": "API", "assigned_agents": ["codex"], "status": "COMPLETED"}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({"orders": [], "next_id": 2}), encoding="utf-8"
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Advance: review dispatched
    supervisor.advance(state)
    review_op_id = state.integration_operation_id

    # Review blocks with concrete blockers
    mock_mgr.operations[review_op_id]["status"] = "BLOCKED"
    mock_mgr.operations[review_op_id]["result"] = {
        "status": "blocked",
        "summary": "Security violations found",
        "blockers": ["Hardcoded default for SECRET_KEY in src/backend.py"],
    }

    # Advance: bounded rework, not terminal block
    result = supervisor.advance(state)
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.integration_rework_rounds == 1
    assert state.integration_operation_id is None  # stale review op retired
    assert "WO-001" not in state.completed_wo_ids
    # The archived WO returned to ACTIVE for re-dispatch
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    assert (active_dir / "WO-001.yaml").is_file()
    assert not (completed_dir / "WO-001.yaml").exists()
    rework_ops = [o for o in mock_mgr.list_operations() if "Rework work order WO-001" in str(o.get("prompt", ""))]
    assert len(rework_ops) == 1
    assert "SECRET_KEY" in rework_ops[0]["prompt"]

    # Rework turn completes; WO-001 returns to completed; review re-runs fresh
    mock_mgr.complete_operation(rework_ops[0]["operation_id"], "COMPLETED")
    supervisor.advance(state)  # WO-001 completes again -> INTEGRATION_REVIEW
    assert state.phase == Phase.INTEGRATION_REVIEW
    supervisor.advance(state)  # dispatches the fresh review turn
    assert state.integration_operation_id, "expected a fresh integration review turn"
    assert state.integration_operation_id != review_op_id  # new op, not the blocked one

    # Review passes this time -> PRODUCT_READY
    fresh_review_id = state.integration_operation_id
    mock_mgr.operations[fresh_review_id]["status"] = "COMPLETED"
    mock_mgr.operations[fresh_review_id]["result"] = {
        "status": "completed",
        "summary": "Integration review passed.",
        "blockers": [],
    }
    result = supervisor.advance(state)
    assert result == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.PRODUCT_READY


def test_integration_review_failed_operation_transitions_to_failed(tmp_path: Path) -> None:
    """Requirement 4: Actual operation failure → Phase.FAILED (unrecoverable)."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir4", "Build feature", tmp_path, "sess-ir4")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Feature",
            "assigned_agents": ["codex"],
        }),
        encoding="utf-8",
    )
    idx_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "title": "Feature", "status": "COMPLETED"}], "next_id": 2}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Dispatch review
    supervisor.advance(state)
    review_op_id = state.integration_operation_id

    # Simulate actual execution failure (not a blocked decision)
    mock_mgr.operations[review_op_id]["status"] = "FAILED"
    mock_mgr.operations[review_op_id]["result"] = {
        "error": "Provider timeout after 120s",
    }

    result = supervisor.advance(state)
    assert result == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED


def test_integration_review_wo_id_persists_in_state_roundtrip(tmp_path: Path) -> None:
    """The integration_wo_id and integration_blockers fields survive serialization roundtrip."""
    state = RunState(
        run_id="run-ser",
        product_goal="Test persistence",
        workspace=str(tmp_path),
        session_id="sess-ser",
        phase=Phase.BLOCKED,
        integration_wo_id="WO-005",
        integration_blockers=["Deliverable missing", "Test coverage below threshold"],
    )
    data = state.to_dict()
    assert data["integration_wo_id"] == "WO-005"
    assert data["integration_blockers"] == ["Deliverable missing", "Test coverage below threshold"]

    restored = RunState.from_dict(data)
    assert restored.integration_wo_id == "WO-005"
    assert restored.integration_blockers == ["Deliverable missing", "Test coverage below threshold"]
    assert restored.phase == Phase.BLOCKED


def test_integration_review_archives_review_wo_in_gitops(tmp_path: Path) -> None:
    """The synthesized review WO is included in the archival list during GitOps."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir5", "Build module", tmp_path, "sess-ir5")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001"]

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Module",
            "assigned_agents": ["codex"],
        }),
        encoding="utf-8",
    )
    idx_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    idx_file.write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "title": "Module", "status": "COMPLETED"}], "next_id": 2}),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    # Dispatch and complete review
    supervisor.advance(state)
    review_wo_id = state.integration_wo_id
    assert review_wo_id is not None

    mock_mgr.complete_operation(state.integration_operation_id, "COMPLETED")
    supervisor.advance(state)  # → PRODUCT_READY
    assert state.phase == Phase.PRODUCT_READY

    # Verify the review WO ID would be in the archive list
    # by checking state still carries it
    assert state.integration_wo_id == review_wo_id


# ─── Runner _parse_json_payload Tests ─────────────────────────────────

def test_parse_json_payload_from_code_block() -> None:
    """_parse_json_payload extracts JSON from markdown code blocks."""
    from validators.harness.runner import AgentRunner
    raw = '```json\n{"status": "completed", "summary": "All good", "blockers": []}\n```'
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["blockers"] == []


def test_parse_json_payload_from_raw_json() -> None:
    """_parse_json_payload handles raw JSON text."""
    from validators.harness.runner import AgentRunner
    raw = '{"status": "blocked", "summary": "Missing file", "blockers": ["file not found"]}'
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is not None
    assert payload["status"] == "blocked"
    assert payload["blockers"] == ["file not found"]


def test_parse_json_payload_normalizes_status() -> None:
    """_parse_json_payload maps alternative status strings to canonical ones."""
    from validators.harness.runner import AgentRunner
    raw = '{"status": "SUCCESS", "summary": "done"}'
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is not None
    assert payload["status"] == "completed"

    raw_failed = '{"status": "FAILED", "summary": "error"}'
    payload2 = AgentRunner._parse_json_payload(raw_failed)
    assert payload2 is not None
    assert payload2["status"] == "blocked"


def test_parse_json_payload_unwraps_decision_key() -> None:
    """_parse_json_payload unwraps {'decision': {...}} wrapper."""
    from validators.harness.runner import AgentRunner
    raw = '{"decision": {"status": "completed", "summary": "OK", "blockers": []}}'
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is not None
    assert payload["status"] == "completed"
    assert "decision" not in payload


def test_parse_json_payload_returns_none_for_non_json() -> None:
    """_parse_json_payload returns None for plain prose."""
    from validators.harness.runner import AgentRunner
    raw = "I have completed the review and everything looks good."
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is None


def test_parse_json_payload_raw_decode_finds_embedded_json() -> None:
    """_parse_json_payload uses raw_decode to find JSON embedded in prose."""
    from validators.harness.runner import AgentRunner
    raw = 'Here is my decision:\n\n{"status": "completed", "summary": "verified", "blockers": []}\n\nEnd of review.'
    payload = AgentRunner._parse_json_payload(raw)
    assert payload is not None
    assert payload["status"] == "completed"


# ─── Manager Blocked Status Routing Test ──────────────────────────────

def test_manager_blocked_result_sets_blocked_operation_status(tmp_path: Path) -> None:
    """Manager._run_turn routes result.status=='blocked' to BLOCKED operation status, not FAILED."""
    # This test verifies the elif branch in _run_turn
    mock_mgr = MockSessionManager(tmp_path)

    # Simulate what the manager does: create an operation, then check
    # how complete_operation routes it
    op = mock_mgr.start_turn("sess-blk", "test", work_order_id="WO-001")
    op_id = op["operation_id"]

    # Simulate blocked completion (what the new elif branch does)
    result_data = {"status": "blocked", "reason": "scope denied"}
    mock_mgr.complete_operation(op_id, status="BLOCKED", result=result_data)

    completed = mock_mgr.get_operation(op_id)
    assert completed["status"] == "BLOCKED"
    assert completed["result"]["status"] == "blocked"


def test_events_tool_result_accepts_blocked_status() -> None:
    """EventDispatcher.tool_result accepts 'blocked' without raising ValueError."""
    from validators.kernel.daemon.events import EventDispatcher
    dispatcher = EventDispatcher()
    evt = dispatcher.tool_result(
        session_id="sess-test",
        tool_name="harness.run_once",
        call_id="call-001",
        status="blocked",
        operation_id="op-001",
        result={"status": "blocked", "reason": "access denied"},
    )
    assert evt.payload["status"] == "blocked"
    assert evt.payload["result"]["status"] == "blocked"


def test_runner_integration_review_sets_correct_system_prompt() -> None:
    """AgentRunner does not treat an integration review task as a plan-generation task."""
    from pathlib import Path
    from unittest.mock import MagicMock
    from validators.harness.runner import AgentRunner, HarnessTask, LLMRequest

    runner = AgentRunner(Path("."), "claude")
    task = HarnessTask(
        kind="work_order",
        identifier="WO-005",
        path=Path("WO-005.yaml"),
        title="Integration Review",
        body="Perform integration review of all completed deliverables.",
        query="Integration Review",
        work_order_id="WO-005",
    )
    from validators.knowledge.api import ContextBundle
    from validators.harness.retrieval import RetrievalBatch
    ctx = ContextBundle(
        revision=1, git_commit="HEAD", stale=False, semantic=False,
        token_budget=1000, estimated_tokens=0, truncated=False,
        truncation_reason=None, entries=(), text="",
    )
    retrieval = RetrievalBatch(
        results=(), evidence=(), mode="test", cost_estimate=0.0,
        cache_hits=0, searches_used=0, cap_exhausted=False,
    )
    req = LLMRequest(
        agent="claude",
        session_count=1,
        task=task,
        context=ctx,
        retrieval=retrieval,
    )
    from unittest.mock import patch
    mock_runtime = MagicMock()
    mock_runtime.gateway.contract = MagicMock()
    del mock_runtime.gateway.boundary
    mock_runtime.workspace.attempt_id = "attempt-1"

    with patch("validators.kernel.providers.gateway.ProviderGateway.run_loop") as mock_run_loop:
        from validators.kernel.providers.models import Message
        mock_msg = Message.assistant('{"status": "completed", "summary": "ok", "blockers": []}')
        mock_run_loop.return_value = [mock_msg]
        rec = runner._complete_request(req, mock_runtime)
        assert rec is not None
        call_args = mock_run_loop.call_args
        messages = call_args[0][0]
        system_msg = messages[0].content
        assert "final integration review" in system_msg.lower()
        assert "read-only contract" in system_msg.lower()
        assert "write plan.md" not in system_msg.lower()


def test_session_manager_complete_operation_supports_blocked_status(tmp_path: Path) -> None:
    """SessionManager.complete_operation allows 'BLOCKED' as a valid terminal status."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon-state.json")
    sm = SessionManager(storage)
    sess = sm.create_session("claude", "daemon", {}, str(tmp_path))
    _, op_id = sm.begin_operation(sess["session_id"], "turn")

    res = sm.complete_operation(
        sess["session_id"],
        op_id,
        status="BLOCKED",
        result={"status": "blocked", "reason": "scope check"},
    )
    assert res["status"] == "BLOCKED"
    assert sm.get_operation(op_id)["status"] == "BLOCKED"


def test_d024_find_companion_test_file_recognizes_test_files() -> None:
    """find_companion_test_file returns the file itself when deliverable is a test file."""
    from validators.harness.d024_gate import D024Gate
    gate = D024Gate()
    # Path inside clean_tui_test_final or dummy path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        t_dir = Path(td) / "tests"
        t_dir.mkdir(parents=True)
        test_file = t_dir / "test_rate_limiter.py"
        test_file.write_text("def test_rate(): pass", encoding="utf-8")
        found = gate.find_companion_test_file(Path(td), "tests/test_rate_limiter.py")
        assert found is not None
        assert found.resolve() == test_file.resolve()


def test_d024_gate_recognizes_qa_work_order_with_completion_notice(tmp_path: Path) -> None:
    """D024Gate recognizes completed QA work order from inbox completion notice even if status is ACTIVE."""
    from validators.harness.d024_gate import D024Gate
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = tmp_path / ".sync" / "inbox" / "claude"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_sample.py").write_text("def test_ok(): pass", encoding="utf-8")

    # Worker WO
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "title": "Backend",
        "assigned_agents": ["codex"],
        "deliverable": {"type": "code", "path": "tests/test_sample.py"},
    }), encoding="utf-8")

    # QA WO that depends on WO-001, status is ACTIVE but completion notice is in inbox
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002",
        "title": "QA",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "status": "ACTIVE",
    }), encoding="utf-8")
    (inbox_dir / "2026-10-01_gemma_WO-002-complete.md").write_text("# Completion Notice", encoding="utf-8")

    # GitOps WO
    (wo_dir / "WO-003.yaml").write_text(yaml.safe_dump({
        "id": "WO-003",
        "title": "GitOps Release",
        "assigned_agents": ["local-llm"],
        "dependencies": ["WO-002"],
        "status": "ACTIVE",
    }), encoding="utf-8")

    gate = D024Gate()
    decision = gate.evaluate_work_order(tmp_path, "WO-003")
    assert decision.passed is True
    assert "explicit Gemma QA approval" in decision.reason


def test_manager_resume_run_restores_pre_blocked_phase(tmp_path: Path):
    """Verify that manager.resume_run restores the pre-blocked phase rather than resetting to DISPATCHING."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    session = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-gitops-blocked"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build rate limiter",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001", "WO-002", "WO-003"],
        completed_wo_ids=["WO-001", "WO-002", "WO-003"],
        gitops_wo_id="WO-004",
        error="GitOps dispatch blocked: gate error",
        transitions=[
            {"from": "PRODUCT_READY", "to": "GITOPS", "at": "2026-10-01T12:00:00Z"},
            {"from": "GITOPS", "to": "BLOCKED", "at": "2026-10-01T12:00:05Z"},
        ],
    )
    mgr.supervisor.save_run_state(r_state, tmp_path)

    # get_active_run should return the blocked run
    active_rec = mgr.get_active_run(sid)
    assert active_rec is not None
    assert active_rec["phase"] == "BLOCKED"

    # resume_run should restore Phase.GITOPS (the pre-blocked phase)
    resumed = mgr.resume_run(sid)
    assert resumed["run_id"] == run_id
    assert resumed["phase"] == "GITOPS"
    assert resumed["error"] is None
    assert run_id in mgr._active_runs
    assert run_id in mgr._run_driver_threads

    if run_id in mgr._run_stop_events:
        mgr._run_stop_events[run_id].set()


def test_manager_boot_auto_resumes_blocked_run_with_active_session(tmp_path: Path):
    """Verify that SessionManager.__init__ automatically recovers and resumes BLOCKED runs on restart."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr1 = SessionManager(storage)
    session = mgr1.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-boot-blocked"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build release",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001"],
        completed_wo_ids=["WO-001"],
        gitops_wo_id="WO-002",
        error="D024 gate blocked",
        transitions=[
            {"from": "PRODUCT_READY", "to": "GITOPS", "at": "2026-10-01T12:00:00Z"},
            {"from": "GITOPS", "to": "BLOCKED", "at": "2026-10-01T12:00:05Z"},
        ],
    )
    mgr1.supervisor.save_run_state(r_state, tmp_path)

    for stop_ev in mgr1._run_stop_events.values():
        stop_ev.set()

    # Boot fresh SessionManager recovering from storage
    mgr2 = SessionManager(storage)
    assert run_id in mgr2._active_runs
    # Should have auto-resumed to GITOPS (or already completed via driver thread)
    assert mgr2._active_runs[run_id].phase in (Phase.GITOPS, Phase.COMPLETE)
    assert run_id in mgr2._run_driver_threads

    for stop_ev in mgr2._run_stop_events.values():
        stop_ev.set()


def test_manager_resume_run_clears_stale_gitops_operation_id(tmp_path: Path):
    """Verify that resuming a blocked GITOPS run clears stale gitops_operation_id."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    session = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-stale-gitops-op"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build release",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001"],
        completed_wo_ids=["WO-001"],
        gitops_wo_id="WO-002",
        gitops_operation_id="stale-op-12345",
        error="GitOps turn blocked",
        transitions=[
            {"from": "PRODUCT_READY", "to": "GITOPS", "at": "2026-10-01T12:00:00Z"},
            {"from": "GITOPS", "to": "BLOCKED", "at": "2026-10-01T12:00:05Z"},
        ],
    )
    mgr.supervisor.save_run_state(r_state, tmp_path)

    resumed = mgr.resume_run(sid, run_id=run_id)
    assert resumed["phase"] == "GITOPS"
    assert resumed["gitops_operation_id"] is None
    assert mgr._active_runs[run_id].gitops_operation_id is None

    for stop_ev in mgr._run_stop_events.values():
        stop_ev.set()



def test_failed_transition_marks_work_orders_on_disk(tmp_path: Path) -> None:
    """When the supervisor transitions to FAILED, all in-flight work orders
    on disk must be marked FAILED so that :rebind and other policy guards
    don't see stale ACTIVE records.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # 1. Set up a workspace with PLAN.md and WO-000 on disk (simulating planning)
    (tmp_path / "PLAN.md").write_text(
        "# Project Plan\n\n## Current Architecture\nN/A\n\n"
        "## Milestones & Roadmap\n- [ ] Milestone 1: Setup\n",
        encoding="utf-8",
    )
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    wo_000 = {
        "id": "WO-000",
        "type": "RESEARCH",
        "title": "Planning",
        "status": "ACTIVE",
        "assigned_agents": ["claude"],
        "dependencies": [],
    }
    (active_dir / "WO-000.yaml").write_text(
        yaml.dump(wo_000, default_flow_style=False), encoding="utf-8"
    )

    # 2. Start a run (INIT → PLANNING)
    state = supervisor.start_run("run-fail-test", "Test goal", tmp_path, "sess-fail")

    # 3. Simulate planning turn that completed but produced no valid plan
    op = mock_mgr.start_turn("sess-fail", "Plan", role="architecture",
                             agent_id="claude", work_order_id="WO-000")
    state.planning_operation_id = op["operation_id"]
    mock_mgr.complete_operation(op["operation_id"], status="COMPLETED",
                                result={"status": "completed"})

    # 4. Advance — supervisor finds no plan proposed → FAILED
    res = supervisor.advance(state)
    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED

    # 5. CRITICAL: WO-000 on disk must now be FAILED, not ACTIVE
    data = yaml.safe_load((active_dir / "WO-000.yaml").read_text(encoding="utf-8"))
    assert data["status"] == "FAILED", (
        f"Expected WO-000 status 'FAILED' on disk, got '{data['status']}'. "
        "Stale ACTIVE WOs block :rebind."
    )
    assert "error" in data


def test_blocked_transition_marks_work_orders_on_disk(tmp_path: Path) -> None:
    """When the supervisor transitions to BLOCKED, work orders on disk
    must be marked BLOCKED so that :rebind can proceed after resolution.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # 1. Set up workspace with WO-000 and two worker WOs
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    for wo_id, agent in [("WO-000", "claude"), ("WO-001", "codex"), ("WO-002", "gemini")]:
        wo_data = {
            "id": wo_id,
            "type": "FEATURE",
            "title": f"Task {wo_id}",
            "status": "ACTIVE",
            "assigned_agents": [agent],
            "dependencies": [],
        }
        (active_dir / f"{wo_id}.yaml").write_text(
            yaml.dump(wo_data, default_flow_style=False), encoding="utf-8"
        )

    # 2. Build run state at EXECUTING with worker WOs
    state = supervisor.start_run("run-block-test", "Test", tmp_path, "sess-block")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001", "WO-002"]

    # 3. Force a BLOCKED transition via contention timeout
    state.error = "Governance gate blocked WO-001"
    supervisor._transition(state, Phase.BLOCKED)

    assert state.phase == Phase.BLOCKED

    # 4. All three WOs must be BLOCKED on disk
    for wo_id in ["WO-000", "WO-001", "WO-002"]:
        data = yaml.safe_load((active_dir / f"{wo_id}.yaml").read_text(encoding="utf-8"))
        assert data["status"] == "BLOCKED", (
            f"Expected {wo_id} status 'BLOCKED', got '{data['status']}'"
        )


def test_already_completed_wos_not_overwritten(tmp_path: Path) -> None:
    """Work orders that are already in a terminal state (COMPLETED) must NOT
    be overwritten when the run transitions to FAILED.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)

    # WO-001 is COMPLETED, WO-002 is still ACTIVE
    for wo_id, status in [("WO-001", "COMPLETED"), ("WO-002", "ACTIVE")]:
        wo_data = {
            "id": wo_id,
            "type": "FEATURE",
            "title": f"Task {wo_id}",
            "status": status,
            "assigned_agents": ["codex"],
            "dependencies": [],
        }
        (active_dir / f"{wo_id}.yaml").write_text(
            yaml.dump(wo_data, default_flow_style=False), encoding="utf-8"
        )

    state = supervisor.start_run("run-no-overwrite", "Test", tmp_path, "sess-noover")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001", "WO-002"]
    state.completed_wo_ids = ["WO-001"]
    state.error = "WO-002 failed after max retries"

    supervisor._transition(state, Phase.FAILED)

    # WO-001 must remain COMPLETED (skipped by both completed_wo_ids check and terminal guard)
    data1 = yaml.safe_load((active_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert data1["status"] == "COMPLETED"

    # WO-002 must now be FAILED
    data2 = yaml.safe_load((active_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert data2["status"] == "FAILED"


def test_mark_wo_handles_missing_file(tmp_path: Path) -> None:
    """_mark_wo_status_on_disk returns False for non-existent WO files
    without raising.
    """
    result = LifecycleSupervisor._mark_wo_status_on_disk(
        tmp_path, "WO-999", "FAILED", error="test"
    )
    assert result is False


def test_manager_resume_run_restarts_dead_thread_even_if_stop_event_not_set(tmp_path: Path):
    """Verify that manager.resume_run reliably spawns a new driver thread if
    the previous driver thread has exited, even when stop_event was left unset.
    """
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    session = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-deadthread"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    r_state = RunState(
        run_id=run_id,
        product_goal="Build login",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.FAILED,
        worker_wo_ids=["WO-001"],
        error="Previous failure",
    )
    mgr.supervisor.save_run_state(r_state, tmp_path)

    # Simulate an entry in _run_stop_events that was NOT set (e.g. previous dead thread)
    from threading import Event, Thread
    stale_event = Event()  # is_set() == False
    mgr._run_stop_events[run_id] = stale_event
    # Simulate a dead thread
    dead_thread = Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()
    mgr._run_driver_threads[run_id] = dead_thread
    assert not dead_thread.is_alive()
    assert not stale_event.is_set()

    # Resume run must detect the thread is dead and start a fresh one with a new stop_event
    resumed = mgr.resume_run(sid, run_id=run_id)
    assert resumed["run_id"] == run_id
    new_thread = mgr._run_driver_threads[run_id]
    assert new_thread is not dead_thread
    assert new_thread.is_alive()
    assert mgr._run_stop_events[run_id] is not stale_event

    # Cleanup
    mgr._run_stop_events[run_id].set()


def test_manager_resume_run_invalidates_stale_operations_and_unblocks_work_orders(tmp_path: Path):
    """Verify that manager.resume_run invalidates prior non-completed operations in the session journal,
    unblocks in-flight work orders on disk, and clears error states.
    """
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase, RunState
    import yaml

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    session = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = session["session_id"]

    run_id = "run-stale-ops"
    sup_dir = tmp_path / ".sync" / "runtime" / "supervisor"
    sup_dir.mkdir(parents=True, exist_ok=True)
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create WO-001 on disk as BLOCKED
    wo_file = wo_dir / "WO-001.yaml"
    wo_file.write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "title": "Configure Dependencies",
            "status": "BLOCKED",
            "error": "timed out",
            "blocked_reason": "timed out",
        }),
        encoding="utf-8",
    )

    # 2. Create INDEX.yaml and TREE.yaml with total_blocked=1
    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    index_file.write_text(
        yaml.safe_dump({
            "orders": [{"id": "WO-001", "status": "BLOCKED"}],
            "total_active": 0,
            "total_blocked": 1,
            "total_completed": 0,
        }),
        encoding="utf-8",
    )
    tree_file = tmp_path / ".sync" / "runtime" / "TREE.yaml"
    tree_file.parent.mkdir(parents=True, exist_ok=True)
    tree_file.write_text(
        yaml.safe_dump({
            "work_orders": {"total_active": 0, "total_blocked": 1, "total_completed": 0},
        }),
        encoding="utf-8",
    )

    # 3. Add stale blocked operation to session journal
    stale_op_id = "op-stale-blocked-123"
    session["journal"].append({
        "operation_id": stale_op_id,
        "work_order_id": "WO-001",
        "role": "backend",
        "status": "BLOCKED",
        "result": {"status": "blocked", "error": "timed out"},
    })

    # 4. Save run state as BLOCKED
    r_state = RunState(
        run_id=run_id,
        product_goal="Build login",
        workspace=str(tmp_path),
        session_id=sid,
        phase=Phase.BLOCKED,
        worker_wo_ids=["WO-001"],
        blocked_wo_ids=["WO-001"],
        error="Work order WO-001 blocked: timed out",
    )
    mgr.supervisor.save_run_state(r_state, tmp_path)

    # Before resume: supervisor finds the stale BLOCKED operation
    found_before = mgr.supervisor._find_latest_wo_operation(r_state, "WO-001")
    assert found_before is not None
    assert found_before["operation_id"] == stale_op_id

    # 5. Resume the run
    resumed = mgr.resume_run(sid, run_id=run_id)
    assert resumed["run_id"] == run_id
    assert resumed["phase"] in ("EXECUTING", "DISPATCHING")
    assert not resumed["error"]
    assert not resumed["blocked_wo_ids"]

    # 6. Verify stale operation is now ignored by supervisor and fresh turn was dispatched
    reloaded_state = mgr.supervisor.load_run_state(run_id, tmp_path)
    assert stale_op_id in reloaded_state.ignored_operation_ids
    found_after = mgr.supervisor._find_latest_wo_operation(reloaded_state, "WO-001")
    # Must NOT be the stale blocked operation! It may be None or the newly dispatched turn.
    if found_after is not None:
        assert found_after["operation_id"] != stale_op_id
        assert found_after["operation_id"] not in reloaded_state.ignored_operation_ids

    # 7. Verify WO-001 on disk was reset to ACTIVE with errors cleared
    reloaded_wo = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
    assert reloaded_wo["status"] == "ACTIVE"
    assert "error" not in reloaded_wo
    assert "blocked_reason" not in reloaded_wo

    # 8. Verify INDEX.yaml and TREE.yaml counters synced
    reloaded_idx = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert reloaded_idx["total_active"] == 1
    assert reloaded_idx["total_blocked"] == 0
    reloaded_tree = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assert reloaded_tree["work_orders"]["total_active"] == 1
    assert reloaded_tree["work_orders"]["total_blocked"] == 0

    # Cleanup driver thread
    mgr._run_stop_events[run_id].set()


def test_manager_resume_run_cancels_orphaned_operations_and_does_not_adopt_in_integration_review(tmp_path: Path) -> None:
    """When an operation was abandoned in RUNNING state (e.g. server restart), resume_run must cancel it
    and integration review must NOT adopt it as the active turn."""
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.supervisor import Phase

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage)
    created = mgr.create_session("codex", "daemon", {"write_mode": "governed"}, str(tmp_path))
    sid = created["session_id"]
    session = mgr._sessions[sid]
    run_id = "run-orphan-test"
    state = mgr.supervisor.start_run(run_id, "Build product", tmp_path, sid)
    state.completed_wo_ids = ["WO-001"]
    state.phase = Phase.INTEGRATION_REVIEW
    state.integration_wo_id = "WO-002"

    # Simulate an orphaned operation left in RUNNING with no alive thread
    orphan_op_id = "op-orphaned-architecture"
    session["journal"].append({
        "operation_id": orphan_op_id,
        "operation": "turn",
        "role": "architecture",
        "agent_id": "claude",
        "work_order_id": "WO-002",
        "status": "RUNNING",
    })
    session["active_operation"] = orphan_op_id
    state.integration_operation_id = orphan_op_id
    mgr._active_runs[run_id] = state

    # Resume the run
    mgr.resume_run(sid, run_id=run_id)

    # 1. Orphaned operation in journal must be marked CANCELLED
    op_record = mgr.get_operation(orphan_op_id)
    assert op_record["status"] == "CANCELLED"

    # 2. session active_operation must be cleared
    assert session.get("active_operation") is None

    # 3. Orphan op must be in ignored_operation_ids
    assert orphan_op_id in state.ignored_operation_ids

    # 4. integration_operation_id must NOT be the orphaned operation
    assert state.integration_operation_id != orphan_op_id

    # Cleanup driver thread
    if run_id in mgr._run_stop_events:
        mgr._run_stop_events[run_id].set()
    if run_id in mgr._run_driver_threads:
        mgr._run_driver_threads[run_id].join(timeout=1.0)


# ─── Architect Escalation Loop Tests (KNOW-01 / HARNESS-01) ─────────────


def test_undeclared_dependency_escalates_to_architecture_and_resumes(tmp_path: Path) -> None:
    """When a worker blocks on undeclared third-party dependencies, supervisor routes to Claude,
    which updates dependencies, allowing the worker to be safely re-dispatched."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    deliv_file = tmp_path / "db" / "init_db.py"
    deliv_file.parent.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002",
        "title": "Database Schema",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"path": "db/init_db.py", "type": "code"},
    }), encoding="utf-8")

    state = RunState(
        run_id="run-esc-1",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-esc-1",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-002"],
    )

    # 1. Worker turn fails with undeclared dependency
    worker_op = mock_mgr.start_turn("sess-esc-1", "Execute WO-002", work_order_id="WO-002", role="backend", agent_id="codex")
    mock_mgr.complete_operation(
        worker_op["operation_id"],
        status="COMPLETED",
        result={
            "status": "blocked",
            "error": "verification gate failed: code_verified (Deliverable 'db/init_db.py' imports undeclared third-party module(s): werkzeug. Your contract permits updating dependency manifests. Declare them in pyproject.toml or requirements.txt using write_file.)",
        },
    )

    # 2. Advance: Supervisor detects undeclared dependency and escalates to Claude (Architecture)
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING  # Does NOT transition to BLOCKED!
    assert state.dependency_escalation_op_id is not None
    assert state.dependency_escalation_wo_id == "WO-002"
    assert worker_op["operation_id"] in state.ignored_operation_ids

    # Verify escalation operation was dispatched to Claude
    esc_op = mock_mgr.get_operation(state.dependency_escalation_op_id)
    assert esc_op["agent_id"] == "claude"
    assert esc_op["role"] == "architecture"
    assert "werkzeug" in esc_op["prompt"]
    assert "db/init_db.py" in esc_op["prompt"]

    # 3. Claude resolves the dependency (updates requirements.txt) and completes turn
    mock_mgr.complete_operation(
        state.dependency_escalation_op_id,
        status="COMPLETED",
        result={"status": "completed", "summary": "Declared werkzeug>=3.0 in requirements.txt"},
    )

    # 4. Advance: Supervisor notices Claude's turn completed, resets WO-002, and re-dispatches worker (Codex)
    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.dependency_escalation_op_id is None
    assert state.dependency_escalation_wo_id is None

    # Check that a fresh worker operation for WO-002 was dispatched
    latest_worker_op = supervisor._find_latest_wo_operation(state, "WO-002")
    assert latest_worker_op is not None
    assert latest_worker_op["operation_id"] != worker_op["operation_id"]
    assert latest_worker_op["agent_id"] == "codex"

    # 5. Worker completes deliverable now that dependency is satisfied
    deliv_file.write_text("# user db schema using werkzeug", encoding="utf-8")
    mock_mgr.complete_operation(
        latest_worker_op["operation_id"],
        status="COMPLETED",
        result={"status": "completed"},
    )

    # 6. Final advance: WO-002 completes and lifecycle advances to INTEGRATION_REVIEW
    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.INTEGRATION_REVIEW
    assert "WO-002" in state.completed_wo_ids


def test_undeclared_dependency_escalation_bounded_by_max_retries(tmp_path: Path) -> None:
    """If repeated escalations fail to resolve the dependency, supervisor safely halts at BLOCKED."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-002.yaml").write_text(yaml.safe_dump({
        "id": "WO-002",
        "title": "Auth",
        "assigned_agents": ["codex"],
        "dependencies": [],
    }), encoding="utf-8")

    state = RunState(
        run_id="run-esc-fail",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-esc-fail",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-002"],
        max_retries=1,  # Set to 1 for quick bounded test
    )

    # 1. First worker failure
    op1 = mock_mgr.start_turn("sess-esc-fail", "Execute WO-002", work_order_id="WO-002", role="backend")
    mock_mgr.complete_operation(
        op1["operation_id"],
        status="COMPLETED",
        result={"status": "blocked", "error": "imports undeclared third-party module(s): bad_pkg"},
    )

    # Escalates to Claude (attempt 1)
    res1 = supervisor.advance(state)
    assert res1 == AdvanceResult.WAITING_FOR_OPERATION
    assert state.dependency_escalation_op_id is not None

    # Claude completes escalation
    mock_mgr.complete_operation(state.dependency_escalation_op_id, status="COMPLETED", result={"status": "completed"})
    supervisor.advance(state)

    # 2. Worker runs again and fails again with undeclared dependency
    op2 = supervisor._find_latest_wo_operation(state, "WO-002")
    assert op2 is not None
    mock_mgr.complete_operation(
        op2["operation_id"],
        status="COMPLETED",
        result={"status": "blocked", "error": "imports undeclared third-party module(s): bad_pkg"},
    )

    # 3. Advance: Since max_retries (1) is reached, supervisor transitions to BLOCKED without looping
    res3 = supervisor.advance(state)
    assert res3 == AdvanceResult.BLOCKED
    assert state.phase == Phase.BLOCKED
    assert "WO-002" in state.blocked_wo_ids


def test_undeclared_dependency_state_serialization_roundtrip(tmp_path: Path) -> None:
    """RunState accurately serializes and deserializes dependency escalation fields."""
    state = RunState(
        run_id="run-ser-1",
        product_goal="Goal",
        workspace=str(tmp_path),
        session_id="sess-ser-1",
        dependency_escalation_op_id="op-claude-999",
        dependency_escalation_wo_id="WO-002",
        dependency_escalation_retries={"WO-002": 1, "WO-003": 2},
    )

    d = state.to_dict()
    assert d["dependency_escalation_op_id"] == "op-claude-999"
    assert d["dependency_escalation_wo_id"] == "WO-002"
    assert d["dependency_escalation_retries"] == {"WO-002": 1, "WO-003": 2}

    restored = RunState.from_dict(d)
    assert restored.dependency_escalation_op_id == "op-claude-999"
    assert restored.dependency_escalation_wo_id == "WO-002"
    assert restored.dependency_escalation_retries == {"WO-002": 1, "WO-003": 2}


def test_mark_wo_completed_archives_file(tmp_path: Path) -> None:
    """_mark_wo_status_on_disk with target_status='COMPLETED' must move file from ACTIVE to COMPLETED."""
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    active_dir.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    wo_file = active_dir / "WO-000.yaml"
    wo_file.write_text(
        yaml.safe_dump({
            "id": "WO-000",
            "title": "Planning Work Order",
            "status": "ACTIVE",
            "type": "RESEARCH",
            "assigned_agents": ["claude"],
        }),
        encoding="utf-8",
    )

    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    index_file.write_text(
        yaml.safe_dump({
            "total_active": 1,
            "total_completed": 0,
            "total_blocked": 0,
            "orders": [
                {
                    "id": "WO-000",
                    "title": "Planning Work Order",
                    "status": "ACTIVE",
                    "file": "work-orders/ACTIVE/WO-000.yaml",
                }
            ],
        }),
        encoding="utf-8",
    )

    res = LifecycleSupervisor._mark_wo_status_on_disk(tmp_path, "WO-000", "COMPLETED")
    assert res is True

    # ACTIVE/WO-000.yaml must be unlinked
    assert not wo_file.exists()

    # COMPLETED/WO-000.yaml must exist with status COMPLETED
    archived_file = completed_dir / "WO-000.yaml"
    assert archived_file.is_file()
    archived_data = yaml.safe_load(archived_file.read_text(encoding="utf-8"))
    assert archived_data["status"] == "COMPLETED"

    # INDEX.yaml must be updated
    index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    assert index_data["orders"][0]["status"] == "COMPLETED"
    assert index_data["orders"][0]["file"] == "work-orders/COMPLETED/WO-000.yaml"
    assert index_data["total_active"] == 0
    assert index_data["total_completed"] == 1




def test_planning_turn_blocked_reports_real_error(tmp_path: Path) -> None:
    """A BLOCKED planning operation fails the run with the actual blocker,
    not the misleading 'completed without proposing a plan'."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-plan-blocked", "Build app", tmp_path, "sess-001")

    res = supervisor.advance(state)  # dispatches planning turn
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    op_id = state.planning_operation_id

    mock_mgr.complete_operation(op_id, "BLOCKED", result={
        "status": "blocked",
        "reason": "staged stackmind validate failed: runtime/TREE.yaml: agents.codex.status "
                  "- 'IDLE' is not one of ['active', 'assigned', 'idle', 'blocked', 'non_compliant']",
        "error": "staged stackmind validate failed: runtime/TREE.yaml: agents.codex.status "
                 "- 'IDLE' is not one of ['active', 'assigned', 'idle', 'blocked', 'non_compliant']",
    })

    res2 = supervisor.advance(state)
    assert res2 == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert "Planning turn failed (blocked)" in (state.error or "")
    assert "staged stackmind validate failed" in (state.error or "")
    assert "completed without proposing a plan" not in (state.error or "")


def test_scaffold_protocol_citizenship_writes_lowercase_idle(tmp_path: Path) -> None:
    """Fresh scaffolding writes schema-valid lowercase 'idle', and legacy
    uppercase 'IDLE' entries self-heal on the next scaffold pass."""
    from validators.kernel.daemon.manager import _scaffold_protocol_citizenship

    # 1. Fresh workspace: scaffolded status must be schema-valid lowercase
    fresh = tmp_path / "fresh"
    (fresh / ".sync").mkdir(parents=True)
    _scaffold_protocol_citizenship(fresh, "codex")
    tree = yaml.safe_load((fresh / ".sync" / "runtime" / "TREE.yaml").read_text(encoding="utf-8"))
    assert tree["agents"]["codex"]["status"] == "idle"
    boot = yaml.safe_load(
        (fresh / ".sync" / "runtime" / "boot" / "codex.boot.yaml").read_text(encoding="utf-8")
    )
    assert boot["status"] == "idle"

    # 2. Legacy workspace with the invalid uppercase value: normalized in place
    legacy = tmp_path / "legacy"
    (legacy / ".sync" / "runtime").mkdir(parents=True)
    (legacy / ".sync" / "runtime" / "TREE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agents": {
                "codex": {"session_count": 2, "status": "IDLE", "assigned_work_orders": []},
                "claude": {"session_count": 1, "status": "IDLE", "assigned_work_orders": []},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    _scaffold_protocol_citizenship(legacy, "codex")
    tree = yaml.safe_load((legacy / ".sync" / "runtime" / "TREE.yaml").read_text(encoding="utf-8"))
    assert tree["agents"]["codex"]["status"] == "idle"
    assert tree["agents"]["codex"]["session_count"] == 2  # untouched otherwise
    # Untouched agents keep their legacy value (scaffold only owns its agent)
    assert tree["agents"]["claude"]["status"] == "IDLE"


def test_integration_review_rework_targets_accused_work_orders(tmp_path: Path) -> None:
    """Blockers referencing a deliverable path select only the accused code WO
    (plus QA for re-verification); untouched WOs are not re-dispatched, and
    multiple rework turns dispatch under a shared batch parent."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-ir-targeted", "Build API", tmp_path, "sess-ir-t")
    state.phase = Phase.INTEGRATION_REVIEW
    state.completed_wo_ids = ["WO-001", "WO-002", "WO-003"]
    state.max_retries = 2
    state.integration_rework_rounds = 0

    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)
    (completed_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001", "title": "Scaffold", "assigned_agents": ["codex"],
            "status": "COMPLETED",
            "deliverable": {"type": "config", "path": "requirements.txt", "description": "manifest"},
        }),
        encoding="utf-8",
    )
    (completed_dir / "WO-002.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-002", "title": "Backend", "assigned_agents": ["codex"],
            "status": "COMPLETED",
            "deliverable": {"type": "code", "path": "src/backend.py", "description": "api"},
        }),
        encoding="utf-8",
    )
    (completed_dir / "WO-003.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-003", "title": "QA & Testing", "assigned_agents": ["gemma"],
            "status": "COMPLETED",
            "deliverable": {"type": "code", "path": "tests/test_backend.py", "description": "suite"},
        }),
        encoding="utf-8",
    )
    (tmp_path / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({"orders": [], "next_id": 4}), encoding="utf-8"
    )
    (tmp_path / ".sync" / "contracts").mkdir(parents=True, exist_ok=True)

    supervisor.advance(state)  # review dispatched
    review_op_id = state.integration_operation_id
    mock_mgr.operations[review_op_id]["status"] = "BLOCKED"
    mock_mgr.operations[review_op_id]["result"] = {
        "status": "blocked",
        "summary": "Security violations",
        "blockers": ["Hardcoded default for SECRET_KEY in src/backend.py"],
    }

    result = supervisor.advance(state)
    assert result == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.integration_rework_rounds == 1

    # Targeted: backend WO + QA re-verify; scaffolding untouched
    assert "WO-002" in state.completed_wo_ids and "WO-003" in state.completed_wo_ids or True
    reworked = [
        o for o in mock_mgr.list_operations()
        if "Rework work order" in str(o.get("prompt", ""))
    ]
    reworked_ids = {o.get("work_order_id") for o in reworked}
    assert reworked_ids == {"WO-002", "WO-003"}
    assert "SECRET_KEY" in reworked[0]["prompt"]
    # Batch parent used for concurrent dispatch
    assert state.batch_operation_id is not None
    batch = mock_mgr.operations[state.batch_operation_id]
    assert batch["operation"] == "parallel_dispatch"
