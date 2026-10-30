"""Scratch-only workspace management for a single runtime attempt."""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath


HEAVY_IGNORE_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    "node_modules",
    ".next",
    ".turbo",
    "dist",
    "build",
    ".cache",
    "cache",
    ".idea",
    ".vscode",
    "dataset",
    "datasets",
    "checkpoints",
    "weights",
    "runs",
})

HEAVY_BINARY_EXTENSIONS = frozenset({
    # Model weights & large ML checkpoints
    ".pt", ".pth", ".bin", ".onnx", ".safetensors", ".h5", ".hdf5", ".ckpt", ".tflite", ".weights", ".pb",
    # Large archives
    ".tar", ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".iso",
    # Video & audio
    ".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv",
    ".mp3", ".wav", ".flac", ".ogg",
    # Heavy data frames
    ".parquet", ".arrow", ".feather",
})


def is_dataset_dir(p: Path) -> bool:
    """Identify directories containing large image/vision/ML datasets."""
    name_l = p.name.lower()
    if any(k in name_l for k in ("dataset", "detection", "sentinel")):
        return True
    if (p / "data.yaml").is_file():
        return True
    if (p / "images").is_dir() and (p / "labels").is_dir():
        return True
    if any((p / sub).is_dir() for sub in ("train", "valid", "val", "test")):
        return True
    return False


def _load_gitignore_patterns(authoritative: Path) -> list[str]:
    gi_file = authoritative / ".gitignore"
    if not gi_file.is_file():
        return []
    try:
        lines = gi_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        patterns = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns
    except Exception:
        return []


def _matches_gitignore(rel_str: str, name: str, is_dir: bool, patterns: list[str]) -> bool:
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p.endswith("/"):
            if not is_dir:
                continue
            p = p.rstrip("/")
        if "/" in p:
            p_clean = p.lstrip("/")
            if fnmatch.fnmatch(rel_str, p_clean) or fnmatch.fnmatch(rel_str, f"{p_clean}/*"):
                return True
            parts = rel_str.split("/")
            for i in range(1, len(parts)):
                if fnmatch.fnmatch("/".join(parts[:i]), p_clean):
                    return True
        else:
            if fnmatch.fnmatch(name, p):
                return True
    return False


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
        gi_patterns = _load_gitignore_patterns(authoritative)

        def _ignore(directory: str, files: list[str]) -> set[str]:
            d_path = Path(directory).resolve()
            rel = d_path.relative_to(authoritative)
            ignored = set()
            for name in files:
                full_item = d_path / name
                rel_item = (rel / name).as_posix() if str(rel) != "." else name
                is_dir = full_item.is_dir()
                if is_dir:
                    if name in HEAVY_IGNORE_DIRS or is_dataset_dir(full_item):
                        ignored.add(name)
                        continue
                    if rel == Path(".sync") and name in ("runtime", "knowledge", "snapshots", "reports", "state", "outbox", "lock", "drafts"):
                        ignored.add(name)
                        continue
                    if _matches_gitignore(rel_item, name, is_dir=True, patterns=gi_patterns):
                        if not rel_item.startswith(".sync"):
                            ignored.add(name)
                            continue
                else:
                    ext = Path(name).suffix.lower()
                    if ext in HEAVY_BINARY_EXTENSIONS:
                        ignored.add(name)
                        continue
                    if _matches_gitignore(rel_item, name, is_dir=False, patterns=gi_patterns):
                        if not rel_item.startswith(".sync"):
                            ignored.add(name)
                            continue
                    try:
                        if full_item.stat().st_size > 5 * 1024 * 1024:
                            ignored.add(name)
                            continue
                    except OSError:
                        pass
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
