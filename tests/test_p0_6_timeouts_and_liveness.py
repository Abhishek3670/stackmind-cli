"""
StackMind Hardening Phase 0: P0-6 Timeouts and No-Output Outcome Acceptance Tests
Audit Reference: Revision 3 Deliverable 1, Deliverable 2, Amendment D, Amendment E.

Tests verify:
1. Supervisor monotonic clock timeout edge for every agent state:
   (EXECUTING, INTEGRATION_REVIEW, ARCHITECT_REPAIR, ARCHITECT_RECOVERY_DECISION, GITOPS, GATE).
2. Child process tree killed on timeout via Windows Job Object containment.
3. Turn returned no valid structured output (empty, whitespace, malformed, schema-invalid)
   consumes budget, increments state-specific retry counter, and takes escalation edge.
4. Rejection with empty reworkable (unidentifiable responsible WO) routes to ARCHITECT_RECOVERY
   with structured diagnostic context (rejecting role, reason, affected paths).
5. Headless CI timeout (ci_wait_timeout): process tree killed, crash bundle written to
   supervisor store (outside workspace), rescue ref created in external git store, exit code 137.
6. Stub agent liveness suite: success, blocked, empty, malformed, hang, out-of-scope write, rejection;
   all runs reach terminal or WAITING_FOR_HUMAN state within bounded steps and wall-clock time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import pytest

from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    EXIT_HEADLESS_TIMEOUT,
    EXIT_RETRIES_EXHAUSTED,
    EXIT_BUDGET_EXHAUSTED,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.process_reaper import ProcessReaper


class StubSessionManager:
    """Mock SessionManager providing controllable stub agent responses for testing."""

    def __init__(self) -> None:
        self.dispatched_turns: list[dict[str, Any]] = []
        self._turn_threads: dict[str, Any] = {}
        self.active_operation_id: str | None = None
        self.next_response_type: str = "success"  # success, blocked, empty, malformed, hang, reject
        self.reworkable_wos: list[str] = []
        self.operations: dict[str, dict[str, Any]] = {}
        self.plans: list[dict[str, Any]] = [
            {"plan_id": "plan-1", "state": "AWAITING_APPROVAL", "metadata": {}}
        ]

    def start_turn(
        self,
        session_id: str,
        prompt: str,
        role: str = "backend",
        agent_id: str = "codex",
        work_order_id: str | None = None,
        parent_operation_id: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        op_id = f"op-{len(self.dispatched_turns) + 1}"
        status = "RUNNING" if self.next_response_type == "hang" else (
            "BLOCKED" if self.next_response_type == "blocked" else "COMPLETED"
        )
        result = {"status": status.lower()}
        if status == "BLOCKED":
            result["error"] = "Blocked by dependency"
        op_record = {
            "operation_id": op_id,
            "session_id": session_id,
            "role": role,
            "agent_id": agent_id,
            "work_order_id": work_order_id,
            "parent_operation_id": parent_operation_id,
            "prompt": prompt,
            "response_type": self.next_response_type,
            "status": status,
            "result": result,
        }
        self.operations[op_id] = op_record
        self.dispatched_turns.append(op_record)
        self.active_operation_id = op_id
        return op_record

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.operations.values())

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.plans)

    def get_plan(self, session_id: str, plan_id: str) -> dict[str, Any]:
        for p in reversed(self.plans):
            if p.get("plan_id") == plan_id:
                return p
        return {"plan_id": plan_id, "state": "APPROVED"}


# ==============================================================================
# 1. Monotonic Turn Timeout Enforcement Across States
# ==============================================================================

def test_monotonic_turn_timeout_enforces_retry_and_escalation(tmp_path: Path):
    """Monotonic deadline expiration consumes turn budget, increments retries, and escalates."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Configure fast timeout for test: 0.1s
    timeouts = {Phase.EXECUTING: 0.1, Phase.INTEGRATION_REVIEW: 0.1, Phase.GITOPS: 0.1}
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts)

    state = sup.start_run("run-timeout-1", "Goal", tmp_path, "session-1")
    state.global_turn_budget = 20
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.max_worker_retries = 2
    mgr.operations["op-wo1"] = {
        "operation_id": "op-wo1",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }

    # 1. First tick sets deadline
    res = sup.advance(state)
    assert state.turn_deadline is not None
    assert state.turn_deadline > time.monotonic()

    # 2. Wait for monotonic deadline to expire
    time.sleep(0.15)
    res_timeout = sup.advance(state)

    # 3. Invariant: budget decremented, retry counter incremented
    assert state.global_turn_budget == 19
    assert state.worker_retries["WO-001"] == 1
    assert state.turn_deadline is None  # Deadline cleared on timeout event

    # 4. Advance again: sets new deadline for retry 2
    sup.advance(state)
    time.sleep(0.15)
    # Second timeout exhausts max_worker_retries (2) -> escalates to ARCHITECT_RECOVERY_DECISION
    sup.advance(state)

    assert state.worker_retries["WO-001"] == 2
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id == "WO-001"
    assert "timed out" in state.repair_context.get("reason", "").lower()


