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
    """When authoring completes, supervisor discovers worker WOs on disk and transitions to DISPATCHING."""
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)
    state = supervisor.start_run("run-001", "Create login", tmp_path, "sess-001")
    state.phase = Phase.AUTHORING

    # Create authoring op
    op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
    state.authoring_operation_id = op["operation_id"]

    # Write worker WOs to disk
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text("id: WO-001\nassigned_agents: [codex]\n", encoding="utf-8")
    (wo_dir / "WO-002.yaml").write_text("id: WO-002\nassigned_agents: [gemini]\n", encoding="utf-8")
    (wo_dir / "WO-003.yaml").write_text("id: WO-003\nassigned_agents: [local-llm]\n", encoding="utf-8")

    # Complete authoring op
    mock_mgr.complete_operation(op["operation_id"], "COMPLETED")

    res = supervisor.advance(state)
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING
    assert state.worker_wo_ids == ["WO-001", "WO-002"]
    assert state.gitops_wo_id == "WO-003"


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


def test_supervisor_authoring_synthesis_fallback_when_model_emits_text(tmp_path: Path) -> None:
    """When an authoring turn finishes without tool writes (e.g. local LLM),
    supervisor autonomously synthesizes validated child WOs and contracts from PLAN.md
    and transitions to DISPATCHING.
    """
    mock_mgr = MockSessionManager(tmp_path)
    supervisor = LifecycleSupervisor(mock_mgr)

    # 1. Create PLAN.md on disk
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
    (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")

    # 2. Propose plan in mock manager
    mock_mgr.propose_plan(
        "sess-001",
        "PLAN-001",
        title="Login System",
        content=plan_content,
    )
    plan = mock_mgr.get_plan("sess-001", "PLAN-001")
    plan["state"] = "APPROVED"

    # 3. Create supervised run at AUTHORING phase
    state = supervisor.start_run("run-auth-test", "Login System", tmp_path, "sess-001")
    state.phase = Phase.AUTHORING
    state.plan_id = "PLAN-001"

    # 4. Simulate authoring operation that completed without writing files
    op = mock_mgr.start_turn("sess-001", "Author WOs", is_authoring=True, work_order_id="WO-000")
    state.authoring_operation_id = op["operation_id"]
    mock_mgr.complete_operation(
        op["operation_id"],
        status="COMPLETED",
        result={"status": "completed", "summary": "I have reviewed PLAN.md."},
    )

    # 5. Advance supervisor
    res = supervisor.advance(state)

    # Must transition to DISPATCHING with discovered worker WOs
    assert res == AdvanceResult.TRANSITIONED
    assert state.phase == Phase.DISPATCHING
    assert state.worker_wo_ids == ["WO-001", "WO-002", "WO-003"]
    assert state.error is None

    # Check that files exist on disk and pass validation
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contract_dir = tmp_path / ".sync" / "contracts"

    assert (wo_dir / "WO-001.yaml").is_file()
    assert (wo_dir / "WO-002.yaml").is_file()
    assert (wo_dir / "WO-003.yaml").is_file()
    assert (contract_dir / "WO-001.yaml").is_file()
    assert (contract_dir / "WO-002.yaml").is_file()
    assert (contract_dir / "WO-003.yaml").is_file()

    # Verify deliverable paths were intelligently derived
    wo1 = yaml.safe_load((wo_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert wo1["deliverable"]["path"] == "requirements.txt"

    wo2 = yaml.safe_load((wo_dir / "WO-002.yaml").read_text(encoding="utf-8"))
    assert wo2["deliverable"]["path"] == "src/backend.py"

    wo3 = yaml.safe_load((wo_dir / "WO-003.yaml").read_text(encoding="utf-8"))
    assert wo3["deliverable"]["path"] == "src/frontend.html"


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



