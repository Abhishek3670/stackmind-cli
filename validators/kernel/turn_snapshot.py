"""
Supervisor-owned per-turn snapshot and safe non-HEAD revert engine (P0-2).

Stores snapshots and rescue refs in a dedicated supervisor-managed bare git repository
outside the workspace boundary, protecting them against tampering, pruning, or ref deletion
by workspace child processes.

Host Substrate:
- Primary host: Windows 11 (tested).
- Windows configuration: core.filemode=false, core.symlinks=false, core.autocrlf=false.
- Symlinks & junctions on Windows: detected and unlinked via os.lstat before restore.
- POSIX portable mode: captures executable bit (100755 vs 100644) when running on non-Windows hosts.

Filter Pipeline Bypass:
- Completely bypasses Git's working-tree filter pipeline (.gitattributes eol, ident, filters):
  * Snapshot: uses `hash-object -w --no-filters` and `update-index --add --cacheinfo` to
    stage raw file bytes directly into the object store without filter smudging or eol translation.
  * Restore: parses `ls-tree -r -z` and streams raw bytes from `cat-file blob <sha>` directly
    to workspace files, bypassing checkout-index and filter pipelines entirely.

Store Integrity & Defense-in-Depth:
- Hashes external store config at baseline; re-verifies SHA-256 before every supervisor operation.
- Injects `-c core.fsmonitor=false -c core.hooksPath="" -c core.excludesFile=""` and
  `GIT_CONFIG_GLOBAL/SYSTEM=null` on every invocation to neutralize config-injection attacks.
- Enforces process-tree reaping as a mandatory precondition prior to diff and restore.
- Unlinks files before overwrite on restore to destroy NTFS Alternate Data Streams (ADS).
- Deletes untracked files and removes emptied directories bottom-up on restore.
- Never moves workspace HEAD or alters workspace branch refs or .git/index.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .process_reaper import ProcessReaper, get_global_reaper


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
        reaper: ProcessReaper | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if storage_root is None:
            import tempfile
            storage_root = Path(tempfile.gettempdir()) / "stackmind_snapshots" / f"store_{os.getpid()}.git"
        self.storage_root = Path(storage_root).resolve()
        self.exclusions = tuple(exclusions or DEFAULT_SUPERVISOR_EXCLUSIONS)
        self.reaper = reaper or get_global_reaper()
        self.snapshot_shas: dict[str, str] = {}
        self.baseline_config_hash: str | None = None

        self.null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
        self.base_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": self.null_dev,
            "GIT_CONFIG_SYSTEM": self.null_dev,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "StackMind Supervisor",
            "GIT_AUTHOR_EMAIL": "supervisor@stackmind.local",
            "GIT_COMMITTER_NAME": "StackMind Supervisor",
            "GIT_COMMITTER_EMAIL": "supervisor@stackmind.local",
        }
        self._init_store()

    def _init_store(self) -> None:
        """Initialize the bare git object store if not already present."""
        config_path = self.storage_root / "config"
        if not config_path.exists():
            self.storage_root.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", str(self.storage_root)],
                check=True,
                capture_output=True,
                env=self.base_env,
            )
            # Configure exact byte-level reproduction & neutralize hooks/fsmonitor
            configs = [
                ("core.autocrlf", "false"),
                ("core.safecrlf", "false"),
                ("core.longpaths", "true"),
                ("core.filemode", "false"),
                ("core.symlinks", "false"),
                ("core.fsmonitor", "false"),
                ("core.hooksPath", ""),
                ("core.excludesFile", ""),
            ]
            for key, val in configs:
                subprocess.run(
                    [
                        "git",
                        "--git-dir", str(self.storage_root),
                        "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=",
                        "-c", "core.excludesFile=",
                        "config", key, val,
                    ],
                    check=True,
                    capture_output=True,
                    env=self.base_env,
                )

        # Baseline SHA-256 of the store configuration file
        self.baseline_config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()

    def _verify_store_config_integrity(self) -> None:
        """Verify the external store config has not been modified by workspace agents."""
        config_path = self.storage_root / "config"
        if not self.storage_root.exists() or not config_path.exists():
            raise SnapshotIntegrityError(
                f"External snapshot store corrupted: config file '{config_path}' is missing or deleted."
            )
        current_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        if self.baseline_config_hash and current_hash != self.baseline_config_hash:
            raise SnapshotIntegrityError(
                f"External snapshot store config tampering detected! "
                f"Expected hash {self.baseline_config_hash}, found {current_hash}."
            )

    def _git_run(
        self,
        args: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a git plumbing command with store integrity check and defensive flags."""
        self._verify_store_config_integrity()
        cmd = [
            "git",
            "--git-dir", str(self.storage_root),
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
            *args,
        ]
        effective_env = env or self.base_env
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture_output,
            text=text,
            input=input,
            env=effective_env,
        )

    def _is_excluded(self, rel_path: str) -> bool:
        """Check whether a relative POSIX path matches supervisor exclusions."""
        for excl in self.exclusions:
            if rel_path == excl or rel_path.startswith(f"{excl}/"):
                return True
        return False

    def check_nested_git(self) -> None:
        """Scan workspace for nested .git directories or gitfiles outside exclusions.

        Decided policy:
        - Nested .git directories inside supervisor-excluded dependency paths
          (such as `node_modules/` or `.venv/`) from package managers are ignored.
        - Any nested .git directory or gitfile anywhere inside tracked source
          directories raises NestedGitError to prevent gitlink file hiding.
        """
        if not self.workspace.exists():
            return
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            if root_path == self.workspace:
                continue
            rel_dir = root_path.relative_to(self.workspace).as_posix()
            # If inside an excluded dependency directory (e.g. node_modules, .venv), skip checking
            if self._is_excluded(rel_dir):
                continue
            if ".git" in dirs or ".git" in files:
                rel = root_path.relative_to(self.workspace)
                raise NestedGitError(
                    f"Nested git repository or gitfile detected at '{rel / '.git'}'. "
                    "Nested git structures hide files from tracking and are prohibited."
                )

    def reap_processes(self) -> list[int]:
        """Reap all managed and descendant processes before diff or restore."""
        if self.reaper:
            return self.reaper.reap_all()
        return []

    def _collect_workspace_files(self) -> list[Path]:
        """Collect all files in workspace outside supervisor exclusions."""
        collected: list[Path] = []
        if not self.workspace.exists():
            return collected

        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.workspace).as_posix() if root_path != self.workspace else ""
            if rel_root and self._is_excluded(rel_root):
                dirs.clear()  # Do not recurse into excluded directory
                continue
            # Filter dirs in-place to avoid descending into excluded subtrees
            dirs[:] = [
                d for d in dirs
                if not self._is_excluded(f"{rel_root}/{d}" if rel_root else d)
            ]
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                if not self._is_excluded(rel_f):
                    collected.append(root_path / f)
        return collected

    def create_turn_snapshot(self, turn_id: str) -> str:
        """Capture a complete working tree snapshot bypassing Git's filter pipeline.

        Uses hash-object --no-filters and update-index --add --cacheinfo to store
        raw bytes directly, neutralizing any workspace .gitattributes filters or eol rules.
        """
        self.check_nested_git()
        self._verify_store_config_integrity()

        temp_index = self.storage_root / f"idx_{turn_id}"
        snap_env = {**self.base_env, "GIT_INDEX_FILE": str(temp_index)}

        try:
            files_to_snap = self._collect_workspace_files()

            if not files_to_snap:
                # Empty workspace tree
                res = self._git_run(["write-tree"], env=snap_env)
                tree_sha = res.stdout.strip()
            else:
                # 1. Ingest raw file bytes into object store in batches using hash-object --no-filters
                batch_size = 50
                cacheinfo_args: list[str] = []

                for i in range(0, len(files_to_snap), batch_size):
                    chunk = files_to_snap[i : i + batch_size]
                    hash_cmd = [
                        "hash-object", "-w", "--no-filters",
                        *[str(p) for p in chunk],
                    ]
                    hash_res = self._git_run(hash_cmd, env=snap_env)
                    shas = [s.strip() for s in hash_res.stdout.splitlines() if s.strip()]
                    if len(shas) != len(chunk):
                        raise TurnSnapshotError(
                            f"hash-object returned {len(shas)} SHAs for {len(chunk)} files."
                        )

                    for p, sha in zip(chunk, shas):
                        rel_path = p.relative_to(self.workspace).as_posix()
                        # Mode: regular 100644 (or 100755 if executable on POSIX)
                        mode = "100644"
                        if sys.platform != "win32":
                            try:
                                if os.stat(p).st_mode & 0o111:
                                    mode = "100755"
                            except OSError:
                                pass
                        cacheinfo_args.extend(["--cacheinfo", f"{mode},{sha},{rel_path}"])

                # 2. Stage directly into temporary index
                update_cmd = ["update-index", "--add", *cacheinfo_args]
                self._git_run(update_cmd, env=snap_env)

                # 3. Write tree object
                tree_res = self._git_run(["write-tree"], env=snap_env)
                tree_sha = tree_res.stdout.strip()

            # 4. Commit tree directly
            commit_res = self._git_run(
                ["commit-tree", tree_sha, "-m", f"turn snapshot {turn_id}"],
                env=snap_env,
            )
            commit_sha = commit_res.stdout.strip()

            # 5. Record ref in external store and in supervisor memory
            ref_name = f"refs/stackmind/turn/{turn_id}"
            self._git_run(["update-ref", ref_name, commit_sha], env=snap_env)
            self.snapshot_shas[turn_id] = commit_sha
            return commit_sha

        finally:
            if temp_index.exists():
                try:
                    temp_index.unlink()
                except OSError:
                    pass

    def list_snapshot_files(self, snapshot_sha: str) -> dict[str, str]:
        """List all tracked file paths and blob SHAs in the snapshot tree."""
        self._verify_store_config_integrity()
        res = self._git_run(
            ["ls-tree", "-r", "-z", f"{snapshot_sha}^{{tree}}"],
            text=False,
        )
        # Parse null-delimited entries: <mode> <type> <sha>\t<path>\0
        raw_output = res.stdout
        files_map: dict[str, str] = {}
        if not raw_output:
            return files_map

        for entry in raw_output.split(b"\x00"):
            if not entry:
                continue
            try:
                meta, path_bytes = entry.split(b"\t", 1)
                rel_path = path_bytes.decode("utf-8", errors="replace")
                meta_parts = meta.split()
                sha = meta_parts[2].decode("ascii")
                files_map[rel_path] = sha
            except Exception:
                continue
        return files_map

    def restore_turn_snapshot(self, turn_id: str, expected_sha: str | None = None) -> None:
        """Safely restore the workspace to a snapshot without moving workspace HEAD.

        Preconditions:
        1. Process tree kill: reaps any active child or background processes.
        2. Config integrity: validates store configuration hash.
        3. Ref & SHA binding: confirms snapshot SHA matches supervisor-tracked state.
        4. Symlink neutralization: lstats and unlinks planted symlinks/junctions.
        5. Filter bypass restore: writes raw blob bytes directly to workspace files.
        6. Untracked cleanup: unlinks newly created untracked files and removes emptied dirs.
        7. Post-turn verification: checks for nested git repositories.
        """
        # 1. Kill process tree as a mandatory precondition
        self.reap_processes()

        # 2. Verify external store config integrity
        self._verify_store_config_integrity()

        # 3. Check ref in external store
        ref_name = f"refs/stackmind/turn/{turn_id}"
        res = self._git_run(
            ["rev-parse", "--verify", "--quiet", ref_name],
            check=False,
        )
        if res.returncode != 0:
            raise SnapshotIntegrityError(
                f"Snapshot integrity verification failed: ref '{ref_name}' not found."
            )
        actual_sha = res.stdout.strip()

        # Check against expected SHA (from memory or caller)
        target_expected = expected_sha or self.snapshot_shas.get(turn_id)
        if target_expected and actual_sha != target_expected:
            raise SnapshotIntegrityError(
                f"Snapshot integrity verification failed: expected {target_expected}, found {actual_sha}."
            )

        # 4. Verify object tree exists and is readable
        tree_check = self._git_run(
            ["cat-file", "-e", f"{actual_sha}^{{tree}}"],
            check=False,
        )
        if tree_check.returncode != 0:
            raise SnapshotIntegrityError(
                f"Snapshot integrity verification failed: commit/tree object for '{actual_sha}' is corrupt or missing."
            )

        snap_entries = self.list_snapshot_files(actual_sha)

        # 5. Neutralize planted symlinks & junctions before checkout to prevent traversal escapes
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            for f in files:
                fp = root_path / f
                try:
                    if os.path.islink(fp) or fp.is_symlink():
                        fp.unlink()
                except OSError:
                    pass
            for d in list(dirs):
                dp = root_path / d
                try:
                    if os.path.islink(dp) or dp.is_symlink():
                        try:
                            dp.unlink()
                        except OSError:
                            os.rmdir(dp)
                        dirs.remove(d)
                except OSError:
                    pass

        # 6. Stream raw blob bytes directly to workspace files (bypassing checkout-index & filters)
        for rel_path, blob_sha in snap_entries.items():
            target_path = self.workspace / rel_path
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Unlinking before writing wipes any attached NTFS Alternate Data Streams (ADS)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass

            # Read raw blob bytes from object store
            blob_res = self._git_run(
                ["cat-file", "blob", blob_sha],
                text=False,
                check=False,
            )
            if blob_res.returncode != 0:
                raise SnapshotIntegrityError(
                    f"Snapshot integrity verification failed: object '{blob_sha}' is corrupt or unreadable."
                )
            target_path.write_bytes(blob_res.stdout)

        # 7. Clean up any newly added untracked files not present in the snapshot
        snap_files_set = set(snap_entries.keys())
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.workspace).as_posix() if root_path != self.workspace else ""
            if rel_root and self._is_excluded(rel_root):
                continue
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                if self._is_excluded(rel_f):
                    continue
                if rel_f not in snap_files_set:
                    try:
                        (root_path / f).unlink()
                    except OSError:
                        pass

        # 8. Clean up emptied directories bottom-up
        for root, dirs, files in os.walk(self.workspace, topdown=False):
            root_path = Path(root)
            if root_path == self.workspace:
                continue
            rel_dir = root_path.relative_to(self.workspace).as_posix()
            if self._is_excluded(rel_dir):
                continue
            try:
                # If directory is now empty, remove it
                if not any(root_path.iterdir()):
                    os.rmdir(root_path)
            except OSError:
                pass

        # 9. Run post-turn nested .git check
        self.check_nested_git()

    def create_rescue_ref(self, run_id: str) -> str:
        """Create a rescue commit and ref under refs/stackmind/rescue/<run_id>."""
        commit_sha = self.create_turn_snapshot(f"rescue_{run_id}")
        ref_name = f"refs/stackmind/rescue/{run_id}"
        self._git_run(["update-ref", ref_name, commit_sha])
        return commit_sha
