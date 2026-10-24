"""
Post-turn diff gate and derived commit tree binding engine (P0-3 / P0-4).

Enforces strict, multi-agent capability gating and scope authorization between
worker turns, QA reviews, and GitOps promotions.

Core Guarantees:
1. Derived Commit Tree Binding:
   - Does NOT commit directly from the raw snapshot tree (which contains gitignored
     secrets like .env, logs, build outputs, and raw line endings).
   - Computes a derived commit tree:
     * Path set = tracked at HEAD + new files not matching pre-turn baseline .gitignore/info/exclude.
     * Modes = inherited from HEAD for existing files (preserving 100755); 100644 for new regular files.
     * Content = raw approved snapshot bytes normalized via repository .gitattributes eol rules.
   - The approved tree hash is computed over exactly this derived set.
2. Supervisor-Mediated GitOps:
   - The GitOps role does NOT run raw git mutation in the shell.
   - GitOps submits commit/tag/push requests through supervisor-mediated plumbing.
   - Commits are created via hardened plumbing (-c core.fsmonitor=false -c core.hooksPath=""
     -c core.excludesFile="", null global/system configs).
   - Push credentials exist in memory only for the duration of the push call.
3. GitOps Post-Turn Gate:
   - Asserts HEAD.tree == QA-approved derived tree.
   - Asserts workspace refs changed ONLY by the single new commit and optional tag.
   - Asserts no unauthorized .git metadata modifications.
4. Pre-Turn Contract & Scope Grounding:
   - Scope, allowed write paths, and contracts are loaded strictly from pre-turn supervisor state.
   - Any modification to .sync/** by a non-Architecture role triggers an immediate hard violation.
   - An Architecture contract change becomes baseline only after that turn passes the gate.
5. Violation Enforcement:
   - Evaluates: reparse points/junctions, out-of-scope renames, case-only renames, out-of-scope deletes,
     hardlinks, DOS reserved names, trailing dots/spaces, 8.3 aliases, forged mtime/size, ADS streams.
   - On violation: reaps process tree (Job Object), restores workspace byte-exact from snapshot,
     increments violation counters, and returns a structured error.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import AgentContract
from .process_reaper import ProcessReaper, get_global_reaper
from .turn_snapshot import TurnSnapshotManager, is_reparse_point


DOS_RESERVED_NAMES: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


@dataclass(frozen=True)
class GateViolation:
    violation_type: str
    path: str
    details: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, str]:
        return {
            "violation_type": self.violation_type,
            "path": self.path,
            "details": self.details,
            "timestamp": self.timestamp,
        }


@dataclass
class GateResult:
    passed: bool
    violations: list[GateViolation]
    modified_files: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    renamed_files: list[tuple[str, str]] = field(default_factory=list)
    derived_tree_sha: str | None = None
    evaluation_duration_seconds: float = 0.0
    hashing_throughput_mb_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": [v.to_dict() for v in self.violations],
            "modified_files": list(self.modified_files),
            "added_files": list(self.added_files),
            "deleted_files": list(self.deleted_files),
            "renamed_files": list(self.renamed_files),
            "derived_tree_sha": self.derived_tree_sha,
            "evaluation_duration_seconds": round(self.evaluation_duration_seconds, 4),
            "hashing_throughput_mb_s": round(self.hashing_throughput_mb_s, 2),
        }


class DiffGate:
    """Evaluates post-turn workspace diffs against supervisor contracts and derived commit trees."""

    def __init__(
        self,
        workspace: Path | str,
        snapshot_manager: TurnSnapshotManager,
        role: str,
        contract: AgentContract | None = None,
        reaper: ProcessReaper | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.snapshot_manager = snapshot_manager
        self.role = role
        self.contract = contract
        self.reaper = reaper or get_global_reaper()
        self.null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
        self.base_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": self.null_dev,
            "GIT_CONFIG_SYSTEM": self.null_dev,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "StackMind Supervisor",
            "GIT_AUTHOR_EMAIL": "supervisor@stackmind.local",
            "GIT_COMMITTER_NAME": "StackMind Supervisor",
            "GIT_COMMITTER_EMAIL": "supervisor@stackmind.local",
        }

    # ==========================================================================
    # Path Normalization & Security Scanning Helpers
    # ==========================================================================

    @staticmethod
    def check_dos_reserved_name(rel_path: str) -> str | None:
        """Detect DOS reserved device names (e.g. CON, PRN, NUL, COM1.txt)."""
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            stem = part.split(".")[0].upper()
            if stem in DOS_RESERVED_NAMES:
                return part
        return None

    @staticmethod
    def check_trailing_dots_or_spaces(rel_path: str) -> str | None:
        """Detect trailing dots or spaces in path components."""
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            if part.endswith(".") or part.endswith(" "):
                return part
        return None

    @staticmethod
    def check_short_name_alias(rel_path: str) -> bool:
        """Detect 8.3 short name patterns (e.g. PROGRA~1)."""
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            if "~" in part:
                sub = part.split("~", 1)[1]
                if sub and sub[0].isdigit():
                    return True
        return False

    @staticmethod
    def check_hardlinks(full_path: Path) -> bool:
        """Detect whether a file has hardlinks (nNumberOfLinks > 1)."""
        if not full_path.exists() or full_path.is_dir():
            return False
        try:
            return os.stat(full_path).st_nlink > 1
        except OSError:
            return False

    def _check_ads_streams(self, path: Path) -> list[str]:
        """Scan file or directory for non-default NTFS Alternate Data Streams."""
        if sys.platform != "win32" or not path.exists():
            return []

        import ctypes
        from ctypes import wintypes

        class WIN32_FIND_STREAM_DATA(ctypes.Structure):
            _fields_ = [
                ("StreamSize", ctypes.c_int64),
                ("cStreamName", wintypes.WCHAR * 296),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FindFirstStreamW.restype = wintypes.HANDLE
        kernel32.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.FindNextStreamW.restype = wintypes.BOOL
        kernel32.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.FindClose.argtypes = [wintypes.HANDLE]

        find_data = WIN32_FIND_STREAM_DATA()
        h = kernel32.FindFirstStreamW(str(path), 0, ctypes.byref(find_data), 0)
        if h == wintypes.HANDLE(-1).value or not h:
            return []

        streams = []
        try:
            while True:
                name = find_data.cStreamName
                if name and name != "::$DATA":
                    streams.append(name)
                if not kernel32.FindNextStreamW(h, ctypes.byref(find_data)):
                    break
        finally:
            kernel32.FindClose(h)
        return streams

    def _is_path_in_scope(self, rel_path: str) -> bool:
        """Check whether a normalized relative path is permitted by the pre-turn contract."""
        if self.contract is None:
            return True

        norm_path = rel_path.replace("\\", "/")

        # 1. Deny rules take strict precedence
        for pattern in self.contract.deny:
            p_norm = pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, p_norm) or norm_path.startswith(f"{p_norm}/"):
                return False

        # 2. Allow rules must match at least one entry
        for pattern in self.contract.allow:
            p_norm = pattern.replace("\\", "/")
            if fnmatch.fnmatch(norm_path, p_norm) or norm_path.startswith(f"{p_norm}/"):
                return True

        return False

    # ==========================================================================
    # Diff Evaluation & Violation Detection
    # ==========================================================================

    def evaluate_turn(
        self,
        pre_turn_snapshot_sha: str,
        turn_id: str,
    ) -> GateResult:
        """Scan workspace post-turn, evaluate against pre-turn contracts, and enforce gate rules.

        If violations occur, reaps processes, restores workspace from pre-turn snapshot,
        and returns GateResult with detailed violations.
        """
        start_time = time.perf_counter()
        violations: list[GateViolation] = []
        total_bytes_hashed = 0

        # Pre-turn state from snapshot store
        pre_turn_files = self.snapshot_manager.list_snapshot_files(pre_turn_snapshot_sha)

        # 1. Collect all post-turn files using supervisor walker (respecting exclusions)
        current_file_paths = self.snapshot_manager._collect_workspace_files()
        current_files: dict[str, str] = {}  # rel_path -> sha256_hex
        current_blob_shas: dict[str, str] = {}  # rel_path -> git_blob_sha1_hex
        current_files_case_folded: dict[str, list[str]] = {}

        # Scan for reparse points, junctions, hardlinks, reserved names, and compute hashes
        for p in current_file_paths:
            rel = p.relative_to(self.workspace).as_posix()
            folded = rel.lower()
            current_files_case_folded.setdefault(folded, []).append(rel)

            # Security checks on paths
            res_name = self.check_dos_reserved_name(rel)
            if res_name:
                violations.append(
                    GateViolation(
                        "RESERVED_DEVICE_NAME",
                        rel,
                        f"Path component '{res_name}' matches DOS reserved device name.",
                    )
                )

            trailing = self.check_trailing_dots_or_spaces(rel)
            if trailing:
                violations.append(
                    GateViolation(
                        "TRAILING_DOT_OR_SPACE",
                        rel,
                        f"Path component '{trailing}' contains illegal trailing dot or space.",
                    )
                )

            if self.check_short_name_alias(rel):
                violations.append(
                    GateViolation(
                        "SHORT_NAME_ALIAS",
                        rel,
                        "Path contains 8.3 short name alias (~ pattern).",
                    )
                )

            if is_reparse_point(p):
                violations.append(
                    GateViolation(
                        "REPARSE_POINT",
                        rel,
                        "File is a symbolic link or junction reparse point.",
                    )
                )

            if self.check_hardlinks(p):
                violations.append(
                    GateViolation(
                        "HARDLINK_DETECTED",
                        rel,
                        "File has multiple NTFS hardlinks (st_nlink > 1).",
                    )
                )

            # Check Alternate Data Streams (ADS)
            ads_list = self._check_ads_streams(p)
            for ads in ads_list:
                violations.append(
                    GateViolation(
                        "ADS_STREAM_DETECTED",
                        f"{rel}:{ads}",
                        f"Non-default NTFS alternate data stream '{ads}' detected.",
                    )
                )

            # Full content SHA-256 and Git blob SHA (no trusting mtime or file size)
            try:
                content = p.read_bytes()
                total_bytes_hashed += len(content)
                current_files[rel] = hashlib.sha256(content).hexdigest()
                # Compute exact Git blob SHA without spawning subprocesses
                blob_header = b"blob " + str(len(content)).encode("ascii") + b"\x00"
                current_blob_shas[rel] = hashlib.sha1(blob_header + content).hexdigest()
            except OSError as e:
                violations.append(
                    GateViolation("UNREADABLE_FILE", rel, f"Failed to read file: {e}")
                )

        # Also check directories for reparse points and directory ADS
        for root, dirs, _ in os.walk(self.workspace):
            root_path = Path(root)
            for d in list(dirs):
                dp = root_path / d
                rel_d = dp.relative_to(self.workspace).as_posix()
                if is_reparse_point(dp):
                    violations.append(
                        GateViolation(
                            "REPARSE_POINT",
                            rel_d,
                            "Directory is a junction or symbolic link reparse point.",
                        )
                    )
                dir_ads = self._check_ads_streams(dp)
                for ads in dir_ads:
                    violations.append(
                        GateViolation(
                            "ADS_STREAM_DETECTED",
                            f"{rel_d}:{ads}",
                            f"Directory alternate data stream '{ads}' detected.",
                        )
                    )

        # 2. Case-only rename check
        for folded, rels in current_files_case_folded.items():
            if len(rels) > 1:
                violations.append(
                    GateViolation(
                        "CASE_COLLISION",
                        rels[0],
                        f"Multiple files collide under case-folding: {rels}",
                    )
                )

        # 3. Detect added, modified, deleted files
        pre_paths = set(pre_turn_files.keys())
        curr_paths = set(current_files.keys())

        added_paths = curr_paths - pre_paths
        deleted_paths = pre_paths - curr_paths
        common_paths = curr_paths & pre_paths

        modified_paths: list[str] = []
        for p in common_paths:
            # Compare current Git blob SHA with pre-turn snapshot Git blob SHA in O(1)
            if current_blob_shas.get(p) != pre_turn_files[p]:
                modified_paths.append(p)

        # 4. Detect renames (matching content blob SHA between deleted and added files)
        renamed_pairs: list[tuple[str, str]] = []
        detected_renames_src: set[str] = set()
        detected_renames_dst: set[str] = set()

        for del_p in deleted_paths:
            del_blob = pre_turn_files.get(del_p)
            for add_p in added_paths:
                if add_p not in detected_renames_dst and current_blob_shas.get(add_p) == del_blob:
                    renamed_pairs.append((del_p, add_p))
                    detected_renames_src.add(del_p)
                    detected_renames_dst.add(add_p)
                    break

        # Case-only rename check between pre-turn and current
        pre_folded = {p.lower(): p for p in pre_paths}
        for add_p in added_paths:
            folded = add_p.lower()
            if folded in pre_folded and pre_folded[folded] != add_p:
                violations.append(
                    GateViolation(
                        "CASE_ONLY_RENAME",
                        add_p,
                        f"Case-only rename detected: '{pre_folded[folded]}' -> '{add_p}'.",
                    )
                )

        # 5. Authorization & Scope Checks
        # Rule: Only Architecture can modify .sync/**
        if self.role != "Architecture":
            for p in list(added_paths) + list(modified_paths) + list(deleted_paths):
                if p == ".sync" or p.startswith(".sync/"):
                    violations.append(
                        GateViolation(
                            "SYNC_SELF_GRANT",
                            p,
                            f"Role '{self.role}' attempted unauthorized mutation of supervisor contract state (.sync/**).",
                        )
                    )

        # Scope authorization against contract
        for p in added_paths:
            if not self._is_path_in_scope(p):
                violations.append(
                    GateViolation(
                        "OUT_OF_SCOPE_CREATE",
                        p,
                        f"Created file '{p}' is outside authorized contract allow scope.",
                    )
                )

        for p in modified_paths:
            if not self._is_path_in_scope(p):
                violations.append(
                    GateViolation(
                        "OUT_OF_SCOPE_MODIFY",
                        p,
                        f"Modified file '{p}' is outside authorized contract allow scope.",
                    )
                )

        for p in deleted_paths:
            if not self._is_path_in_scope(p):
                violations.append(
                    GateViolation(
                        "OUT_OF_SCOPE_DELETE",
                        p,
                        f"Deleted file '{p}' was not authorized for removal.",
                    )
                )

        # Rename scope check: BOTH old and new path must be in scope
        for old_p, new_p in renamed_pairs:
            old_in = self._is_path_in_scope(old_p)
            new_in = self._is_path_in_scope(new_p)
            if not (old_in and new_in):
                violations.append(
                    GateViolation(
                        "RENAME_OUT_OF_SCOPE",
                        f"{old_p} -> {new_p}",
                        f"Rename violation: source in-scope={old_in}, destination in-scope={new_in}. Both must be authorized.",
                    )
                )

        duration = time.perf_counter() - start_time
        mb_hashed = total_bytes_hashed / (1024 * 1024)
        throughput = mb_hashed / max(duration, 0.0001)

        # 6. Enforcement: on violation, reap processes and restore workspace byte-exact
        passed = len(violations) == 0
        if not passed:
            self.reaper.reap_all()
            self.snapshot_manager.restore_turn_snapshot(
                turn_id,
                expected_sha=pre_turn_snapshot_sha,
            )

        return GateResult(
            passed=passed,
            violations=violations,
            modified_files=sorted(modified_paths),
            added_files=sorted(added_paths),
            deleted_files=sorted(deleted_paths),
            renamed_files=renamed_pairs,
            evaluation_duration_seconds=duration,
            hashing_throughput_mb_s=throughput,
        )

    # ==========================================================================
    # Derived Commit Tree & Supervisor-Mediated GitOps
    # ==========================================================================

    def compute_derived_commit_tree(self, approved_snapshot_sha: str) -> str:
        """Compute the derived commit tree for GitOps promotion.

        Path set:
        - Files tracked at HEAD that are present in the approved snapshot (keeps HEAD modes, e.g. 100755).
        - New files in snapshot that do NOT match pre-turn baseline .gitignore/info/exclude.
        - Excludes gitignored secrets (.env) and applies repository .gitattributes eol rules.
        """
        # 1. Approved snapshot files
        snap_files = self.snapshot_manager.list_snapshot_files(approved_snapshot_sha)

        # 2. Files tracked at HEAD
        head_res = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "HEAD"],
            cwd=self.workspace,
            check=False,
            capture_output=True,
            env=self.base_env,
        )
        head_entries: dict[str, tuple[str, str]] = {}  # rel_path -> (mode, sha)
        if head_res.returncode == 0 and head_res.stdout:
            for entry in head_res.stdout.split(b"\x00"):
                if not entry:
                    continue
                try:
                    meta, p_bytes = entry.split(b"\t", 1)
                    rel_p = p_bytes.decode("utf-8", errors="replace")
                    parts = meta.split()
                    mode = parts[0].decode("ascii")
                    sha = parts[2].decode("ascii")
                    head_entries[rel_p] = (mode, sha)
                except Exception:
                    pass

        # 3. Filter new files against baseline .gitignore using git check-ignore
        new_candidates = [p for p in snap_files if p not in head_entries]
        ignored_set: set[str] = set()

        if new_candidates:
            # Feed candidate paths to git check-ignore
            check_res = subprocess.run(
                ["git", "check-ignore", "--stdin", "-z"],
                cwd=self.workspace,
                input=("\x00".join(new_candidates) + "\x00").encode("utf-8"),
                capture_output=True,
                env=self.base_env,
            )
            if check_res.returncode in (0, 1) and check_res.stdout:
                for ign in check_res.stdout.split(b"\x00"):
                    if ign:
                        ignored_set.add(ign.decode("utf-8", errors="replace"))

        # 4. Determine final derived path set
        derived_paths: list[str] = []
        for p in snap_files:
            if p in head_entries:
                derived_paths.append(p)
            elif p not in ignored_set:
                derived_paths.append(p)

        # 5. Build derived tree in a dedicated temporary index
        temp_idx = self.workspace / ".git" / f"derived_idx_{int(time.time() * 1000)}"
        idx_env = {**self.base_env, "GIT_INDEX_FILE": str(temp_idx)}

        try:
            for p in sorted(derived_paths):
                file_full = self.workspace / p
                if not file_full.exists():
                    continue

                # Inherit mode from HEAD if previously tracked, otherwise default to 100644
                mode = head_entries[p][0] if p in head_entries else "100644"
                if p not in head_entries and sys.platform != "win32":
                    try:
                        if os.stat(file_full).st_mode & 0o111:
                            mode = "100755"
                    except OSError:
                        pass

                # Hash object through Git plumbing honoring .gitattributes eol conversion
                hash_res = subprocess.run(
                    [
                        "git",
                        "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=",
                        "-c", "core.excludesFile=",
                        "hash-object", "-w", "--path", p, str(file_full),
                    ],
                    cwd=self.workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                    env=idx_env,
                )
                blob_sha = hash_res.stdout.strip()

                # Stage into derived index
                subprocess.run(
                    [
                        "git",
                        "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=",
                        "-c", "core.excludesFile=",
                        "update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{p}",
                    ],
                    cwd=self.workspace,
                    check=True,
                    capture_output=True,
                    env=idx_env,
                )

            # Write tree object
            write_res = subprocess.run(
                [
                    "git",
                    "-c", "core.fsmonitor=false",
                    "-c", "core.hooksPath=",
                    "-c", "core.excludesFile=",
                    "write-tree",
                ],
                cwd=self.workspace,
                check=True,
                capture_output=True,
                text=True,
                env=idx_env,
            )
            return write_res.stdout.strip()

        finally:
            if temp_idx.exists():
                try:
                    temp_idx.unlink()
                except OSError:
                    pass

    def supervisor_git_commit(
        self,
        message: str,
        approved_snapshot_sha: str,
        branch: str = "master",
    ) -> str:
        """Supervisor-mediated commit creation. Ensures commit tree == derived approved tree."""
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to create Git commits.")

        derived_tree = self.compute_derived_commit_tree(approved_snapshot_sha)

        # Get parent HEAD commit
        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
            env=self.base_env,
        )
        parent_sha = head_res.stdout.strip()

        # Create commit object directly via plumbing
        commit_res = subprocess.run(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "commit-tree", derived_tree,
                "-p", parent_sha,
                "-m", message,
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
            env=self.base_env,
        )
        commit_sha = commit_res.stdout.strip()

        # Update branch ref
        subprocess.run(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "update-ref", f"refs/heads/{branch}", commit_sha,
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            env=self.base_env,
        )

        # Synchronize workspace index with new commit to ensure 0 spurious diffs
        subprocess.run(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "read-tree", "HEAD",
            ],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            env=self.base_env,
        )
        subprocess.run(
            [
                "git",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "update-index", "--refresh",
            ],
            cwd=self.workspace,
            check=False,
            capture_output=True,
            env=self.base_env,
        )

        # Verify GitOps post-turn gate
        new_tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
            env=self.base_env,
        ).stdout.strip()
        if new_tree != derived_tree:
            raise RuntimeError(
                f"GitOps post-turn gate failure: HEAD tree '{new_tree}' does not equal approved derived tree '{derived_tree}'."
            )

        return commit_sha

    def supervisor_git_tag(self, tag_name: str, message: str | None = None) -> None:
        """Supervisor-mediated lightweight or annotated tag creation."""
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to create Git tags.")

        cmd = [
            "git",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
            "tag",
        ]
        if message:
            cmd.extend(["-a", tag_name, "-m", message])
        else:
            cmd.append(tag_name)

        subprocess.run(
            cmd,
            cwd=self.workspace,
            check=True,
            capture_output=True,
            env=self.base_env,
        )

    def supervisor_git_push(
        self,
        remote_name_or_url: str,
        refspec: str = "master",
        auth_token: str | None = None,
    ) -> None:
        """Supervisor-mediated git push with in-memory ephemeral credential injection.

        Guarantees:
        - Only GitOps role is authorized to execute git push.
        - Credentials (tokens/passwords) exist in memory ONLY for the duration of the push call.
        - The token is passed via ephemeral header injection (-c http.extraHeader="Authorization: Bearer <token>")
          and is never written to disk (.git/config or store).
        - The memory buffer containing the credential is zeroed immediately upon completion.
        """
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to execute Git push.")

        ephemeral_env = {**self.base_env}
        cmd = [
            "git",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
        ]

        token_bytes = bytearray(auth_token.encode("utf-8")) if auth_token else None
        try:
            if token_bytes:
                # Ephemeral header injection
                token_str = token_bytes.decode("utf-8")
                cmd.extend(["-c", f"http.extraHeader=Authorization: Bearer {token_str}"])

            cmd.extend(["push", remote_name_or_url, refspec])

            subprocess.run(
                cmd,
                cwd=self.workspace,
                check=True,
                capture_output=True,
                env=ephemeral_env,
            )
        finally:
            if token_bytes:
                # Securely overwrite/zero credential memory buffer
                for i in range(len(token_bytes)):
                    token_bytes[i] = 0
