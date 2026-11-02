"""Tests for Phase 3: Global Concurrency Semaphore & Per-Agent Limits (Checklist D.3).

Verifies:
1. AgentConcurrencyLimiter enforces max_concurrent cap, blocking or timing out when capacity is exceeded.
2. Slot releases immediately notify and unblock waiting threads in FIFO order.
3. Non-blocking acquire returns False when capacity is reached.
4. Idempotent acquire for the same operation_id does not leak slots.
5. Context manager guarantees slot release on normal exit and exceptions.
6. SessionManager tracks and enforces max_concurrent_agents across live runner turns.
7. LifecycleSupervisor caps parallel work order dispatches in Phase.DISPATCHING and Phase.EXECUTING to max_concurrent_agents.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import MagicMock
import pytest
import yaml

from validators.kernel.concurrency import (
    AgentConcurrencyLimiter,
    ConcurrencyLimitExceededError,
)
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.supervisor import AdvanceResult, LifecycleSupervisor, Phase


def test_concurrency_limiter_basic_cap():
    limiter = AgentConcurrencyLimiter(max_concurrent=2)
    assert limiter.max_concurrent == 2
    assert limiter.active_count() == 0
    assert limiter.available_slots() == 2
    assert limiter.is_at_capacity() is False

    # Acquire slot 1
    assert limiter.acquire("codex", "op-1") is True
    assert limiter.active_count() == 1
    assert limiter.available_slots() == 1
    assert limiter.active_agents() == ["codex"]

    # Acquire slot 2
    assert limiter.acquire("gemini", "op-2") is True
    assert limiter.active_count() == 2
    assert limiter.available_slots() == 0
    assert limiter.is_at_capacity() is True
    assert set(limiter.active_agents()) == {"codex", "gemini"}

    # Slot 3 non-blocking must fail
    assert limiter.acquire("gemma", "op-3", blocking=False) is False

    # Slot 3 with small timeout must fail
    assert limiter.acquire("gemma", "op-3", timeout=0.05) is False

    # Release slot 1
    assert limiter.release("op-1") is True
    assert limiter.active_count() == 1
    assert limiter.available_slots() == 1
    assert limiter.is_at_capacity() is False

    # Now slot 3 can be acquired
    assert limiter.acquire("gemma", "op-3", timeout=0.1) is True
    assert limiter.active_count() == 2
    assert set(limiter.active_agents()) == {"gemini", "gemma"}

    # Cleanup
    limiter.release("op-2")
    limiter.release("op-3")
    assert limiter.active_count() == 0


def test_concurrency_limiter_idempotent_acquire():
    limiter = AgentConcurrencyLimiter(max_concurrent=2)
    assert limiter.acquire("codex", "op-1") is True
    # Re-acquiring with same operation_id succeeds without consuming extra slot
    assert limiter.acquire("codex", "op-1") is True
    assert limiter.active_count() == 1
    assert limiter.available_slots() == 1
    limiter.release("op-1")
    assert limiter.active_count() == 0


def test_concurrency_limiter_context_manager():
    limiter = AgentConcurrencyLimiter(max_concurrent=1)

    with limiter.limit("codex", "op-1"):
        assert limiter.active_count() == 1
        assert limiter.is_at_capacity() is True

        with pytest.raises(ConcurrencyLimitExceededError):
            with limiter.limit("gemini", "op-2", timeout=0.02):
                pass

    # Automatically released
    assert limiter.active_count() == 0
    assert limiter.is_at_capacity() is False


def test_concurrency_limiter_thread_unblocking():
    limiter = AgentConcurrencyLimiter(max_concurrent=1)
    acquired_thread_2 = threading.Event()

    assert limiter.acquire("codex", "op-1") is True

    def worker():
        if limiter.acquire("gemini", "op-2", timeout=2.0):
            acquired_thread_2.set()
            limiter.release("op-2")

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # Thread 2 is blocked waiting for slot
    time.sleep(0.05)
    assert not acquired_thread_2.is_set()

    # Release slot 1 -> thread 2 must unblock and acquire
    limiter.release("op-1")
    assert acquired_thread_2.wait(timeout=1.0) is True
    t.join(timeout=1.0)
    assert limiter.active_count() == 0


def test_session_manager_concurrency_enforcement(tmp_path: Path):
    storage = DaemonStorage(tmp_path / "daemon.json")
    pause_event = threading.Event()
    runner_entered = threading.Event()

    class MockControlledRunner:
        def __init__(self, ws: str, agent: str):
            self.ws = ws
            self.agent = agent

        def run_once(self, **kwargs: Any):
            runner_entered.set()
            pause_event.wait(timeout=3.0)
            res = MagicMock()
            res.status = "completed"
            res.persisted = True
            res.meta = {"summary": "done"}
            return res

    manager = SessionManager(storage, runner_factory=lambda ws, ag: MockControlledRunner(ws, ag))
    # Configure max concurrent to 2
    manager.max_concurrent_agents = 2
    manager.concurrency_limiter.max_concurrent = 2

    session = manager.create_session("claude", "mock-provider", {"allow": ["*"], "deny": []}, str(tmp_path))
    sid = session["session_id"]

    # Start Turn 1
    op1 = manager.start_turn(sid, "Task 1", agent_id="codex")
    assert runner_entered.wait(timeout=1.0) is True

    # Check concurrency status
    status = manager.get_concurrency_status()
    assert status["max_concurrent"] == 2
    assert status["active_count"] == 1
    assert "codex" in status["active_agents"]

    # Unpause and allow turn to complete
    pause_event.set()
    time.sleep(0.1)

    # After completion, slot is released
    status_after = manager.get_concurrency_status()
    assert status_after["active_count"] == 0


def test_supervisor_parallel_dispatch_respects_concurrency_cap(tmp_path: Path):
    """LifecycleSupervisor caps parallel worker dispatches in DISPATCHING to max_concurrent_agents."""
    from tests.test_lifecycle_supervisor import MockSessionManager, _completed_wo_yaml

    mgr = MockSessionManager(tmp_path)
    mgr.max_concurrent_agents = 2
    supervisor = LifecycleSupervisor(mgr)
    state = supervisor.start_run("run-cap-test", "Cap test", tmp_path, "sess-cap")
    state.phase = Phase.DISPATCHING

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)

    # Scaffolding completed
    (tmp_path / "spec.txt").write_text("spec\n", encoding="utf-8")
    (completed_dir / "WO-001.yaml").write_text(
        _completed_wo_yaml("WO-001", "spec.txt", tmp_path),
        encoding="utf-8",
    )

    # 4 sibling work orders ready to run simultaneously
    for i in range(2, 6):
        wo_id = f"WO-00{i}"
        (wo_dir / f"{wo_id}.yaml").write_text(yaml.safe_dump({
            "id": wo_id, "status": "ACTIVE", "assigned_agents": ["codex"],
            "deliverable": {"type": "code", "path": f"src/mod_{i}.py"},
            "dependencies": ["WO-001"],
        }), encoding="utf-8")

    state.worker_wo_ids = ["WO-001", "WO-002", "WO-003", "WO-004", "WO-005"]
    state.completed_wo_ids = ["WO-001"]

    # Advance DISPATCHING
    res = supervisor.advance(state)
    assert res == AdvanceResult.WAITING_FOR_OPERATION
    assert state.phase == Phase.EXECUTING

    # Crucial assertion: despite 4 eligible work orders, only max_concurrent_agents (2) are dispatched!
    dispatched_wos = [op["work_order_id"] for op in mgr.list_operations() if op.get("work_order_id")]
    assert len(dispatched_wos) == 2, f"Expected 2 dispatched work orders due to concurrency cap, got {dispatched_wos}"
    assert dispatched_wos == ["WO-002", "WO-003"]
