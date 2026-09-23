"""Ordered structured events emitted by the local runtime daemon."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import RLock
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
        self._lock = RLock()
        self._listeners: dict[int, Callable[[RuntimeEvent], None]] = {}
        self._next_listener_id = 0

    def publish(self, name: str, session_id: str, **payload: Any) -> RuntimeEvent:
        with self._lock:
            event = RuntimeEvent(len(self._events) + 1, name, session_id, payload, _now())
            self._events.append(event)
            listeners = tuple(self._listeners.values())
            on_publish = self._on_publish
        if on_publish:
            on_publish(event)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                # A streaming listener must not be able to break daemon event publication.
                continue
        return event

    def subscribe(self, listener: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        """Register a live listener and return its idempotent unsubscribe hook."""
        with self._lock:
            listener_id = self._next_listener_id
            self._next_listener_id += 1
            self._listeners[listener_id] = listener

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.pop(listener_id, None)

        return unsubscribe

    def events(self, session_id: str | None = None, after: int = 0) -> list[RuntimeEvent]:
        with self._lock:
            return [
                event for event in self._events
                if event.sequence > after and (session_id is None or event.session_id == session_id)
            ]

    def dump(self) -> list[dict[str, Any]]:
        with self._lock:
            return [event.as_dict() for event in self._events]

    def tool_call(
        self,
        session_id: str,
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any],
        operation_id: str | None = None,
    ) -> RuntimeEvent:
        """Publish the standardized start event for a tool invocation."""
        return self.publish(
            "event.toolCall",
            session_id,
            status="running",
            tool_name=tool_name,
            call_id=call_id,
            arguments=arguments,
            operation_id=operation_id,
        )

    def tool_result(
        self,
        session_id: str,
        tool_name: str,
        call_id: str,
        status: str,
        *,
        operation_id: str | None = None,
        result: Any = None,
        error: Any = None,
    ) -> RuntimeEvent:
        """Publish a standardized terminal tool outcome."""
        if status not in {"success", "failure", "cancelled", "denied", "approval_required"}:
            raise ValueError("invalid tool result status")
        return self.publish(
            "event.toolResult",
            session_id,
            status=status,
            tool_name=tool_name,
            call_id=call_id,
            operation_id=operation_id,
            result=result,
            error=error,
        )
