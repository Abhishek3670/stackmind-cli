"""Scratch-only workspace management for a single runtime attempt."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath


class WorkspaceEscapeError(PermissionError):
    """Raised when an operation attempts to escape the scratch workspace."""


@dataclass
class ScratchWorkspace:
    """A disposable copy of an authoritative repository, never its live directory."""

    authoritative_root: Path
    root: Path
    attempt_id: str
    verified: bool = False

    @classmethod
    def create(cls, authoritative_root: Path, attempt_id: str) -> "ScratchWorkspace":
        authoritative = authoritative_root.resolve()
        if not authoritative.is_dir():
            raise ValueError("authoritative_root must be an existing directory")
        root = Path(tempfile.mkdtemp(prefix=f"stackmind-{attempt_id}-"))

        def _ignore(directory: str, files: list[str]) -> set[str]:
            rel = Path(directory).resolve().relative_to(authoritative)
            ignored = set()
            for name in files:
                if name in (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".coverage"):
                    ignored.add(name)
                elif rel == Path(".sync") and name in ("runtime", "knowledge", "snapshots", "reports", "state", "outbox", "lock", "drafts"):
                    ignored.add(name)
            return ignored

        shutil.copytree(authoritative, root, dirs_exist_ok=True, ignore=_ignore)
        return cls(authoritative, root.resolve(), attempt_id)

    def path_for(self, relative_target: str) -> Path:
        candidate = PurePath(relative_target.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WorkspaceEscapeError("Absolute and parent-directory targets are forbidden")
        resolved = (self.root / Path(candidate)).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceEscapeError("Target escapes the scratch workspace") from exc

        # Read fallback for .sync protocol files that exist in the authoritative repository
        if not resolved.exists() and candidate.parts and candidate.parts[0] == ".sync":
            auth_resolved = (self.authoritative_root / Path(candidate)).resolve()
            try:
                auth_resolved.relative_to(self.authoritative_root)
                if auth_resolved.exists():
                    return auth_resolved
            except ValueError:
                pass

        return resolved

    def verify(self) -> None:
        """Mark this scratch state ready for an externally authorized commit."""
        self.verified = True

    def change_set(self) -> Path:
        """Expose only a verified scratch tree; it never writes to authoritative_root."""
        if not self.verified:
            raise PermissionError("Scratch changes require verification before commit authorization")
        return self.root
