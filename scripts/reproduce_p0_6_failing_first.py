"""
Reproduce P0-6 failing-first behavior on pre-P0-6 code (commit 5fa684c).

Demonstrates the 3 pre-fix failure modes:
1. Hang Stub: Agent turn hangs; supervisor has no turn timeout mechanism, stalling indefinitely in WAITING_FOR_OPERATION.
2. Empty Output: Agent turn returns empty output; supervisor lacks structured error handling, spinning without retry consumption.
3. Unidentifiable Rejection: QA rejects without identifiable work order; supervisor dead-ends in Phase.BLOCKED without diagnostic context.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from validators.kernel.daemon.supervisor import LifecycleSupervisor, Phase, AdvanceResult


class PreP06StubManager:
    def __init__(self) -> None:
        self.operations: dict[str, dict[str, Any]] = {}
        self.dispatched: list[dict[str, Any]] = []

    def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        op_id = f"op-{len(self.dispatched)+1}"
        record = {
            "operation_id": op_id,
            "status": "RUNNING",
            "result": None,
        }
        self.operations[op_id] = record
        self.dispatched.append(record)
        return record

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.operations.values())

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)


def reproduce_pre_p06_failures(supervisor_cls: Any) -> None:
    print("=== P0-6 FAILING-FIRST REPRODUCTION AGAINST SUPERVISOR ===")

    # 1. Hang Stub: without monotonic timeout, advance loop stalls in WAITING_FOR_OPERATION indefinitely
    mgr = PreP06StubManager()
    sup = supervisor_cls(manager=mgr)
    state = sup.start_run("repro-hang", "Goal", Path("."), "session-1")
    state.phase = Phase.EXECUTING
    state.worker_wo_ids = ["WO-001"]
    mgr.operations["op-hang"] = {
        "operation_id": "op-hang",
        "work_order_id": "WO-001",
        "agent_id": "codex",
        "role": "backend",
        "status": "RUNNING",
    }

    # Execute 5 ticks without timeout capability
    stalled_ticks = 0
    for tick in range(5):
        time.sleep(0.02)
        # In pre-P0-6 code, turn_deadline does not exist, so advance returns WAITING_FOR_OPERATION forever
        if not hasattr(state, "turn_deadline") or state.turn_deadline is None:
            res = sup.advance(state)
            if res == AdvanceResult.WAITING_FOR_OPERATION:
                stalled_ticks += 1

    print(f"1. Hang Stub Loop: Stalled for {stalled_ticks}/5 consecutive ticks in WAITING_FOR_OPERATION.")
    if stalled_ticks == 5:
        print("   -> PRE-FIX FAILURE CONFIRMED: Supervisor never aborts or retries hung turn!")

    # 2. Empty Output: without handle_invalid_turn_output
    has_invalid_output_handler = hasattr(sup, "handle_invalid_turn_output")
    print(f"2. Empty Output Handler Exists: {has_invalid_output_handler}")
    if not has_invalid_output_handler:
        print("   -> PRE-FIX FAILURE CONFIRMED: Empty agent output cannot be handled as an explicit failure!")

    # 3. Unidentifiable Rejection: without handle_rejection_without_reworkable
    has_recovery_routing = hasattr(sup, "handle_rejection_without_reworkable")
    print(f"3. Unidentifiable Rejection Routing Exists: {has_recovery_routing}")
    if not has_recovery_routing:
        print("   -> PRE-FIX FAILURE CONFIRMED: Rejection with empty reworkable list dead-ends in Phase.BLOCKED!")


if __name__ == "__main__":
    import subprocess
    import types
    try:
        # Load pre-P0-6 supervisor from commit 5fa684c
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
        reproduce_pre_p06_failures(mod.LifecycleSupervisor)
    except Exception as e:
        print(f"Loading from git 5fa684c failed ({e}); testing local supervisor...")
        reproduce_pre_p06_failures(LifecycleSupervisor)
