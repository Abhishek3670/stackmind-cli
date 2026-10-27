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
    mgr.next_response_type = "hang"
    op = sup.manager.start_turn(
        state.session_id,
        "Run worker turn",
        role="backend",
        agent_id="codex",
        work_order_id="WO-001",
    )

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


def test_turn_timeout_does_not_double_charge_budget(tmp_path: Path):
    """Dispatching a turn decrements budget once; timing out does NOT double charge."""
    mgr = StubSessionManager()
    mgr.next_response_type = "hang"
    reaper = ProcessReaper()
    timeouts = {Phase.PLANNING: 0.1}
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts)

    state = sup.start_run("run-no-double-charge", "Goal", tmp_path, "session-1")
    initial_budget = state.global_turn_budget

    # First advance dispatches planning turn
    res = sup.advance(state)
    assert len(mgr.dispatched_turns) == 1
    assert state.global_turn_budget == initial_budget - 1

    # Let monotonic turn deadline expire
    time.sleep(0.15)
    sup.advance(state)

    # Invariant: Budget was charged exactly once for the dispatched turn (not double charged)
    assert initial_budget - state.global_turn_budget == len(mgr.dispatched_turns)


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
# ==============================================================================
# 5. Headless CI Wait Timeout Semantics: Human Wait Abortion & Non-Interference
# ==============================================================================

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
    # Configure short ci_wait_timeout: 0.1s; phase timeout: 5.0s
    sup = LifecycleSupervisor(manager=mgr, ci_wait_timeout=0.1, timeouts={Phase.EXECUTING: 5.0}, headless=True)
    state = sup.start_run("run-long-active", "Goal", tmp_path, "session-1")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]

    # Sleep 0.15s (longer than ci_wait_timeout of 0.1s, but we are NOT in WAITING_FOR_HUMAN)
    time.sleep(0.15)
    res = sup.advance(state)

    # Invariant: Run is NOT aborted because it is not waiting for a human
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.exit_code == 0
    assert state.error is None


