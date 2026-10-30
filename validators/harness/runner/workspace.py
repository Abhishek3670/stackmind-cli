"""Workspace lock, operation application and release-target resolution.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import time
from pathlib import Path

from cli.lock import acquire_lock

from .models import (
    FileMove,
    FileWrite,
)

class _WorkspaceMixin:
    """Workspace lock, operation application and release-target resolution."""

    def _acquire_runtime_lock(self) -> tuple[bool, str, int]:
        waited_ms = 0
        for attempt in range(self.max_lock_retries):
            started = time.monotonic()
            ok, message = acquire_lock(self.sync_path, self.agent, session_id='harness')
            waited_ms += int((time.monotonic() - started) * 1000)
            if ok:
                return True, message, waited_ms
            if attempt == self.max_lock_retries - 1:
                return False, message, waited_ms
            self.sleep_fn(self.backoff_seconds * (2 ** attempt))
        return False, 'lock acquisition failed', waited_ms

    def _apply_ops(self, base_path: Path, ops: list[FileWrite | FileMove]) -> None:
        for op in ops:
            if isinstance(op, FileMove):
                source = base_path / op.source
                target = base_path / op.target
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                if source.exists():
                    source.rename(target)
                continue

            target = base_path / op.path
            target.parent.mkdir(parents=True, exist_ok=True)
            from validators.kernel.io import durable_write_text
            if op.append and target.exists():
                existing = target.read_text(encoding='utf-8')
                durable_write_text(target, existing + op.content, encoding='utf-8', newline='\n')
            else:
                durable_write_text(target, op.content, encoding='utf-8', newline='\n')

    def _resolve_fallback_release_target(self) -> str:
        """Resolve a non-empty release target for completion notices (GEMINI-04)."""
        try:
            version_file = self.project_path / "VERSION"
            if version_file.is_file():
                v = version_file.read_text(encoding="utf-8").strip()
                if v:
                    return f"v{v.lstrip('v')}"
        except Exception:
            pass
        try:
            tree_file = self.sync_path / "runtime" / "TREE.yaml"
            if tree_file.is_file():
                import yaml
                data = yaml.safe_load(tree_file.read_text(encoding="utf-8")) or {}
                rel = data.get("release")
                if rel:
                    return f"v{str(rel).strip().lstrip('v')}"
        except Exception:
            pass
        return "v0.1.0"
