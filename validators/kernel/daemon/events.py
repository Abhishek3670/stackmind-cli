"""Ordered structured events emitted by the local runtime daemon."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    name: str
    session_id: str
    payload: dict[str, Any]
    timestamp: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventDispatcher:
    """In-process replayable event stream with an optional persistence callback."""

    def __init__(self, events: list[dict[str, Any]] | None = None,
                 on_publish: Callable[[RuntimeEvent], None] | None = None) -> None:
        self._events = [RuntimeEvent(**event) for event in events or []]
        self._on_publish = on_publish

    def publish(self, name: str, session_id: str, **payload: Any) -> RuntimeEvent:
        event = RuntimeEvent(len(self._events) + 1, name, session_id, payload, _now())
        self._events.append(event)
        if self._on_publish:
            self._on_publish(event)
        return event

    def events(self, session_id: str | None = None, after: int = 0) -> list[RuntimeEvent]:
        return [
            event for event in self._events
            if event.sequence > after and (session_id is None or event.session_id == session_id)
        ]

    def dump(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._events]