def test_headless_ci_wait_timeout_aborts_on_human_wait(tmp_path: Path):
    """A headless run waiting for human longer than ci_wait_timeout aborts with 137 and rescue ref."""
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
    (ws / "main.py").write_text("print('rescue file content')\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "add", "main.py"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True, env=git_env)

    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Configure short ci_wait_timeout for test: 0.1s
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, ci_wait_timeout=0.1, headless=True)
    sup.state_manager._storage_dir = storage_dir

    state = sup.start_run("run-ci-wait-timeout", "Goal", ws, "session-1")
    # Launch runaway writer in Job Object to verify process is killed prior to rescue ref snapshot
    runaway = reaper.launch_in_job([sys.executable, "-c", "import time; time.sleep(10)"])

    # Transition to WAITING_FOR_HUMAN
    sup._transition(state, Phase.WAITING_FOR_HUMAN)
    assert state.waiting_for_human_entered_at is not None

    # Wait 0.15s to exceed ci_wait_timeout
    time.sleep(0.15)
    res = sup.advance(state)

    # Invariant: Runaway child process was terminated before or during rescue capture
    assert runaway.poll() is not None, "Child process tree was not reaped prior to rescue capture!"

    # 1. State transition to terminal FAILED and unified exit code 137
    assert res == AdvanceResult.FAILED
    assert state.phase == Phase.FAILED
    assert state.exit_code == EXIT_HEADLESS_TIMEOUT  # 137
    assert "HEADLESS_CI_TIMEOUT" in str(state.error)

    # 2. Crash bundle verified in supervisor store (outside workspace)
    bundle_dir = storage_dir / "crash_bundles"
    assert bundle_dir.is_dir()
    bundles = list(bundle_dir.glob("crash_bundle_run-ci-wait-timeout_*.json"))
    assert len(bundles) == 1
    bundle_content = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle_content["run_id"] == "run-ci-wait-timeout"
    assert bundle_content["exit_code"] == 137
    assert bundle_content["budget"] is not None
    assert bundle_content["counters"] is not None
    assert bundle_content["checkpoint_hash"] is not None

    # Invariant: workspace contains ZERO crash artifacts
    assert not (ws / "crash_bundle.json").exists()

    # 3. Rescue ref created in supervisor external git store with exact file contents
    cat_out = subprocess.run(
        ["git", "--git-dir", str(store_git), "cat-file", "-p", "refs/rescue/run-ci-wait-timeout/waiting_for_human:main.py"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env,
    ).stdout
    assert "print('rescue file content')" in cat_out


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

    # 1. Human resumes exhausted -> fails terminal FAILED, not unresumable wait
    s1 = sup.start_run("r-dead-1", "Goal", tmp_path, "s1")
    s1.phase = Phase.WAITING_FOR_HUMAN
    s1.human_resumes_used = s1.max_human_resumes  # 2
    res1 = sup.human_resume(s1, reason="attempt resume when exhausted")
    assert res1 == AdvanceResult.FAILED
    assert s1.phase == Phase.FAILED

    # 2. Daemon restart cap exceeded -> fails terminal FAILED
    s2 = sup.start_run("r-dead-2", "Goal", tmp_path, "s2")
    s2.phase = Phase.WAITING_FOR_HUMAN
    s2.daemon_restarts = s2.max_daemon_restarts  # 3
    with pytest.raises(ValueError, match="maximum allowed daemon restarts"):
        sup.re_attest_run(s2, human_signature="admin", confirmed_budget=20)
    assert s2.phase == Phase.FAILED
    assert s2.exit_code == EXIT_FAILURE

    # 3. Re-attest cannot raise budget when resumes exhausted -> fails terminal FAILED
    s3 = sup.start_run("r-dead-3", "Goal", tmp_path, "s3")
    s3.phase = Phase.WAITING_FOR_HUMAN
    s3.global_turn_budget = 5
    s3.human_resumes_used = s3.max_human_resumes  # 2
    with pytest.raises(ValueError, match="human resume cap .* is exhausted"):
        sup.re_attest_run(s3, human_signature="admin", confirmed_budget=15)
    assert s3.phase == Phase.FAILED
    assert s3.exit_code == EXIT_BUDGET_EXHAUSTED

    # 4. In headless mode, waiting for human aborts terminal to FAILED
    sup_headless = LifecycleSupervisor(manager=mgr, ci_wait_timeout=0.05, headless=True)
    s4 = sup_headless.start_run("r-dead-4", "Goal", tmp_path, "s4")
    sup_headless._transition(s4, Phase.WAITING_FOR_HUMAN)
    time.sleep(0.08)
    res4 = sup_headless.advance(s4)
    assert res4 == AdvanceResult.FAILED
    assert s4.phase == Phase.FAILED
    assert s4.exit_code == EXIT_HEADLESS_TIMEOUT  # 137

    # 5. Plan review rejected -> fails terminal FAILED
    s5 = sup.start_run("r-dead-5", "Goal", tmp_path, "s5")
    s5.phase = Phase.AWAITING_APPROVAL
    res5 = sup.reject_plan(s5, reason="Human rejected architecture plan")
    assert res5 == AdvanceResult.FAILED
    assert s5.phase == Phase.FAILED
    assert s5.exit_code == EXIT_PLAN_REJECTED  # 4

    # 6. Valid human resume progresses to active phase within bounded steps
    s6 = sup.start_run("r-dead-6", "Goal", tmp_path, "s6")
    s6.phase = Phase.WAITING_FOR_HUMAN
    s6.global_turn_budget = 0
    s6.budget_exhausted = True
    sup.human_resume(s6, reason="Grant extra budget", additional_budget=10)
    assert s6.phase != Phase.WAITING_FOR_HUMAN
    assert s6.global_turn_budget == 10


# ==============================================================================
# 6. Stub Agent Liveness Suite (Bounded Steps & Wall-Clock Termination)
# ==============================================================================

def test_full_timeout_table_execution_and_counters(tmp_path: Path):
    """Verify every row of published timeout table: state, key, default, target, and counter consumed."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper)

    # 1. Defaults verification
    assert sup.get_timeout_for_phase(Phase.PLANNING) == 60.0
    assert sup.get_timeout_for_phase(Phase.AUTHORING) == 600.0
    assert sup.get_timeout_for_phase(Phase.AUTHORING_READINESS_GATE) == 60.0
    assert sup.get_timeout_for_phase(Phase.DISPATCHING) == 60.0
    assert sup.get_timeout_for_phase(Phase.EXECUTING) == 180.0
    assert sup.get_timeout_for_phase(Phase.QA_REVIEW) == 180.0
    assert sup.get_timeout_for_phase(Phase.INTEGRATION_REVIEW) == 180.0
    assert sup.get_timeout_for_phase(Phase.ARCHITECT_REPAIR) == 180.0
    assert sup.get_timeout_for_phase(Phase.ARCHITECT_RECOVERY_DECISION) == 300.0
    assert sup.get_timeout_for_phase(Phase.GITOPS) == 60.0
    assert sup.ci_wait_timeout == 60.0
    assert sup.ci_run_timeout == 1800.0

    # 2. PLANNING timeout consumes planning_retries (NOT authoring_repair_attempts)
    s = sup.start_run("r-timeout-planning", "Goal", tmp_path, "s1")
    s.phase = Phase.PLANNING
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.planning_retries == 1
    assert s.authoring_repair_attempts == 0
    # Second timeout exceeds max_planning_retries (2) -> WAITING_FOR_HUMAN
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.planning_retries == 2
    assert s.phase == Phase.WAITING_FOR_HUMAN
    assert s.exit_code == EXIT_RETRIES_EXHAUSTED

    # 3. AUTHORING timeout consumes authoring_repair_attempts
    s = sup.start_run("r-timeout-auth", "Goal", tmp_path, "s2")
    s.phase = Phase.AUTHORING
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.authoring_repair_attempts == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.authoring_repair_attempts == 2
    assert s.phase == Phase.WAITING_FOR_HUMAN
    assert s.exit_code == EXIT_RETRIES_EXHAUSTED

    # 4. AUTHORING_READINESS_GATE timeout transitions to ARCHITECT_REPAIR
    s = sup.start_run("r-timeout-gate", "Goal", tmp_path, "s3")
    s.phase = Phase.AUTHORING_READINESS_GATE
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.phase == Phase.ARCHITECT_REPAIR

    # 5. DISPATCHING timeout transitions to ARCHITECT_RECOVERY_DECISION
    s = sup.start_run("r-timeout-dispatch", "Goal", tmp_path, "s4")
    s.phase = Phase.DISPATCHING
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.phase == Phase.ARCHITECT_RECOVERY_DECISION

    # 6. EXECUTING timeout consumes worker_retries[wo_id] -> ARCHITECT_RECOVERY_DECISION
    s = sup.start_run("r-timeout-exec", "Goal", tmp_path, "s5")
    s.phase = Phase.EXECUTING
    s.worker_wo_ids = ["WO-101"]
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.worker_retries["WO-101"] == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.worker_retries["WO-101"] == 2
    assert s.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert s.recovery_wo_id == "WO-101"

# 7b. QA_REVIEW timeout consumes qa_reworks[wo_id] -> ARCHITECT_RECOVERY_DECISION
    s = sup.start_run("r-timeout-qareview", "Goal", tmp_path, "s6b")
    s.phase = Phase.QA_REVIEW
    s.integration_wo_id = "WO-101"
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.qa_reworks["WO-101"] == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.qa_reworks["WO-101"] == 2
    assert s.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert s.recovery_wo_id == "WO-101"

    # 7. INTEGRATION_REVIEW (QA_REVIEW) timeout consumes qa_reworks[wo_id] -> ARCHITECT_RECOVERY_DECISION
    s = sup.start_run("r-timeout-qa", "Goal", tmp_path, "s6")
    s.phase = Phase.INTEGRATION_REVIEW
    s.integration_wo_id = "WO-101"
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.qa_reworks["WO-101"] == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.qa_reworks["WO-101"] == 2
    assert s.phase == Phase.ARCHITECT_RECOVERY_DECISION
    assert s.recovery_wo_id == "WO-101"

    # 8. ARCHITECT_REPAIR timeout consumes authoring_repair_attempts
    s = sup.start_run("r-timeout-repair", "Goal", tmp_path, "s7")
    s.phase = Phase.ARCHITECT_REPAIR
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.authoring_repair_attempts == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.authoring_repair_attempts == 2
    assert s.phase == Phase.WAITING_FOR_HUMAN
    assert s.exit_code == EXIT_RETRIES_EXHAUSTED

    # 9. ARCHITECT_RECOVERY_DECISION timeout consumes recovery_attempts
    s = sup.start_run("r-timeout-recovery", "Goal", tmp_path, "s8")
    s.phase = Phase.ARCHITECT_RECOVERY_DECISION
    s.recovery_wo_id = "WO-101"
    s.max_retries = 2
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.recovery_attempts["WO-101"] == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.recovery_attempts["WO-101"] == 2
    assert s.phase == Phase.WAITING_FOR_HUMAN
    assert s.exit_code == EXIT_RETRIES_EXHAUSTED

    # 10. GITOPS timeout consumes gitops_retries
    s = sup.start_run("r-timeout-gitops", "Goal", tmp_path, "s9")
    s.phase = Phase.GITOPS
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.gitops_retries == 1
    s.turn_deadline = time.monotonic() - 1.0
    sup.advance(s)
    assert s.gitops_retries == 2
    assert s.phase == Phase.WAITING_FOR_HUMAN
    assert s.exit_code == EXIT_RETRIES_EXHAUSTED


AGENT_OWNED_PHASES = [
    Phase.PLANNING,
    Phase.EXECUTING,
    Phase.QA_REVIEW,
    Phase.INTEGRATION_REVIEW,
    Phase.ARCHITECT_REPAIR,
    Phase.ARCHITECT_RECOVERY_DECISION,
    Phase.GITOPS,
]

STUB_BEHAVIORS = [
    "success",
    "blocked",
    "empty",
    "malformed",
    "hang",
    "out_of_scope_write",
    "rejection_unidentifiable",
]


@pytest.mark.parametrize("agent_phase", AGENT_OWNED_PHASES)
@pytest.mark.parametrize("stub_behavior", STUB_BEHAVIORS)
def test_stub_matrix_agent_phase_bounded_termination(tmp_path: Path, agent_phase: Phase, stub_behavior: str):
    """Every combination of agent-owned phase and stub behavior terminates in < 30 steps and < 3.0s."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Shrunk turn timeouts for fast test execution
    timeouts = {p: 0.04 for p in Phase}
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, timeouts=timeouts, ci_wait_timeout=1.5)
    state = sup.start_run(f"run-matrix-{agent_phase.name}-{stub_behavior}", "Goal", tmp_path, "s-matrix")
    state.phase = agent_phase
    state.global_turn_budget = 15
    state.max_worker_retries = 2
    state.max_authoring_repairs = 2
    state.max_gitops_retries = 2
    state.max_retries = 2
    state.max_planning_retries = 2
    state.worker_wo_ids = ["WO-001"]
    state.executing_wo_id = "WO-001"
    state.integration_wo_id = "WO-001"
    state.recovery_wo_id = "WO-001"

    start_mono = time.monotonic()
    max_steps = 30
    step_count = 0

    while step_count < max_steps:
        step_count += 1

        if stub_behavior == "hang":
            time.sleep(0.045)  # Exceeds 0.04s timeout
        elif stub_behavior == "empty":
            sup.handle_invalid_turn_output(state, role="agent", error_type="EMPTY_OUTPUT")
        elif stub_behavior == "malformed":
            sup.handle_invalid_turn_output(state, role="agent", error_type="MALFORMED_OUTPUT")
        elif stub_behavior == "out_of_scope_write":
            state.global_turn_budget = 0
        elif stub_behavior == "rejection_unidentifiable":
            sup.handle_rejection_without_reworkable(state, rejecting_role="qa", reason="unidentified defect")
        elif stub_behavior == "blocked":
            state.blocked = True
            state.error = "Agent blocked on external dependency"
            sup._transition(state, Phase.WAITING_FOR_HUMAN)
            break
        elif stub_behavior == "success":
            if state.phase == Phase.PLANNING:
                state.proposed_plan = {"milestones": ["M1"]}
                sup._transition(state, Phase.AUTHORING)
            elif state.phase == Phase.AUTHORING:
                sup._transition(state, Phase.AUTHORING_READINESS_GATE)
            elif state.phase == Phase.ARCHITECT_REPAIR:
                sup._transition(state, Phase.AUTHORING_READINESS_GATE)
            elif state.phase == Phase.AUTHORING_READINESS_GATE:
                sup._transition(state, Phase.DISPATCHING)
            elif state.phase == Phase.DISPATCHING:
                sup._transition(state, Phase.EXECUTING)
            elif state.phase == Phase.EXECUTING:
                sup._transition(state, Phase.INTEGRATION_REVIEW)
            elif state.phase == Phase.QA_REVIEW:
                sup._transition(state, Phase.INTEGRATION_REVIEW)
            elif state.phase == Phase.INTEGRATION_REVIEW:
                sup._transition(state, Phase.GITOPS)
            elif state.phase == Phase.GITOPS:
                sup._transition(state, Phase.COMPLETE)
            elif state.phase == Phase.ARCHITECT_RECOVERY_DECISION:
                sup._transition(state, Phase.EXECUTING)

        res = sup.advance(state)

        # Terminal conditions: COMPLETE, FAILED, WAITING_FOR_HUMAN, BLOCKED, or human gate in AWAITING_APPROVAL
        if (
            res in (AdvanceResult.COMPLETE, AdvanceResult.FAILED, AdvanceResult.WAITING_FOR_HUMAN, AdvanceResult.BLOCKED)
            or state.phase in (Phase.COMPLETE, Phase.FAILED, Phase.WAITING_FOR_HUMAN, Phase.AWAITING_APPROVAL, Phase.BLOCKED)
        ):
            break

    duration = time.monotonic() - start_mono

    # Invariants: Termination strictly bounded in < 30 steps and < 3.0s wall-clock
    assert step_count < max_steps, f"Phase {agent_phase.name} with behavior '{stub_behavior}' exceeded 30 steps ({step_count})!"
    assert duration < 3.0, f"Phase {agent_phase.name} with behavior '{stub_behavior}' took {duration:.2f}s (exceeded 3.0s)!"
    assert state.phase in (Phase.COMPLETE, Phase.FAILED, Phase.WAITING_FOR_HUMAN, Phase.AWAITING_APPROVAL, Phase.BLOCKED)


