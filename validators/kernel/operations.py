"""Auditable first-class operation requests and records."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OperationType(str, Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    RUN_COMMAND = "run_command"
    QUERY_GRAPH = "query_graph"


@dataclass(frozen=True)
class OperationRequest:
    operation_type: OperationType
    target: str
    session_id: str
    attempt_id: str
    actor_id: str
    provider_id: str
    correlation_id: str | None = None
    operation_id: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", str(uuid4()))


@dataclass(frozen=True)
class OperationRecord:
    request: OperationRequest
    authorized: bool
    reason: str
    result: Any = None
    recorded_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None


class OperationJournal:
    def __init__(self) -> None:
        self._records: list[OperationRecord] = []

    @property
    def records(self) -> tuple[OperationRecord, ...]:
        return tuple(self._records)

    def record(self, record: OperationRecord) -> OperationRecord:
        if any(existing.request.operation_id == record.request.operation_id for existing in self._records):
            raise ValueError("Operation IDs must be unique")
        self._records.append(record)
        return record

    def complete(self, operation_id: str, result: Any) -> OperationRecord:
        for index, record in enumerate(self._records):
            if record.request.operation_id == operation_id:
                completed = replace(record, result=result, completed_at=utc_now())
                self._records[index] = completed
                return completed
        raise KeyError(operation_id)
