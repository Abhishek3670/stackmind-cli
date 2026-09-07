"""Explicit trust modes for IDE-integrated StackMind sessions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OperatingMode(StrEnum):
    """The source of an operation, not a claim about an IDE's capabilities."""

    GOVERNED = "governed"
    UNMANAGED = "unmanaged"


@dataclass(frozen=True)
class SessionModeState:
    session_id: str
    mode: OperatingMode
    learning_eligible: bool


class OperatingModeTracker:
    """Retains eligibility only while a session stays inside StackMind's gateway."""

    def __init__(self) -> None:
        self._states: dict[str, SessionModeState] = {}

    def start(self, session_id: str) -> SessionModeState:
        state = SessionModeState(session_id, OperatingMode.GOVERNED, True)
        self._states[session_id] = state
        return state

    def record_governed(self, session_id: str) -> SessionModeState:
        return self._states.setdefault(
            session_id, SessionModeState(session_id, OperatingMode.GOVERNED, True)
        )

    def mark_unmanaged(self, session_id: str) -> SessionModeState:
        state = SessionModeState(session_id, OperatingMode.UNMANAGED, False)
        self._states[session_id] = state
        return state

    def state_for(self, session_id: str) -> SessionModeState:
        return self._states.setdefault(
            session_id, SessionModeState(session_id, OperatingMode.GOVERNED, True)
        )
