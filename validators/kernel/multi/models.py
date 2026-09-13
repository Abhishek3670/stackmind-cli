"""Role-scoped models for supervised, non-peer-to-peer collaboration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contract import AgentContract
from ..identity import AgentIdentity
from ..workspace import ScratchWorkspace


class AgentRole(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class EnsembleMember:
    identity: AgentIdentity
    contract: AgentContract
    workspace: ScratchWorkspace
    role: AgentRole


@dataclass
class TaskDelegation:
    task_id: str
    parent_task: str | None
    from_agent: str
    to_agent: str
    instructions: str
    status: str = "PENDING"
    result: Any = None


@dataclass(frozen=True)
class HandoffRecord:
    task_id: str
    from_agent: str
    to_agent: str
    workspace: str
    change_summary: str
    verification: dict[str, bool]
