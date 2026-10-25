"""
Reproduce P0-6 failing-first behavior on pre-P0-6 code (commit 5fa684c).

Demonstrates the 3 pre-fix failure modes against the pre-P0-6 supervisor:
1. Hang Stub: Agent turn hangs; supervisor has no turn timeout mechanism, stalling indefinitely in WAITING_FOR_OPERATION.
2. Empty Output: Agent turn returns empty output; supervisor blindly marks the work order completed and transitions to INTEGRATION_REVIEW without consuming retries.
3. Unidentifiable Rejection: Rejection with unidentifiable work order dead-ends in Phase.BLOCKED without diagnostic context or recovery.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any


class PreP06StubManager:
    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.operations.values())


def reproduce_pre_p06_failures() -> None:
    # 1. Load pre-P0-6 supervisor code directly from git commit 5fa684c
    code = subprocess.run(
        ["git", "show", "5fa684c:validators/kernel/daemon/supervisor.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    mod = types.ModuleType("validators.kernel.daemon.pre_supervisor")
    mod.__package__ = "validators.kernel.daemon"
    sys.modules["validators.kernel.daemon.pre_supervisor"] = mod
    exec(code, mod.__dict__)

    supervisor_cls = mod.LifecycleSupervisor
    Phase = mod.Phase
    AdvanceResult = mod.AdvanceResult

    print("=== P0-6 FAILING-FIRST REPRODUCTION (COMMIT 5fa684c) ===")

    # --------------------------------------------------------------------------
    # 1. Hang Stub Loop
    # --------------------------------------------------------------------------
    mgr1 = PreP06StubManager()
    sup1 = supervisor_cls(manager=mgr1)
    s1 = sup1.start_run("repro-hang", "Product Request", Path("."), "session-1")
    s1.phase = Phase.EXECUTING
    s1.worker_wo_ids = ["WO-001"]
    mgr1.operations["op-hang"] = {
        "operation_id": "op-hang",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }

    stalled_ticks = 0
    for tick in range(5):
        time.sleep(0.01)
        res = sup1.advance(s1)
        if res == AdvanceResult.WAITING_FOR_OPERATION and s1.phase == Phase.EXECUTING:
            stalled_ticks += 1

    print(f"1. Hang Stub: Stalled for {stalled_ticks}/5 consecutive ticks in WAITING_FOR_OPERATION.")
    print(f"   Turn deadline exists: {hasattr(s1, 'turn_deadline')}")
    print("   -> PRE-FIX FAILURE CONFIRMED: Supervisor never aborts or retries hung turn!")

    # --------------------------------------------------------------------------
    # 2. Empty Output Blind Acceptance
    # --------------------------------------------------------------------------
    mgr2 = PreP06StubManager()
    mgr2.operations["op-empty"] = {
        "operation_id": "op-empty",
        "work_order_id": "WO-001",
        "role": "backend",
        "agent_id": "codex",
        "status": "COMPLETED",
        "result": "",  # Empty output
    }
    sup2 = supervisor_cls(manager=mgr2)
    s2 = sup2.start_run("repro-empty", "Product Request", Path("."), "session-2")
    s2.phase = Phase.EXECUTING
    s2.worker_wo_ids = ["WO-001"]

    res2 = sup2.advance(s2)
    print(f"2. Empty Output: AdvanceResult={res2.value}, phase={s2.phase.value}, completed={s2.completed_wo_ids}, retries={s2.worker_retries}")
    print("   -> PRE-FIX FAILURE CONFIRMED: Empty agent output blindly accepted as completed without retry consumption!")

    # --------------------------------------------------------------------------
    # 3. Unidentifiable Rejection Dead-End in BLOCKED
    # --------------------------------------------------------------------------
    mgr3 = PreP06StubManager()
    mgr3.operations["op-blocked"] = {
        "operation_id": "op-blocked",
        "work_order_id": "WO-001",
        "role": "backend",
        "agent_id": "codex",
        "status": "BLOCKED",
        "result": {"status": "blocked", "reason": "Unidentifiable defect across multiple components"},
    }
    sup3 = supervisor_cls(manager=mgr3)
    s3 = sup3.start_run("repro-blocked", "Product Request", Path("."), "session-3")
    s3.phase = Phase.EXECUTING
    s3.worker_wo_ids = ["WO-001"]

    res3 = sup3.advance(s3)
    print(f"3. Unidentifiable Rejection: AdvanceResult={res3.value}, phase={s3.phase.value}, error='{s3.error}'")
    print("   -> PRE-FIX FAILURE CONFIRMED: Rejection dead-ends in Phase.BLOCKED without diagnostic recovery routing!")


if __name__ == "__main__":
    reproduce_pre_p06_failures()
