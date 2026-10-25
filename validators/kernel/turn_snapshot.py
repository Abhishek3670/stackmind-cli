"""
Supervisor-owned per-turn snapshot and safe non-HEAD revert engine (P0-2).

Stores snapshots and rescue refs in a dedicated supervisor-managed bare git repository
outside the workspace boundary, protecting them against tampering, pruning, or ref deletion
by workspace child processes.

Host Substrate:
- Primary host: Windows 11 (tested, empirical Job Object containment).
- Windows configuration: core.filemode=false, core.symlinks=false, core.autocrlf=false.
- Reparse points & junctions on Windows: detected and unlinked via os.lstat/os.rmdir
  without recursive traversal before restore and cleanup.
- POSIX portable mode: UNVERIFIED (marked per project policy).

Filter Pipeline Bypass:
- Completely bypasses Git's working-tree filter pipeline (.gitattributes eol, ident, filters):
  * Snapshot: uses `hash-object -w --no-filters` and `update-index --add --cacheinfo` to
    stage raw file bytes directly into the object store without filter smudging or eol translation.
  * Restore: parses `ls-tree -r -z` and streams raw bytes from `cat-file blob <sha>` directly
    to workspace files, bypassing checkout-index and filter pipelines entirely.

Store Integrity & Defense-in-Depth:
- Computes comprehensive metadata digest across the external store (all files except object pools
  under objects/??/ and objects/pack/); verifies objects only grow monotonically.
- Neutralizes hooks, fsmonitor, and alternates tampering (e.g. objects/info/alternates or hooks/*).
- Injects `-c core.fsmonitor=false -c core.hooksPath="" -c core.excludesFile=""` and
  `GIT_CONFIG_GLOBAL/SYSTEM=null` on every invocation to neutralize config-injection attacks.
- Enforces Job Object process-tree reaping (KILL_ON_JOB_CLOSE, no breakaway) prior to diff and restore.
- Unlinks files before overwrite on restore to destroy NTFS Alternate Data Streams (ADS),
  and scans with FindFirstStreamW to detect and eradicate any residual ADS on unchanged files.
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


def is_reparse_point(path: Path | str) -> bool:
    """Detect whether a path is a Windows directory junction, symlink, or reparse point."""
    try:
        st = os.lstat(path)
        if hasattr(st, "st_file_attributes"):
            return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return os.path.islink(path)
    except OSError:
        return False


# Default exclusions strictly anchored to workspace root.
# Note: __pycache__ is omitted from exclusions so that any bytecode planted in
# subdirectories is captured and monitored, while PYTHONDONTWRITEBYTECODE=1 prevents noise.
DEFAULT_SUPERVISOR_EXCLUSIONS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    ".pytest_cache",
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
        self.baseline_store_hash: str | None = None
        self.known_objects: set[str] = set()
        self.ads_violations: list[str] = []

        self.null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
        self.base_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": self.null_dev,
            "GIT_CONFIG_SYSTEM": self.null_dev,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG": self.null_dev,
            "PYTHONDONTWRITEBYTECODE": "1",
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
                    env={k: v for k, v in self.base_env.items() if k != "GIT_CONFIG"},
                )

        self.baseline_store_hash = self._compute_store_metadata_hash()
        self.known_objects = self._scan_store_objects()

    def _is_object_pool_path(self, rel_path: str) -> bool:
        """Check whether a path points to the loose object or pack pool."""
        parts = rel_path.split("/")
        if len(parts) >= 2 and parts[0] == "objects":
            # Loose objects: objects/xx/yyyy...
            if len(parts[1]) == 2 and all(c in "0123456789abcdefABCDEF" for c in parts[1]):
                return True
            # Pack files: objects/pack/pack-...
            if parts[1] == "pack":
                return True
        return False

    def _compute_store_metadata_hash(self) -> str:
        """Compute digest over all files in storage_root EXCEPT actual object pools."""
        hasher = hashlib.sha256()
        if not self.storage_root.exists():
            return ""

        meta_entries: list[tuple[str, bytes]] = []
        for root, dirs, files in os.walk(self.storage_root):
            root_path = Path(root)
            for f in sorted(files):
                # Skip temporary index files generated per-turn
                if f.startswith("idx_"):
                    continue
                fp = root_path / f
                rel = fp.relative_to(self.storage_root).as_posix()
                # Exclude object pools (objects/xx/* and objects/pack/*)
                # Note: objects/info/alternates IS hashed as metadata!
                if self._is_object_pool_path(rel):
                    continue
                try:
                    meta_entries.append((rel, fp.read_bytes()))
                except OSError:
                    pass

        # Sort deterministically by relative path
        meta_entries.sort(key=lambda x: x[0])
        for rel_name, content in meta_entries:
            hasher.update(rel_name.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(hashlib.sha256(content).digest())

        return hasher.hexdigest()

    def _scan_store_objects(self) -> set[str]:
        """Collect relative paths of all loose objects and pack files in the store."""
        objects_dir = self.storage_root / "objects"
        objs: set[str] = set()
        if not objects_dir.exists():
            return objs
        for root, dirs, files in os.walk(objects_dir):
            root_path = Path(root)
            for f in files:
                rel = (root_path / f).relative_to(self.storage_root).as_posix()
                if self._is_object_pool_path(rel):
                    objs.add(rel)
        return objs

    def _verify_store_integrity(self) -> None:
        """Verify external store metadata and monotonic growth of objects."""
        if not self.storage_root.exists():
            raise SnapshotIntegrityError(
                f"External snapshot store corrupted: root '{self.storage_root}' is missing or deleted."
            )
        config_path = self.storage_root / "config"
        if not config_path.exists():
            raise SnapshotIntegrityError(
                f"External snapshot store corrupted: config file '{config_path}' is missing or deleted."
            )

        # 1. Verify store metadata (config, hooks, HEAD, alternates, description)
        curr_meta_hash = self._compute_store_metadata_hash()
        if self.baseline_store_hash and curr_meta_hash != self.baseline_store_hash:
            raise SnapshotIntegrityError(
                f"External snapshot store tampering detected: store metadata or unauthorized file added "
                f"(e.g. hooks or alternates)! Expected {self.baseline_store_hash}, found {curr_meta_hash}."
            )

        # 2. Verify objects only grew monotonically (none were deleted or truncated)
        current_objs = self._scan_store_objects()
        missing_objs = self.known_objects - current_objs
        if missing_objs:
            raise SnapshotIntegrityError(
                f"External snapshot store corrupted: {len(missing_objs)} objects missing or deleted from object pool!"
            )
        # Update known objects with newly added objects
        self.known_objects.update(current_objs)

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
        self._verify_store_integrity()
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
        """Check whether a relative POSIX path matches workspace-root-anchored exclusions."""
        for excl in self.exclusions:
            # Strictly anchored to workspace root: rel_path is either the root folder itself or inside it
            if rel_path == excl or rel_path.startswith(f"{excl}/"):
                return True
        return False

    def check_nested_git(self) -> None:
        """Scan workspace for nested .git directories or gitfiles outside exclusions.

        Decided policy:
        - Root-anchored exclusions (e.g. `node_modules/` or `.venv/` at workspace root) are ignored.
        - Any nested .git directory or gitfile anywhere else inside tracked source directories
          (e.g. `src/sub_repo/.git` or `src/node_modules/.git`) raises NestedGitError.
        """
        if not self.workspace.exists():
            return
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            if root_path == self.workspace:
                continue
            rel_dir = root_path.relative_to(self.workspace).as_posix()
            if self._is_excluded(rel_dir):
                dirs.clear()
                continue
            if ".git" in dirs or ".git" in files:
                rel = root_path.relative_to(self.workspace)
                raise NestedGitError(
                    f"Nested git repository or gitfile detected at '{rel / '.git'}'. "
                    "Nested git structures hide files from tracking and are prohibited."
                )

    def check_post_turn(self, turn_id: str | None = None) -> None:
        """Post-turn gate verification.

        If a nested git repository was planted during the turn, reaps all processes,
        restores the workspace from the turn snapshot, and raises NestedGitError.
        """
        try:
            self.check_nested_git()
        except NestedGitError:
            self.reap_processes()
            if turn_id:
                try:
                    self.restore_turn_snapshot(turn_id)
                except Exception:
                    pass
            raise

    def reap_processes(self) -> list[int]:
        """Reap all managed and descendant processes before diff or restore."""
        if self.reaper:
            return self.reaper.reap_all()
        return []

    def _collect_workspace_files(self) -> list[Path]:
        """Collect all files in workspace outside root-anchored exclusions."""
        collected: list[Path] = []
        if not self.workspace.exists():
            return collected

        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            rel_root = root_path.relative_to(self.workspace).as_posix() if root_path != self.workspace else ""
            if rel_root and self._is_excluded(rel_root):
                dirs.clear()
                continue
            # Filter dirs in-place to avoid descending into root-anchored excluded subtrees
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
        """Capture a complete working tree snapshot bypassing Git's filter pipeline."""
        self.check_nested_git()
        self._verify_store_integrity()

        temp_index = self.storage_root / f"idx_{turn_id}"
        snap_env = {**self.base_env, "GIT_INDEX_FILE": str(temp_index)}

        try:
            files_to_snap = self._collect_workspace_files()

            if not files_to_snap:
                res = self._git_run(["write-tree"], env=snap_env)
                tree_sha = res.stdout.strip()
            else:
                # Hash all files in one invocation via --stdin-paths
                stdin_paths = "\n".join(str(p) for p in files_to_snap) + "\n"
                hash_res = self._git_run(
                    ["hash-object", "-w", "--no-filters", "--stdin-paths"],
                    input=stdin_paths,
                    env=snap_env,
                )
                shas = [s.strip() for s in hash_res.stdout.splitlines() if s.strip()]
                if len(shas) != len(files_to_snap):
                    raise TurnSnapshotError(
                        f"hash-object returned {len(shas)} SHAs for {len(files_to_snap)} files."
                    )

                index_lines: list[str] = []
                for p, sha in zip(files_to_snap, shas):
                    rel_path = p.relative_to(self.workspace).as_posix()
                    mode = "100644"
                    if sys.platform != "win32":
                        try:
                            if os.stat(p).st_mode & 0o111:
                                mode = "100755"
                        except OSError:
                            pass
                    index_lines.append(f"{mode} {sha} 0\t{rel_path}")

                # Populate index in one invocation via --index-info -z
                index_input = "\x00".join(index_lines) + "\x00"
                self._git_run(
                    ["update-index", "-z", "--index-info"],
                    input=index_input,
                    env=snap_env,
                )

                tree_res = self._git_run(["write-tree"], env=snap_env)
                tree_sha = tree_res.stdout.strip()

            commit_res = self._git_run(
                ["commit-tree", tree_sha, "-m", f"turn snapshot {turn_id}"],
                env=snap_env,
            )
            commit_sha = commit_res.stdout.strip()

            ref_name = f"refs/stackmind/turn/{turn_id}"
            self._git_run(["update-ref", ref_name, commit_sha], env=snap_env)
            self.snapshot_shas[turn_id] = commit_sha
            # Update known objects and baseline metadata after commit
            self.known_objects.update(self._scan_store_objects())
            self.baseline_store_hash = self._compute_store_metadata_hash()
            return commit_sha

        finally:
            if temp_index.exists():
                try:
                    temp_index.unlink()
                except OSError:
                    pass

    def list_snapshot_files(self, snapshot_sha: str) -> dict[str, str]:
        """List all tracked file paths and blob SHAs in the snapshot tree."""
        self._verify_store_integrity()
        res = self._git_run(
            ["ls-tree", "-r", "-z", f"{snapshot_sha}^{{tree}}"],
            text=False,
        )
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

    def _enumerate_and_wipe_ads(self, path: Path) -> list[str]:
        """Enumerate, record violation, and eliminate non-default NTFS Alternate Data Streams on files/dirs."""
        if sys.platform != "win32" or not path.exists():
            return []

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class WIN32_FIND_STREAM_DATA(ctypes.Structure):
            _fields_ = [
                ("StreamSize", ctypes.c_int64),
                ("cStreamName", wintypes.WCHAR * 296),
            ]

        kernel32.FindFirstStreamW.restype = wintypes.HANDLE
        kernel32.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.FindNextStreamW.restype = wintypes.BOOL
        kernel32.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.FindClose.argtypes = [wintypes.HANDLE]

        find_data = WIN32_FIND_STREAM_DATA()
        h = kernel32.FindFirstStreamW(str(path), 0, ctypes.byref(find_data), 0)
        if h == wintypes.HANDLE(-1).value or not h:
            return []

        found_streams: list[str] = []
        try:
            while True:
                name = find_data.cStreamName
                if name and name != "::$DATA":
                    found_streams.append(name)
                if not kernel32.FindNextStreamW(h, ctypes.byref(find_data)):
                    break
        finally:
            kernel32.FindClose(h)

        if found_streams:
            rel = path.relative_to(self.workspace).as_posix() if path != self.workspace else "."
            for s in found_streams:
                self.ads_violations.append(f"{rel}:{s}")
            # Eradicate stream on regular files by re-writing base file
            if not path.is_dir():
                try:
                    content = path.read_bytes()
                    path.unlink()
                    path.write_bytes(content)
                except OSError:
                    pass
        return found_streams

    def restore_turn_snapshot(self, turn_id: str, expected_sha: str | None = None) -> None:
        """Safely restore the workspace to a snapshot without moving workspace HEAD."""
        # 1. Kill process tree as a mandatory precondition
        self.reap_processes()

        # 2. Verify external store integrity (config, hooks, alternates, objects)
        self._verify_store_integrity()

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

        # 5. Neutralize planted symlinks & junctions before restore without recursive descent
        for root, dirs, files in os.walk(self.workspace):
            root_path = Path(root)
            for f in files:
                fp = root_path / f
                if is_reparse_point(fp):
                    try:
                        fp.unlink()
                    except OSError:
                        pass
            for d in list(dirs):
                dp = root_path / d
                if is_reparse_point(dp):
                    dirs.remove(d)  # Never recurse through planted junction or symlink
                    try:
                        os.rmdir(dp)
                    except OSError:
                        try:
                            dp.unlink()
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
            # Neutralize directory links during walk and do not descend
            for d in list(dirs):
                dp = root_path / d
                if is_reparse_point(dp):
                    dirs.remove(d)
                    try:
                        os.rmdir(dp)
                    except OSError:
                        try:
                            dp.unlink()
                        except OSError:
                            pass
                else:
                    self._enumerate_and_wipe_ads(dp)
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                if self._is_excluded(rel_f):
                    continue
                if rel_f not in snap_files_set:
                    try:
                        (root_path / f).unlink()
                    except OSError:
                        pass
                else:
                    # Enumerate and wipe non-default ADS on unchanged files
                    self._enumerate_and_wipe_ads(root_path / f)

        # 8. Clean up emptied directories bottom-up
        for root, dirs, files in os.walk(self.workspace, topdown=False):
            root_path = Path(root)
            if root_path == self.workspace:
                continue
            rel_dir = root_path.relative_to(self.workspace).as_posix()
            if self._is_excluded(rel_dir):
                continue
            if is_reparse_point(root_path):
                try:
                    os.rmdir(root_path)
                except OSError:
                    pass
                continue
            try:
                if not any(root_path.iterdir()):
                    os.rmdir(root_path)
            except OSError:
                pass

        # 9. Run post-turn nested .git check (ensures processes reaped before raising)
        try:
            self.check_nested_git()
        except NestedGitError:
            self.reap_processes()
            raise

    def create_rescue_ref(self, run_id: str) -> str:
        """Create a rescue commit and ref under refs/stackmind/rescue/<run_id>."""
        commit_sha = self.create_turn_snapshot(f"rescue_{run_id}")
        ref_name = f"refs/stackmind/rescue/{run_id}"
        self._git_run(["update-ref", ref_name, commit_sha])
        self.known_objects.update(self._scan_store_objects())
        self.baseline_store_hash = self._compute_store_metadata_hash()
        return commit_sha
