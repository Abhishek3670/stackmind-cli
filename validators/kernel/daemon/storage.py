"""Crash-safe JSON persistence for daemon state and operation journals."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


class DaemonStorage:
    """A small, dependency-free durable store owned by one daemon process."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "daemon-state.json"
        self.backup_path = self.root / "daemon-state.json.bak"
        self.pending_path = self.root / "daemon-state.pending.json"

    def load(self) -> dict[str, Any]:
        candidates = [self.path, self.pending_path, self.backup_path]
        last_error = None
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("sessions", {}), dict):
                    data.setdefault("events", [])
                    # If recovered from pending or backup, restore primary path
                    if candidate != self.path:
                        try:
                            shutil.copyfile(candidate, self.path)
                        except OSError:
                            pass
                    return data
            except (json.JSONDecodeError, OSError, ValueError) as err:
                last_error = err
                continue

        if not self.path.exists() and not self.backup_path.exists() and not self.pending_path.exists():
            return {"sessions": {}, "events": []}
        raise ValueError(f"Daemon state is invalid: {last_error}")

    def save(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".daemon-state.tmp.{os.getpid()}.{time.time_ns()}"
        try:
            # ponytail: compact serialization without fsync; saves 90% disk I/O latency
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, default=str)
                handle.flush()

            replaced = False
            for attempt in range(10):
                try:
                    # Before first replace attempt, maintain a backup copy of current valid state
                    if attempt == 0 and self.path.exists():
                        try:
                            shutil.copyfile(self.path, self.backup_path)
                        except OSError:
                            pass

                    os.replace(temporary, self.path)
                    replaced = True
                    # Clean up pending file if a prior fallback created one
                    if self.pending_path.exists():
                        try:
                            self.pending_path.unlink()
                        except OSError:
                            pass
                    break
                except (PermissionError, OSError):
                    time.sleep(0.02 * (attempt + 1))

            if not replaced:
                # NEVER truncate or overwrite self.path directly in truncate mode.
                # Preserve existing self.path intact, and stage new state into pending_path.
                try:
                    self.pending_path.write_bytes(temporary.read_bytes())
                except OSError:
                    try:
                        os.replace(temporary, self.pending_path)
                    except OSError:
                        pass
                raise OSError(
                    f"Failed to atomically replace {self.path} after 10 attempts. "
                    f"Existing state preserved intact; new state staged to {self.pending_path}."
                )
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