def test_every_terminal_failed_path_creates_rescue_ref_and_crash_bundle(tmp_path: Path):
    """Every terminal FAILED path writes a rescue ref and crash bundle to the supervisor store outside the workspace."""
    import subprocess
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
    (ws / "main.py").write_text("print('critical production rescue')\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "add", "main.py"], cwd=ws, capture_output=True, check=True, env=git_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, capture_output=True, check=True, env=git_env)

    mgr = StubSessionManager()
    reaper = ProcessReaper()
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper)
    sup.state_manager._storage_dir = storage_dir

    # 1. Plan rejection terminal path
    s_reject = sup.start_run("run-failed-plan-reject", "Goal", ws, "session-r1")
    s_reject.phase = Phase.AWAITING_APPROVAL
    sup.reject_plan(s_reject, reason="Architecture plan rejected")
    assert s_reject.phase == Phase.FAILED

    bundle_dir = storage_dir / "crash_bundles"
    assert bundle_dir.is_dir()
    bundles_reject = list(bundle_dir.glob("crash_bundle_run-failed-plan-reject_*.json"))
    assert len(bundles_reject) == 1
    assert "rejected" in bundles_reject[0].read_text(encoding="utf-8").lower()

    # Rescue ref exists in external store
    cat_out = subprocess.run(
        ["git", "--git-dir", str(store_git), "cat-file", "-p", "refs/rescue/run-failed-plan-reject/failed:main.py"],
        capture_output=True, text=True, check=True, env=git_env
    ).stdout
    assert "print('critical production rescue')" in cat_out

    # 2. Resumes exhausted terminal path
    s_resume = sup.start_run("run-failed-resumes", "Goal", ws, "session-r2")
    s_resume.phase = Phase.WAITING_FOR_HUMAN
    s_resume.human_resumes_used = s_resume.max_human_resumes  # 2
    res = sup.human_resume(s_resume, reason="excess resume")
    assert res == AdvanceResult.FAILED
    assert s_resume.phase == Phase.FAILED

    bundles_resume = list(bundle_dir.glob("crash_bundle_run-failed-resumes_*.json"))
    assert len(bundles_resume) == 1

    cat_out2 = subprocess.run(
        ["git", "--git-dir", str(store_git), "cat-file", "-p", "refs/rescue/run-failed-resumes/failed:main.py"],
        capture_output=True, text=True, check=True, env=git_env
    ).stdout
    assert "print('critical production rescue')" in cat_out2