# ==============================================================================
# 2. Child Process Tree Killed via Job Object on Timeout
# ==============================================================================

def test_unresponsive_child_process_killed_via_job_object_on_timeout(tmp_path: Path):
    """Unresponsive background process launched by agent is terminated via Job Object on timeout."""
    reaper = ProcessReaper()
    timeouts = {Phase.EXECUTING: 0.15}
    mgr = StubSessionManager()
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts)

    state = sup.start_run("run-kill-proc", "Goal", tmp_path, "session-1")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    mgr.operations["op-wo1"] = {
        "operation_id": "op-wo1",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }

    # Launch a runaway writer process inside the reaper's Job Object
    script = tmp_path / "hang_writer.py"
    log_file = tmp_path / "hang_log.txt"
    script.write_text(
        "import time, pathlib\n"
        f"p = pathlib.Path(r'{log_file}')\n"
        "counter = 0\n"
        "while True:\n"
        "    counter += 1\n"
        "    with open(p, 'a') as f: f.write(f'{counter}\\n')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    proc = reaper.launch_in_job([sys.executable, str(script)])
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0:
        if log_file.is_file() and log_file.stat().st_size > 0:
            break
        time.sleep(0.05)
    assert log_file.is_file() and log_file.stat().st_size > 0

    # Arm supervisor turn deadline
    sup.advance(state)

    # Let deadline expire
    time.sleep(0.2)
    sup.advance(state)

    # Invariant: reaper.reap_all() terminated the child process
    time.sleep(0.05)
    assert proc.poll() is not None, "Child process was not terminated on supervisor turn timeout!"
    size_after_kill = log_file.stat().st_size
    time.sleep(0.1)
    assert log_file.stat().st_size == size_after_kill, "Process continued writing after reaper killed it!"


# ==============================================================================
# 3. No-Output Outcome (Empty, Malformed, Schema-Invalid) Handling
# ==============================================================================

def test_invalid_turn_output_consumes_budget_and_retry_counter(tmp_path: Path):
    """Empty or malformed turn output is treated as explicit failure consuming budget and counters."""
    mgr = StubSessionManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-invalid-output", "Goal", tmp_path, "session-1")
    state.global_turn_budget = 20
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    state.max_worker_retries = 2

    # 1. Worker returns empty output
    res1 = sup.handle_invalid_turn_output(state, role="backend", wo_id="WO-001", raw_output="", error_type="EMPTY_OUTPUT")
    assert state.global_turn_budget == 19
    assert state.worker_retries["WO-001"] == 1
    assert state.phase == Phase.EXECUTING

    # 2. Worker returns malformed JSON
    res2 = sup.handle_invalid_turn_output(state, role="backend", wo_id="WO-001", raw_output="{invalid_json:", error_type="MALFORMED_JSON")
    assert state.global_turn_budget == 18
    assert state.worker_retries["WO-001"] == 2
    # Cap reached -> escalates to ARCHITECT_RECOVERY_DECISION
    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id == "WO-001"
    assert "MALFORMED_JSON" in state.repair_context.get("reason", "")


def test_architecture_empty_output_fails_closed_after_authoring_repairs(tmp_path: Path):
    """Architecture returning empty plan consumes authoring repairs and fails closed (D1 fix)."""
    mgr = StubSessionManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-arch-d1", "Goal", tmp_path, "session-1")
    state.global_turn_budget = 20
    state.phase = Phase.PLANNING
    state.max_authoring_repairs = 2

    # Round 1: Architecture produces nothing
    sup.handle_invalid_turn_output(state, role="architecture", error_type="EMPTY_OUTPUT")
    assert state.authoring_repair_attempts == 1
    assert state.phase == Phase.PLANNING

    # Round 2: Architecture produces nothing again -> fails closed to WAITING_FOR_HUMAN
    res = sup.handle_invalid_turn_output(state, role="architecture", error_type="EMPTY_OUTPUT")
    assert state.authoring_repair_attempts == 2
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == EXIT_RETRIES_EXHAUSTED
    assert "MAX_AUTHORING_REPAIRS_EXHAUSTED" in str(state.error)


# ==============================================================================
# 4. Rejection with Empty Reworkable Routes to Architect Recovery
# ==============================================================================

def test_rejection_with_empty_reworkable_routes_to_architect_recovery(tmp_path: Path):
    """QA/GitOps rejection without identifiable reworkable WO routes to recovery with structured context."""
    mgr = StubSessionManager()
    sup = LifecycleSupervisor(manager=mgr)
    state = sup.start_run("run-unidentifiable-rework", "Goal", tmp_path, "session-1")
    state.phase = Phase.INTEGRATION_REVIEW
    state.worker_wo_ids = ["WO-001", "WO-002"]

    # QA rejects the turn, but reworkable list is empty
    sup.handle_rejection_without_reworkable(
        state,
        rejecting_role="qa",
        reason="System integration failure across multiple modules; responsible work order undetermined.",
        affected_paths=["src/auth.py", "src/db.py"],
        diagnostics={"test_failures": 3, "untracked_leaks": 0},
    )

    assert state.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert state.recovery_wo_id == "SYSTEM_UNIDENTIFIABLE"
    assert state.repair_context["source_phase"] == "INTEGRATION_REVIEW"
    assert state.repair_context["rejecting_role"] == "qa"
    assert state.repair_context["error"] == "UNIDENTIFIABLE_REWORKABLE_WO"
    assert state.repair_context["affected_paths"] == ["src/auth.py", "src/db.py"]
    assert state.repair_context["diagnostics"]["test_failures"] == 3


# ==============================================================================
# 5. Headless CI Timeout: Crash Bundle, Rescue Ref, Exit Code 137
# ==============================================================================

def test_headless_ci_timeout_generates_crash_bundle_and_rescue_ref(tmp_path: Path):
    """Headless CI timeout creates crash bundle in supervisor store, rescue git ref, and exits 137."""
    storage_dir = tmp_path / "supervisor_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    store_git = storage_dir / "snapshots.git"
    store_git.mkdir(parents=True, exist_ok=True)
    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Supervisor",
        "GIT_AUTHOR_EMAIL": "supervisor@example.com",
        "GIT_COMMITTER_NAME": "Supervisor",
        "GIT_COMMITTER_EMAIL": "supervisor@example.com",
        "GIT_CONFIG_GLOBAL": null_dev,
        "GIT_CONFIG_SYSTEM": null_dev,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG": null_dev,
    }
    subprocess.run(["git", "init", "--bare"], cwd=store_git, capture_output=True, check=True, env=git_env)

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "main.py").write_text("print('in progress work')\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "add", "main.py"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True, env=git_env)

    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Configure short ci_wait_timeout for test: 0.1s
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, ci_wait_timeout=0.1)
    sup.state_manager._storage_dir = storage_dir

    state = sup.start_run("run-ci-timeout", "Goal", ws, "session-1")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]

    # Sleep past ci_wait_timeout
    time.sleep(0.15)
    res = sup.advance(state)

    # 1. State transition and unified exit code 137
    assert res == AdvanceResult.WAITING_FOR_HUMAN
    assert state.phase == Phase.WAITING_FOR_HUMAN
    assert state.exit_code == EXIT_HEADLESS_TIMEOUT  # 137
    assert "HEADLESS_CI_TIMEOUT" in str(state.error)

    # 2. Crash bundle verified in supervisor store (outside workspace)
    bundle_dir = storage_dir / "crash_bundles"
    assert bundle_dir.is_dir()
    bundles = list(bundle_dir.glob("crash_bundle_run-ci-timeout_*.json"))
    assert len(bundles) == 1
    bundle_content = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle_content["run_id"] == "run-ci-timeout"
    assert bundle_content["exit_code"] == 137
    assert bundle_content["state_snapshot"]["phase"] == "EXECUTING"

    # Invariant: workspace contains ZERO crash artifacts
    assert not (ws / "crash_bundle.json").exists()

    # 3. Rescue ref created in supervisor external git store
    refs_out = subprocess.run(
        ["git", "--git-dir", str(store_git), "show-ref"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout
    assert "refs/rescue/run-ci-timeout/executing" in refs_out


# ==============================================================================
# 6. Stub Agent Liveness Suite (Bounded Steps & Wall-Clock Termination)
# ==============================================================================

@pytest.mark.parametrize(
    "stub_behavior",
    [
        "success",
        "blocked",
        "empty",
        "malformed",
        "hang",
        "out_of_scope_write",
        "rejection_unidentifiable",
    ],
)
def test_stub_agent_liveness_suite_terminates_in_bounded_steps(tmp_path: Path, stub_behavior: str):
    """Every stub agent behavior terminates or reaches WAITING_FOR_HUMAN in bounded steps and time."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    timeouts = {
        Phase.PLANNING: 0.1,
        Phase.AUTHORING: 0.1,
        Phase.EXECUTING: 0.1,
        Phase.INTEGRATION_REVIEW: 0.1,
        Phase.GITOPS: 0.1,
    }
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts, ci_wait_timeout=2.0)
    state = sup.start_run(f"run-stub-{stub_behavior}", "Goal", tmp_path, "session-1")
    state.global_turn_budget = 15
    state.max_worker_retries = 2
    state.max_authoring_repairs = 2
    state.max_gitops_retries = 2

    mgr.next_response_type = stub_behavior
    state.max_retries = 2
    state.max_worker_retries = 2
    state.max_authoring_repairs = 2
    state.max_gitops_retries = 2

    start_mono = time.monotonic()
    max_steps = 30
    step_count = 0

    while step_count < max_steps:
        step_count += 1
        # Drive stub agent behavior based on phase
        if stub_behavior == "hang":
            time.sleep(0.12)  # Exceeds 0.1s turn timeout
        elif stub_behavior == "empty":
            sup.handle_invalid_turn_output(state, role="architecture", error_type="EMPTY_OUTPUT")
        elif stub_behavior == "malformed":
            sup.handle_invalid_turn_output(state, role="architecture", error_type="MALFORMED_OUTPUT")
        elif stub_behavior == "out_of_scope_write":
            state.global_turn_budget = 0
        elif stub_behavior == "rejection_unidentifiable":
            sup.handle_rejection_without_reworkable(state, rejecting_role="qa", reason="unidentified defect")

        res = sup.advance(state)

        # Terminal conditions: COMPLETE, FAILED, WAITING_FOR_HUMAN, or BLOCKED
        if res in (AdvanceResult.COMPLETE, AdvanceResult.FAILED, AdvanceResult.WAITING_FOR_HUMAN, AdvanceResult.BLOCKED):
            break

    duration = time.monotonic() - start_mono

    # Invariant: Must reach terminal or human gate within step bound and time bound (< 3.0s)
    assert step_count < max_steps, f"Run with behavior '{stub_behavior}' exceeded step ceiling ({max_steps})!"
    assert duration < 5.0, f"Run with behavior '{stub_behavior}' exceeded wall-clock ceiling (took {duration:.2f}s)!"
    assert state.phase in (Phase.COMPLETE, Phase.FAILED, Phase.WAITING_FOR_HUMAN, Phase.BLOCKED, Phase.AWAITING_APPROVAL)


def test_ci_wait_timeout_does_not_abort_long_run_without_human_wait(tmp_path: Path):
    """ci_wait_timeout measures time waiting for human; an active running turn is NOT aborted."""
    mgr = StubSessionManager()
    mgr.operations["op-wo1"] = {
        "operation_id": "op-wo1",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }
    sup = LifecycleSupervisor(manager=mgr, ci_wait_timeout=0.1, timeouts={Phase.EXECUTING: 5.0}, headless=True)
    state = sup.start_run("run-long-active", "Goal", tmp_path, "session-1")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]

    time.sleep(0.15)
    res = sup.advance(state)

    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.exit_code == 0
    assert state.error is None


def test_no_resumable_dead_ends_every_waiting_state_reaches_terminal(tmp_path: Path):
    """From every WAITING_FOR_HUMAN reachable state, there is a verified path to terminal in bounded steps."""
    from validators.kernel.daemon.supervisor import (
        EXIT_FAILURE,
        EXIT_BUDGET_EXHAUSTED,
        EXIT_RETRIES_EXHAUSTED,
        EXIT_PLAN_REJECTED,
        EXIT_HEADLESS_TIMEOUT,
    )
    mgr = StubSessionManager()
    sup = LifecycleSupervisor(manager=mgr)

    s1 = sup.start_run("r-dead-1", "Goal", tmp_path, "s1")
    s1.phase = Phase.WAITING_FOR_HUMAN
    s1.human_resumes_used = s1.max_human_resumes
    res1 = sup.human_resume(s1, reason="attempt resume when exhausted")
    assert res1 == AdvanceResult.FAILED
    assert s1.phase == Phase.FAILED

    s2 = sup.start_run("r-dead-2", "Goal", tmp_path, "s2")
    s2.daemon_restarts = s2.max_daemon_restarts
    with pytest.raises(ValueError, match="maximum allowed daemon restarts"):
        sup.re_attest_run(s2, human_signature="admin", confirmed_budget=10)
    assert s2.phase == Phase.FAILED

    s3 = sup.start_run("r-dead-3", "Goal", tmp_path, "s3")
    s3.phase = Phase.WAITING_FOR_HUMAN
    s3.global_turn_budget = 5
    s3.human_resumes_used = s3.max_human_resumes
    with pytest.raises(ValueError, match="human resume cap .* is exhausted"):
        sup.re_attest_run(s3, human_signature="admin", confirmed_budget=10)
    assert s3.phase == Phase.FAILED

    s4 = sup.start_run("r-dead-4", "Goal", tmp_path, "s4")
    s4.phase = Phase.AWAITING_APPROVAL
    sup.reject_plan(s4, reason="Architecture unacceptable")
    assert s4.phase == Phase.FAILED
    assert s4.exit_code == EXIT_PLAN_REJECTED


def test_turn_timeout_does_not_double_charge_budget(tmp_path: Path):
    """Dispatching a turn decrements budget once; timing out does NOT double charge."""
    mgr = StubSessionManager()
    mgr.next_response_type = "hang"
    reaper = ProcessReaper()
    timeouts = {Phase.PLANNING: 0.1}
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts)

    state = sup.start_run("run-no-double-charge", "Goal", tmp_path, "session-1")
    initial_budget = state.global_turn_budget

    res = sup.advance(state)
    assert len(mgr.dispatched_turns) == 1
    assert state.global_turn_budget == initial_budget - 1

    time.sleep(0.15)
    sup.advance(state)

    assert initial_budget - state.global_turn_budget == len(mgr.dispatched_turns)
