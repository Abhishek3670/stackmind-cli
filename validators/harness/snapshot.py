"""Workspace filesystem snapshotting, diff derivation, and verification dimension primitives.

Implements Phase 0 (§28.2–§28.7) of StackMind Verified Procedural Learning:
1. Runner-owned before/after workspace change detection.
2. Observed vs. declared change set comparison.
3. Multi-dimensional verification status reporting.
4. Canonical learning eligibility gate (OBSERVABLE -> VERIFIED -> LEARNING_ELIGIBLE).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

DEFAULT_IGNORED_PATTERNS = frozenset({
    ".git",
    ".sync",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    ".vscode",
    ".windsurf",
    ".claude",
})


@dataclass(frozen=True)
class FileSnapshot:
    """Snapshot metadata and content hash for a single workspace file."""

    path: str
    size: int
    content_hash: str
    mtime: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "mtime": self.mtime,
            "path": self.path,
            "size": self.size,
        }


@dataclass(frozen=True)
class WorkspaceDiff:
    """Authoritative filesystem difference between two workspace snapshots."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def all_changed_files(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added + self.modified + self.deleted)))

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)

    def matches_declaration(self, declared_files: Sequence[str]) -> tuple[bool, str | None]:
        """Compare observed filesystem changes against LLM-declared modified_files."""
        declared_normalized = {
            p.replace("\\", "/").strip("/").removeprefix("./")
            for p in declared_files
            if p
        }
        observed_normalized = {
            p.replace("\\", "/").strip("/").removeprefix("./")
            for p in self.all_changed_files
        }

        unannounced_changes = observed_normalized - declared_normalized
        phantom_declarations = declared_normalized - observed_normalized

        if not unannounced_changes and not phantom_declarations:
            return True, None

        reasons: list[str] = []
        if unannounced_changes:
            reasons.append(f"Unannounced filesystem modifications: {sorted(unannounced_changes)}")
        if phantom_declarations:
            reasons.append(f"Declared files not modified on disk: {sorted(phantom_declarations)}")
        return False, "; ".join(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "all_changed_files": list(self.all_changed_files),
            "deleted": list(self.deleted),
            "is_empty": self.is_empty,
            "modified": list(self.modified),
        }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Runner-owned point-in-time filesystem snapshot of a workspace."""

    root: str
    timestamp: str
    files: dict[str, FileSnapshot] = field(default_factory=dict)

    @classmethod
    def capture(
        cls,
        root: Path | str,
        *,
        ignored_patterns: Sequence[str] | None = None,
        scope_paths: Sequence[str] | None = None,
    ) -> WorkspaceSnapshot:
        """Capture an authoritative snapshot of the workspace filesystem."""
        root_path = Path(root).resolve()
        ignored = set(ignored_patterns or DEFAULT_IGNORED_PATTERNS)
        files: dict[str, FileSnapshot] = {}

        for dirpath, dirnames, filenames in os.walk(root_path):
            rel_dir = Path(dirpath).relative_to(root_path).as_posix()
            
            # Prune ignored directories
            dirnames[:] = [
                d for d in dirnames
                if d not in ignored and not any((rel_dir + "/" + d).startswith(p) for p in ignored)
            ]

            if rel_dir != "." and (rel_dir in ignored or any(rel_dir.startswith(p) for p in ignored)):
                continue

            for fname in filenames:
                if fname in ignored:
                    continue
                file_full_path = Path(dirpath) / fname
                rel_file_path = file_full_path.relative_to(root_path).as_posix()

                if scope_paths and not any(
                    rel_file_path.startswith(p.strip("/")) for p in scope_paths
                ):
                    continue

                try:
                    stat = file_full_path.stat()
                    content = file_full_path.read_bytes()
                    content_hash = hashlib.sha256(content).hexdigest()
                    files[rel_file_path] = FileSnapshot(
                        path=rel_file_path,
                        size=stat.st_size,
                        content_hash=content_hash,
                        mtime=stat.st_mtime,
                    )
                except (OSError, PermissionError):
                    continue

        return cls(
            root=root_path.as_posix(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            files=files,
        )

    def diff(self, after_snapshot: WorkspaceSnapshot) -> WorkspaceDiff:
        """Derive authoritative diff against an after snapshot."""
        before_keys = set(self.files.keys())
        after_keys = set(after_snapshot.files.keys())

        added = tuple(sorted(after_keys - before_keys))
        deleted = tuple(sorted(before_keys - after_keys))
        
        modified_list: list[str] = []
        for common_path in sorted(before_keys & after_keys):
            before_file = self.files[common_path]
            after_file = after_snapshot.files[common_path]
            if (
                before_file.size != after_file.size
                or before_file.content_hash != after_file.content_hash
            ):
                modified_list.append(common_path)

        return WorkspaceDiff(
            added=added,
            modified=tuple(modified_list),
            deleted=deleted,
        )


@dataclass(frozen=True)
class VerificationDimensions:
    """Explicit, multi-dimensional verification flags for governed execution."""

    scope_verified: bool = False
    state_verified: bool = False
    code_verified: bool = False
    behavioral_verified: bool = False
    security_verified: bool = False
    outcome_verified: bool = False

    @property
    def all_passed(self) -> bool:
        return (
            self.scope_verified
            and self.state_verified
            and self.code_verified
            and self.behavioral_verified
            and self.security_verified
            and self.outcome_verified
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "all_passed": self.all_passed,
            "behavioral_verified": self.behavioral_verified,
            "code_verified": self.code_verified,
            "outcome_verified": self.outcome_verified,
            "scope_verified": self.scope_verified,
            "security_verified": self.security_verified,
            "state_verified": self.state_verified,
        }


class TrustLevel(str, Enum):
    """Canonical three-level trust hierarchy (§28.7 & §29)."""

    OBSERVABLE = "OBSERVABLE"
    VERIFIED = "VERIFIED"
    LEARNING_ELIGIBLE = "LEARNING_ELIGIBLE"


def evaluate_learning_eligibility(
    *,
    decision_status: str,
    dimensions: VerificationDimensions,
    declaration_matches: bool,
    has_unhandled_blockers: bool = False,
) -> TrustLevel:
    """Evaluate canonical trust progression gate.

    OBSERVABLE: Executed and recorded in history.
    VERIFIED: Passed multi-dimensional verification checks.
    LEARNING_ELIGIBLE: Verified, matching declarations, completed successfully without blockers.
    """
    if not dimensions.all_passed:
        return TrustLevel.OBSERVABLE

    if not declaration_matches or has_unhandled_blockers or decision_status != "completed":
        return TrustLevel.VERIFIED

    return TrustLevel.LEARNING_ELIGIBLE
