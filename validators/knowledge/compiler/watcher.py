"""Polling file watcher for incremental graph updates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .incremental import IncrementalUpdateResult, incremental_update


@dataclass(frozen=True)
class WatchBatch:
    """A debounced batch of changed and deleted paths."""

    changed_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]


class PollingWatcher:
    """Watch Python source files with exclusion and debounce semantics."""

    def __init__(
        self,
        project_path: Path,
        *,
        agent: str = 'codex',
        poll_interval: float = 0.25,
        debounce_seconds: float = 0.5,
        runner: Callable[..., IncrementalUpdateResult] | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.agent = agent
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds
        self.runner = runner or self._run_incremental
        self._snapshot = self._scan()
        self._pending_changed: set[str] = set()
        self._pending_deleted: set[str] = set()
        self._last_event_at: float | None = None

    def step(self, *, now: float | None = None) -> IncrementalUpdateResult | None:
        """Advance the watcher by one poll iteration."""
        current = self._scan()
        changed = {
            path
            for path, mtime in current.items()
            if path not in self._snapshot or self._snapshot[path] != mtime
        }
        deleted = set(self._snapshot) - set(current)

        clock = time.monotonic() if now is None else now
        if changed or deleted:
            self._pending_changed.update(changed)
            self._pending_deleted.update(deleted)
            self._last_event_at = clock
            self._snapshot = current
            return None

        self._snapshot = current
        if self._last_event_at is None:
            return None
        if clock - self._last_event_at < self.debounce_seconds:
            return None

        batch = WatchBatch(
            changed_paths=tuple(sorted(self._pending_changed)),
            deleted_paths=tuple(sorted(self._pending_deleted)),
        )
        self._pending_changed.clear()
        self._pending_deleted.clear()
        self._last_event_at = None
        return self.runner(
            self.project_path,
            agent=self.agent,
            changed_paths=batch.changed_paths,
            deleted_paths=batch.deleted_paths,
        )

    def watch(self, *, max_batches: int | None = None) -> int:
        """Run the watcher loop until interrupted or the batch limit is reached."""
        processed = 0
        while max_batches is None or processed < max_batches:
            result = self.step()
            if result is not None:
                processed += 1
            time.sleep(self.poll_interval)
        return processed

    def _run_incremental(
        self,
        project_path: Path,
        *,
        agent: str,
        changed_paths: tuple[str, ...],
        deleted_paths: tuple[str, ...],
    ) -> IncrementalUpdateResult:
        return incremental_update(
            project_path,
            agent=agent,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
        )

    def _scan(self) -> dict[str, int]:
        files: dict[str, int] = {}
        for path in sorted(self.project_path.rglob('*.py')):
            rel_parts = path.relative_to(self.project_path).parts
            excluded = {'.sync', '__pycache__', '.pytest_cache', '.ruff_cache'}
            if any(part in excluded for part in rel_parts):
                continue
            files[path.relative_to(self.project_path).as_posix()] = path.stat().st_mtime_ns
        return files


__all__ = ['PollingWatcher', 'WatchBatch']