def test_legitimate_long_run_below_ci_run_timeout_is_not_aborted(tmp_path: Path):
    """A legitimate long run executing multiple turns below ci_run_timeout is not aborted."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Configure ci_run_timeout to 10.0s for test
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, ci_run_timeout=10.0, headless=True)
    state = sup.start_run("run-legit-long", "Goal", tmp_path, "session-long")
    state.global_turn_budget = 30
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]

    # Run executes actively across multiple simulated steps over 0.2s (< 10.0s)
    mgr.operations["op-wo1"] = {
        "operation_id": "op-wo1",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }
    time.sleep(0.1)
    res = sup.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.exit_code == 0


def test_interactive_run_ignores_ci_run_timeout(tmp_path: Path):
    """An interactive/TUI run (headless=False) is never aborted by ci_run_timeout."""
    mgr = StubSessionManager()
    reaper = ProcessReaper()
    # Configure low ci_run_timeout (0.01s) but headless=False
    sup = LifecycleSupervisor(manager=mgr, reaper=reaper, ci_run_timeout=0.01, headless=False)
    state = sup.start_run("run-interactive", "Goal", tmp_path, "session-int")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    mgr.operations["op-wo1"] = {
        "operation_id": "op-wo1",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }
    # Artificially set run_started_at far in the past (> 0.01s)
    state.run_started_at = time.monotonic() - 100.0

    res = sup.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING
    assert state.error is None


def test_prepare_run_for_resume_resets_run_started_at(tmp_path: Path):
    """Resuming a failed or blocked run resets run_started_at to current monotonic time."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon-storage.json")
    mgr = SessionManager(storage)
    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    state = mgr.supervisor.start_run("run-test-resume", "Goal", tmp_path, session["session_id"])
    state.phase = Phase.FAILED
    state.error = "HEADLESS_RUN_TIMEOUT: Total run wall-clock time exceeded 1800.0s."
    state.run_started_at = time.monotonic() - 5000.0
    state.turn_deadline = time.monotonic() - 100.0

    mgr._prepare_run_for_resume(state, session, tmp_path)

    assert state.error is None
    assert abs(state.run_started_at - time.monotonic()) < 2.0
    assert state.turn_deadline is None


