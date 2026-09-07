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
        shutil.copytree(authoritative, root, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".sync"))
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
        return resolved

    def verify(self) -> None:
        """Mark this scratch state ready for an externally authorized commit."""
        self.verified = True

    def change_set(self) -> Path:
        """Expose only a verified scratch tree; it never writes to authoritative_root."""
        if not self.verified:
            raise PermissionError("Scratch changes require verification before commit authorization")
        return self.root
