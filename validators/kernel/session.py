"""Session and attempt lifecycle state machines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LifecycleState(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TRANSITIONS = {
    LifecycleState.CREATED: {LifecycleState.RUNNING, LifecycleState.CANCELLED, LifecycleState.FAILED},
    LifecycleState.RUNNING: {LifecycleState.WAITING, LifecycleState.PAUSED, LifecycleState.VERIFYING,
                             LifecycleState.FAILED, LifecycleState.CANCELLED},
    LifecycleState.WAITING: {LifecycleState.RUNNING, LifecycleState.PAUSED, LifecycleState.FAILED,
                             LifecycleState.CANCELLED},
    LifecycleState.PAUSED: {LifecycleState.RUNNING, LifecycleState.CANCELLED},
    LifecycleState.VERIFYING: {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED},
    LifecycleState.COMPLETED: set(), LifecycleState.FAILED: set(), LifecycleState.CANCELLED: set(),
}


@dataclass
class Attempt:
    """One immutable-contract execution attempt within an agent session."""

    attempt_id: str
    contract: Any
    state: LifecycleState = LifecycleState.CREATED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    usage: dict[str, int] = field(default_factory=dict)

    def transition(self, target: LifecycleState) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise ValueError(f"Invalid attempt transition: {self.state.value} -> {target.value}")
        self.state = target
        self.updated_at = utc_now()

    def add_usage(self, **usage: int) -> None:
        if self.state in {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED}:
            raise ValueError("Cannot add usage to a terminal attempt")
        for key, value in usage.items():
            if value < 0:
                raise ValueError("Usage values must be non-negative")
            self.usage[key] = self.usage.get(key, 0) + value


@dataclass
class AgentSession:
    """The logical ownership and aggregate lifecycle of a runtime session."""

    agent_id: str
    provider_id: str
    workspace_id: str
    session_id: str = field(default_factory=lambda: str(uuid4()))
    state: LifecycleState = LifecycleState.CREATED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    attempts: list[Attempt] = field(default_factory=list)

    def create_attempt(self, contract: Any, attempt_id: str | None = None) -> Attempt:
        if self.state in {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED}:
            raise ValueError("Cannot create an attempt for a terminal session")
        frozen = contract.freeze() if hasattr(contract, "freeze") else contract
        attempt = Attempt(attempt_id or str(uuid4()), frozen)
        self.attempts.append(attempt)
        return attempt

    def transition(self, target: LifecycleState) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise ValueError(f"Invalid session transition: {self.state.value} -> {target.value}")
        self.state = target
        self.updated_at = utc_now()
