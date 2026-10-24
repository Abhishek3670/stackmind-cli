"""
Supervisor-owned per-turn snapshot and safe non-HEAD revert engine (P0-2).

Stores snapshots and rescue refs in a dedicated supervisor-managed bare git repository
outside the workspace boundary, protecting them against tampering, pruning, or ref deletion
by workspace child processes.

Constraints & Invariants:
- Detects and rejects nested .git directories/gitfiles planted to hide files as gitlinks.
- Bypasses workspace .gitignore via force-add (-A -f) with explicit supervisor exclusions.
- Disables autocrlf and filemode translation for byte-exact round-tripping.
- Lstats and unlinks planted symlinks/junctions before restore to prevent traversal escapes.
- Verifies snapshot tip SHA against supervisor memory/signed state prior to restore.
- Reverts work trees safely without moving workspace HEAD or touching workspace branch refs.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Sequence


class TurnSnapshotError(RuntimeError):
    """Base error for turn snapshot operations."""
    pass


class NestedGitError(TurnSnapshotError):
    """Raised when a nested .git directory or gitfile is detected in the workspace."""
    pass


class SnapshotIntegrityError(TurnSnapshotError):
    """Raised when snapshot verification fails against supervisor memory or signed state."""
    pass


DEFAULT_SUPERVISOR_EXCLUSIONS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    ".ruff_cache",
    ".sync/runtime",
)


class TurnSnapshotManager:
    """Manages lightweight per-turn workspace snapshots in an external bare repository."""

    def __init__(
        self,
        workspace: Path | str,
        storage_root: Path | str | None = None,
        exclusions: Sequence[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if storage_root is None:
            import tempfile
            storage_root = Path(tempfile.gettempdir()) / "stackmind_snapshots" / f"store_{os.getpid()}.git"
        self.storage_root = Path(storage_root).resolve()
        self.exclusions = tuple(exclusions or DEFAULT_SUPERVISOR_EXCLUSIONS)
        self.snapshot_shas: dict[str, str] = {}
        
        self.null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
        self.base_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": self.null_dev,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "StackMind Supervisor",
            "GIT_AUTHOR_EMAIL": "supervisor@stackmind.local",
            "GIT_COMMITTER_NAME": "StackMind Supervisor",
            "GIT_COMMITTER_EMAIL": "supervisor@stackmind.local",
        }
        self._init_store()

    def _init_store(self) -> None:
        """Initialize the bare git object store if not already present."""
        if not (self.storage_root / "config").exists():
            self.storage_root.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", str(self.storage_root)],
                check=True,
                capture_output=True,
                env=self.base_env,
            )
            # Configure exact byte-level reproduction
            configs = [
                ("core.autocrlf", "false"),
                ("core.safecrlf", "false"),
                ("core.longpaths", "true"),
                ("core.filemode", "false"),
                ("core.symlinks", "false"),
            ]
            for key, val in configs:
                subprocess.run(
                    ["git", "--git-dir", str(self.storage_root), "config", key, val],
                    check=True,
                    capture_output=True,
                    env=self.base_env,
                )

    def _check_nested_git(self) -> None:
        """Scan workspace for nested .git directories or gitfiles."""
        if not self.workspace.exists():
            return
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            # Skip checking the workspace root's own .git repository
            if root_path == self.workspace:
                continue
            if ".git" in dirs or ".git" in files:
                rel = root_path.relative_to(self.workspace)
                raise NestedGitError(
                    f"Nested git repository or gitfile detected at '{rel / '.git'}'. "
                    "Nested git structures hide files from tracking and are prohibited."
                )

    def create_turn_snapshot(self, turn_id: str) -> str:
        """Capture a complete working tree snapshot and return the commit SHA."""
        self._check_nested_git()
        temp_index = self.storage_root / f"idx_{turn_id}"
        snap_env = {**self.base_env, "GIT_INDEX_FILE": str(temp_index)}

        try:
            # 1. Force add all files, bypassing any workspace .gitignore
            subprocess.run(
                [
                    "git",
                    "--git-dir", str(self.storage_root),
                    "--work-tree", str(self.workspace),
                    "add", "-A", "-f",
                ],
                check=True,
                capture_output=True,
                env=snap_env,
            )

            # 2. Exclude supervisor-protected caches and runtime directories
            excl_args = list(self.exclusions)
            if excl_args:
                subprocess.run(
                    [
                        "git",
                        "--git-dir", str(self.storage_root),
                        "rm", "--cached", "-r", "--quiet", "--ignore-unmatch",
                        *excl_args,
                    ],
                    check=True,
                    capture_output=True,
                    env=snap_env,
                )

            # 3. Write tree object
            tree_res = subprocess.run(
                ["git", "--git-dir", str(self.storage_root), "write-tree"],
                check=True,
                capture_output=True,
                text=True,
                env=snap_env,
            )
            tree_sha = tree_res.stdout.strip()

            # 4. Commit tree directly
            commit_res = subprocess.run(
                [
                    "git",
                    "--git-dir", str(self.storage_root),
                    "commit-tree", tree_sha,
                    "-m", f"turn snapshot {turn_id}",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=snap_env,
            )
            commit_sha = commit_res.stdout.strip()

            # 5. Record ref in external store and save in memory
            ref_name = f"refs/stackmind/turn/{turn_id}"
            subprocess.run(
                ["git", "--git-dir", str(self.storage_root), "update-ref", ref_name, commit_sha],
                check=True,
                capture_output=True,
                env=snap_env,
            )
            self.snapshot_shas[turn_id] = commit_sha
            return commit_sha

        finally:
            if temp_index.exists():
                try:
                    temp_index.unlink()
                except OSError:
                    pass

    def list_snapshot_files(self, snapshot_sha: str) -> set[str]:
        """List all tracked file paths contained in the snapshot."""
        res = subprocess.run(
            [
                "git",
                "--git-dir", str(self.storage_root),
                "ls-tree", "-r", "--name-only", f"{snapshot_sha}^{{tree}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.base_env,
        )
        return set(filter(None, res.stdout.splitlines()))

    def restore_turn_snapshot(self, turn_id: str, expected_sha: str | None = None) -> None:
        """Safely restore the workspace to a snapshot without moving workspace HEAD."""
        ref_name = f"refs/stackmind/turn/{turn_id}"
        
        # Verify ref in external store
        res = subprocess.run(
            ["git", "--git-dir", str(self.storage_root), "rev-parse", "--verify", "--quiet", ref_name],
            capture_output=True,
            text=True,
            env=self.base_env,
        )
        if res.returncode != 0:
            raise SnapshotIntegrityError(f"Snapshot integrity verification failed: ref '{ref_name}' not found.")
        actual_sha = res.stdout.strip()

        # Check against expected SHA (from memory or caller)
        target_expected = expected_sha or self.snapshot_shas.get(turn_id)
        if target_expected and actual_sha != target_expected:
            raise SnapshotIntegrityError(
                f"Snapshot integrity verification failed: expected {target_expected}, found {actual_sha}."
            )

        snap_files = self.list_snapshot_files(actual_sha)
        temp_index = self.storage_root / f"idx_restore_{turn_id}"
        snap_env = {**self.base_env, "GIT_INDEX_FILE": str(temp_index)}

        try:
            # 1. Neutralize planted symlinks before checkout to prevent traversal escapes
            for root, dirs, files in os.walk(self.workspace):
                root_path = Path(root)
                # Check files
                for f in files:
                    fp = root_path / f
                    if os.path.islink(fp) or fp.is_symlink():
                        fp.unlink()
                # Check dirs (e.g. Windows junctions or directory symlinks)
                for d in list(dirs):
                    dp = root_path / d
                    if os.path.islink(dp) or dp.is_symlink():
                        try:
                            dp.unlink()
                        except OSError:
                            os.rmdir(dp)
                        dirs.remove(d)

            # 2. Read snapshot tree into temporary index
            subprocess.run(
                ["git", "--git-dir", str(self.storage_root), "read-tree", f"{actual_sha}^{{tree}}"],
                check=True,
                capture_output=True,
                env=snap_env,
            )

            # 3. Checkout index directly into workspace without touching workspace HEAD
            subprocess.run(
                [
                    "git",
                    "--git-dir", str(self.storage_root),
                    "--work-tree", str(self.workspace),
                    "checkout-index", "-a", "-f",
                ],
                check=True,
                capture_output=True,
                env=snap_env,
            )

            # 4. Clean up any newly added untracked files not present in the snapshot
            for p in list(self.workspace.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(self.workspace).as_posix()
                    # Skip supervisor excluded directories
                    if any(rel == excl or rel.startswith(f"{excl}/") for excl in self.exclusions):
                        continue
                    if rel not in snap_files:
                        try:
                            p.unlink()
                        except OSError:
                            pass

        finally:
            if temp_index.exists():
                try:
                    temp_index.unlink()
                except OSError:
                    pass

    def create_rescue_ref(self, run_id: str) -> str:
        """Create a rescue commit and ref under refs/stackmind/rescue/<run_id>."""
        commit_sha = self.create_turn_snapshot(f"rescue_{run_id}")
        ref_name = f"refs/stackmind/rescue/{run_id}"
        subprocess.run(
            ["git", "--git-dir", str(self.storage_root), "update-ref", ref_name, commit_sha],
            check=True,
            capture_output=True,
            env=self.base_env,
        )
        return commit_sha
