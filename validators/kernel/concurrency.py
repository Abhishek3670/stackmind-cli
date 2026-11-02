"""Global concurrency management and per-workspace execution semaphores.

Enforces concurrency caps on the number of specialist agents executing
simultaneously, preventing memory exhaustion and ensuring deterministic coordination.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import threading
from typing import Any


DEFAULT_MAX_CONCURRENT_AGENTS: int = 2


class ConcurrencyLimitExceededError(RuntimeError):
    """Raised when an operation cannot acquire a concurrency slot within its timeout."""
    pass


class AgentConcurrencyLimiter:
    """Thread-safe concurrency limiter with FIFO queuing and status introspection."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        if max_concurrent is None:
            env_val = os.environ.get("STACKMIND_MAX_CONCURRENT_AGENTS")
            try:
                max_concurrent = int(env_val) if env_val is not None else DEFAULT_MAX_CONCURRENT_AGENTS
            except ValueError:
                max_concurrent = DEFAULT_MAX_CONCURRENT_AGENTS
        self._max_concurrent = max(1, int(max_concurrent))
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        # Map of operation_id -> agent_id
        self._active_slots: dict[str, str] = {}

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @max_concurrent.setter
    def max_concurrent(self, value: int) -> None:
        with self._condition:
            self._max_concurrent = max(1, int(value))
            self._condition.notify_all()

    def acquire(
        self,
        agent_id: str,
        operation_id: str,
        timeout: float | None = None,
        blocking: bool = True,
    ) -> bool:
        """Acquire an execution slot. Returns True if acquired, False on timeout or non-blocking failure."""
        import time
        with self._condition:
            # If already acquired for this operation_id, succeed idempotently
            if operation_id in self._active_slots:
                return True

            deadline = time.monotonic() + timeout if timeout is not None else None

            while len(self._active_slots) >= self._max_concurrent:
                if not blocking:
                    return False
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(timeout=remaining)
                else:
                    self._condition.wait()

            self._active_slots[operation_id] = agent_id
            return True

    def release(self, operation_id: str) -> bool:
        """Release an execution slot for an operation. Returns True if released, False if not tracked."""
        with self._condition:
            if operation_id in self._active_slots:
                self._active_slots.pop(operation_id, None)
                self._condition.notify_all()
                return True
            return False

    def is_at_capacity(self) -> bool:
        """Check whether all available concurrency slots are currently occupied."""
        with self._lock:
            return len(self._active_slots) >= self._max_concurrent

    def available_slots(self) -> int:
        """Return the number of slots available for new agent operations."""
        with self._lock:
            return max(0, self._max_concurrent - len(self._active_slots))

    def active_count(self) -> int:
        """Return the number of currently running agent operations."""
        with self._lock:
            return len(self._active_slots)

    def active_agents(self) -> list[str]:
        """Return the list of agent identifiers currently holding execution slots."""
        with self._lock:
            return list(self._active_slots.values())

    def active_operations(self) -> dict[str, str]:
        """Return a snapshot map of operation_id -> agent_id."""
        with self._lock:
            return dict(self._active_slots)

    @contextmanager
    def limit(self, agent_id: str, operation_id: str, timeout: float | None = None):
        """Context manager wrapping block execution with guaranteed slot release."""
        acquired = self.acquire(agent_id, operation_id, timeout=timeout)
        if not acquired:
            raise ConcurrencyLimitExceededError(
                f"Concurrency limit reached ({self._max_concurrent} active agents). "
                f"Operation '{operation_id}' for agent '{agent_id}' could not acquire execution slot."
            )
        try:
            yield
        finally:
            self.release(operation_id)
