"""Crash-safe JSON persistence for daemon state and operation journals."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class DaemonStorage:
    """A small, dependency-free durable store owned by one daemon process."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "daemon-state.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sessions": {}, "events": []}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict):
            raise ValueError("Daemon state is invalid")
        data.setdefault("events", [])
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, self.path)
                break
            except (PermissionError, OSError):
                if attempt == 4:
                    try:
                        # Fallback for Windows file contention: direct write
                        with self.path.open("w", encoding="utf-8") as handle:
                            json.dump(state, handle, indent=2, sort_keys=True)
                    finally:
                        temporary.unlink(missing_ok=True)
                    break
                time.sleep(0.05)