def test_resume_cancelled_session_and_run(tmp_path: Path):
    """A session cancelled by operator (e.g. Ctrl+C) can be resumed cleanly."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon-storage.json")
    mgr = SessionManager(storage)
    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]

    # Simulate operator interruption
    mgr.cancel_session(session_id)
    assert mgr._sessions[session_id]["state"] == "CANCELLED"

    # Operator runs :resume
    resumed = mgr.resume_session(session_id)
    assert resumed["state"] == "RUNNING"
    assert mgr._sessions[session_id]["state"] == "RUNNING"


def test_prepare_run_for_resume_unblocks_cancelled_session_and_stall(tmp_path: Path):
    """Resuming a run with a cancelled session clears stall ticks and unblocks the session."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon-storage.json")
    mgr = SessionManager(storage)
    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]
    mgr.cancel_session(session_id)
    assert mgr._sessions[session_id]["state"] == "CANCELLED"
    raw_session = mgr._sessions[session_id]

    state = mgr.supervisor.start_run("run-test-stall", "Goal", tmp_path, session_id)
    state.phase = Phase.WAITING_FOR_HUMAN
    state.stall_ticks = 3
    state.blocked_wo_ids = ["WO-004"]
    state.recovery_wo_id = "WO-001"
    state.error = "IDLE_STALL (C_stall): Phase EXECUTING stalled for 3 consecutive ticks"

    mgr._prepare_run_for_resume(state, session, tmp_path)

    assert state.stall_ticks == 0
    assert state.blocked_wo_ids == []
    assert state.recovery_wo_id is None
    assert state.error is None
    assert mgr._sessions[session_id]["state"] == "RUNNING"
    assert mgr._sessions[session_id]["state"] == "RUNNING"


def test_supervisor_unblock_in_flight_work_orders_restores_session(tmp_path: Path):
    """LifecycleSupervisor.unblock_in_flight_work_orders restores cancelled session and resets stall ticks."""
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    storage = DaemonStorage(tmp_path / "daemon-storage.json")
    mgr = SessionManager(storage)
    session = mgr.create_session("claude", "test", {}, str(tmp_path))
    session_id = session["session_id"]
    mgr._sessions[session_id]["state"] = "CANCELLED"

    state = mgr.supervisor.start_run("run-test-unblock", "Goal", tmp_path, session_id)
    state.stall_ticks = 3
    state.blocked_wo_ids = ["WO-004"]

    mgr.supervisor.unblock_in_flight_work_orders(state)

    assert state.stall_ticks == 0
    assert state.blocked_wo_ids == []
    assert mgr._sessions[session_id]["state"] == "RUNNING"
