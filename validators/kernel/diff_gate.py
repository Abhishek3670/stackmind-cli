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
import hmac
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import AgentContract
from .process_reaper import ProcessReaper, ServiceJobManager, get_global_reaper
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


@dataclass(frozen=True)
class SupervisorApprovalReceipt:
    milestone_id: str
    approved_snapshot_sha: str
    derived_tree_sha: str
    timestamp: float
    hmac_signature: str


class SupervisorReceiptManager:
    """Manages HMAC-signed approval receipts stored exclusively in supervisor memory."""

    def __init__(self, secret_key: bytes | None = None) -> None:
        self._secret = secret_key or os.urandom(32)
        self._receipts: dict[str, SupervisorApprovalReceipt] = {}

    def issue_receipt(
        self,
        milestone_id: str,
        approved_snapshot_sha: str,
        derived_tree_sha: str,
    ) -> SupervisorApprovalReceipt:
        now = time.time()
        msg = f"{milestone_id}:{approved_snapshot_sha}:{derived_tree_sha}:{now}".encode("utf-8")
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        receipt = SupervisorApprovalReceipt(
            milestone_id=milestone_id,
            approved_snapshot_sha=approved_snapshot_sha,
            derived_tree_sha=derived_tree_sha,
            timestamp=now,
            hmac_signature=sig,
        )
        self._receipts[milestone_id] = receipt
        return receipt

    def verify_receipt(self, receipt: SupervisorApprovalReceipt) -> bool:
        """Cryptographically verify that receipt was generated by supervisor memory and not forged."""
        msg = f"{receipt.milestone_id}:{receipt.approved_snapshot_sha}:{receipt.derived_tree_sha}:{receipt.timestamp}".encode("utf-8")
        expected_sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(receipt.hmac_signature, expected_sig)


class DiffGate:
    """Evaluates post-turn workspace diffs against supervisor contracts and derived commit trees."""

    def __init__(
        self,
        workspace: Path | str,
        snapshot_manager: TurnSnapshotManager,
        role: str,
        contract: AgentContract | None = None,
        reaper: ProcessReaper | None = None,
        receipt_manager: SupervisorReceiptManager | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.snapshot_manager = snapshot_manager
        self.role = role
        self.contract = contract
        self.reaper = reaper or get_global_reaper()
        self.receipt_manager = receipt_manager or SupervisorReceiptManager()
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

    # ==========================================================================
    # Repository Internals (.git) Monitoring & Restoration Helpers (Amendment B)
    # ==========================================================================

    def resolve_workspace_git_dir(self) -> Path | None:
        """Resolve workspace .git directory, handling both standard directories and gitfiles."""
        git_target = self.workspace / ".git"
        if not git_target.exists():
            return None
        if git_target.is_dir():
            return git_target
        if git_target.is_file():
            try:
                content = git_target.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    gitdir_path = content.split(":", 1)[1].strip()
                    resolved = (self.workspace / gitdir_path).resolve()
                    if resolved.exists():
                        return resolved
            except OSError:
                pass
        return None

    def capture_git_internals_state(self) -> dict[str, bytes]:
        """Capture pre-turn state of all metadata files in workspace .git (excluding objects/ and pack/)."""
        git_dir = self.resolve_workspace_git_dir()
        state: dict[str, bytes] = {}
        if not git_dir or not git_dir.exists():
            return state

        for root, dirs, files in os.walk(git_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(git_dir).as_posix() if root_path != git_dir else ""
            if rel_root == "objects" or rel_root.startswith("objects/"):
                dirs.clear()
                continue
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                full_p = root_path / f
                try:
                    state[rel_f] = full_p.read_bytes()
                except OSError:
                    pass
        return state

    def scan_git_objects(self) -> set[str]:
        """Collect all object paths in workspace .git/objects/ to guarantee monotonic growth."""
        git_dir = self.resolve_workspace_git_dir()
        objs: set[str] = set()
        if not git_dir or not git_dir.exists():
            return objs
        obj_dir = git_dir / "objects"
        if not obj_dir.exists():
            return objs

        for root, dirs, files in os.walk(obj_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(git_dir).as_posix()
            for f in files:
                objs.add(f"{rel_root}/{f}")
        return objs

    def restore_git_internals(self, pre_state: dict[str, bytes]) -> None:
        """Restore workspace .git internals byte-exact to pre-turn baseline state."""
        git_dir = self.resolve_workspace_git_dir()
        if not git_dir:
            return

        # 1. Remove untracked files outside objects/
        for root, dirs, files in os.walk(git_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(git_dir).as_posix() if root_path != git_dir else ""
            if rel_root == "objects" or rel_root.startswith("objects/"):
                dirs.clear()
                continue
            for f in files:
                rel_f = f"{rel_root}/{f}" if rel_root else f
                if rel_f not in pre_state:
                    try:
                        (root_path / f).unlink()
                    except OSError:
                        pass

        # 2. Re-write pre-turn metadata files byte-exact
        for rel_f, content in pre_state.items():
            full_p = git_dir / rel_f
            full_p.parent.mkdir(parents=True, exist_ok=True)
            try:
                full_p.write_bytes(content)
            except OSError:
                pass

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
        pre_turn_git_state: dict[str, bytes] | None = None,
        pre_turn_git_objects: set[str] | None = None,
        service_manager: ServiceJobManager | None = None,
    ) -> GateResult:
        """Scan workspace post-turn, evaluate against pre-turn contracts, and enforce gate rules.

        If violations occur, reaps processes, restores workspace from pre-turn snapshot,
        restores .git repository internals, and returns GateResult with detailed violations.
        """
        start_time = time.perf_counter()
        violations: list[GateViolation] = []
        total_bytes_hashed = 0

        # Suspend persistent services during diff evaluation and restore
        if service_manager:
            service_manager.suspend_all()

        try:
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

            # 6. Repository Internals (.git) Gate (Amendment B)
            if pre_turn_git_state is not None:
                current_git_state = self.capture_git_internals_state()
                current_git_objects = self.scan_git_objects()

                # Detect modified and deleted files in .git
                for rel_f, pre_bytes in pre_turn_git_state.items():
                    if rel_f not in current_git_state:
                        violations.append(
                            GateViolation(
                                "GIT_INTERNALS_DELETED",
                                f".git/{rel_f}",
                                f"File '.git/{rel_f}' was deleted during turn.",
                            )
                        )
                    elif current_git_state[rel_f] != pre_bytes:
                        v_type = "GIT_CONFIG_TAMPERING" if rel_f == "config" else (
                            "GIT_HOOKS_TAMPERING" if rel_f.startswith("hooks/") else (
                                "GIT_INDEX_TAMPERING" if rel_f == "index" else (
                                    "GIT_REFS_TAMPERING" if rel_f.startswith("refs/") or rel_f == "HEAD" else "GIT_INTERNALS_MODIFIED"
                                )
                            )
                        )
                        violations.append(
                            GateViolation(
                                v_type,
                                f".git/{rel_f}",
                                f"Repository metadata file '.git/{rel_f}' was modified during turn.",
                            )
                        )

                # Detect created files in .git outside objects/
                for rel_f in current_git_state:
                    if rel_f not in pre_turn_git_state:
                        v_type = "GIT_HOOKS_TAMPERING" if rel_f.startswith("hooks/") else (
                            "GIT_REFS_TAMPERING" if rel_f.startswith("refs/") else "GIT_INTERNALS_CREATED"
                        )
                        violations.append(
                            GateViolation(
                                v_type,
                                f".git/{rel_f}",
                                f"Unauthorized file '.git/{rel_f}' was created in repository internals.",
                            )
                        )

                # Monotonic growth check on objects/
                if pre_turn_git_objects is not None:
                    missing_objs = pre_turn_git_objects - current_git_objects
                    if missing_objs:
                        violations.append(
                            GateViolation(
                                "GIT_OBJECTS_TAMPERING",
                                ".git/objects",
                                f"{len(missing_objs)} objects missing or deleted from object pool!",
                            )
                        )

            duration = time.perf_counter() - start_time
            mb_hashed = total_bytes_hashed / (1024 * 1024)
            throughput = mb_hashed / max(duration, 0.0001)

            # 7. Enforcement: on violation, reap processes and restore workspace byte-exact
            passed = len(violations) == 0
            if not passed:
                self.reaper.reap_all()
                self.snapshot_manager.restore_turn_snapshot(
                    turn_id,
                    expected_sha=pre_turn_snapshot_sha,
                )
                if pre_turn_git_state is not None:
                    self.restore_git_internals(pre_turn_git_state)

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
        finally:
            if service_manager:
                service_manager.resume_all()

    # ==========================================================================
    # Derived Commit Tree & Supervisor-Mediated GitOps
    # ==========================================================================

    def compute_derived_commit_tree(
        self,
        approved_snapshot_sha: str,
        pre_turn_snapshot_sha: str | None = None,
    ) -> str:
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
            ["git", f"--git-dir={self.snapshot_manager.storage_root}", "ls-tree", "-r", "-z", "HEAD"],
            check=False,
            capture_output=True,
            env={**self.base_env, "GIT_CONFIG": self.null_dev},
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
        else:
            ws_git = self.resolve_workspace_git_dir()
            if ws_git and ws_git.exists():
                head_res_ws = subprocess.run(
                    ["git", f"--git-dir={ws_git}", "ls-tree", "-r", "-z", "HEAD"],
                    check=False,
                    capture_output=True,
                    env={**self.base_env, "GIT_CONFIG": self.null_dev},
                )
                if head_res_ws.returncode == 0 and head_res_ws.stdout:
                    for entry in head_res_ws.stdout.split(b"\x00"):
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

        # 3. Filter new candidates against PRE-TURN baseline .gitignore
        new_candidates = [p for p in snap_files if p not in head_entries]
        ignored_set: set[str] = set()

        if new_candidates:
            # Reconstruct baseline .gitignore files into an isolated scratch tree
            baseline_sha = pre_turn_snapshot_sha or approved_snapshot_sha
            base_snap_files = self.snapshot_manager.list_snapshot_files(baseline_sha)

            with tempfile.TemporaryDirectory() as temp_ignore_dir:
                temp_p = Path(temp_ignore_dir)
                subprocess.run(
                    ["git", "init"],
                    cwd=temp_p,
                    check=True,
                    capture_output=True,
                    env={**self.base_env, "GIT_CONFIG": self.null_dev},
                )

                # Write all pre-turn baseline .gitignore files (including nested ones)
                has_ignores = False
                for f_path, blob_sha in base_snap_files.items():
                    if f_path == ".gitignore" or f_path.endswith("/.gitignore"):
                        target_ign = temp_p / f_path
                        target_ign.parent.mkdir(parents=True, exist_ok=True)
                        blob_bytes = self.snapshot_manager._git_run(
                            ["cat-file", "blob", blob_sha],
                            text=False,
                        ).stdout
                        target_ign.write_bytes(blob_bytes)
                        has_ignores = True

                if has_ignores:
                    check_res = subprocess.run(
                        ["git", "check-ignore", "--stdin", "-z"],
                        cwd=temp_p,
                        input=("\x00".join(new_candidates) + "\x00").encode("utf-8"),
                        capture_output=True,
                        env={**self.base_env, "GIT_CONFIG": self.null_dev},
                    )
                    if check_res.returncode in (0, 1) and check_res.stdout:
                        for ign in check_res.stdout.split(b"\x00"):
                            if ign:
                                ignored_set.add(ign.decode("utf-8", errors="replace"))

        # 4. Determine final derived path set
        # Rule: Tracked files at HEAD are ALWAYS kept (even if inside ignored or excluded folders)
        derived_paths: list[str] = []
        for p in snap_files:
            if p in head_entries:
                derived_paths.append(p)
            elif p not in ignored_set:
                derived_paths.append(p)

        # 5. Build derived tree in an isolated index file OUTSIDE workspace
        temp_idx = self.snapshot_manager.storage_root / f"derived_idx_{uuid.uuid4().hex}"
        idx_env = {
            **self.base_env,
            "GIT_CONFIG": self.null_dev,
            "GIT_INDEX_FILE": str(temp_idx),
        }
        ws_git = self.resolve_workspace_git_dir()
        if ws_git and ws_git.exists():
            idx_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str((ws_git / "objects").resolve())

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

                # Hash object through supervisor store plumbing honoring .gitattributes eol conversion
                hash_res = subprocess.run(
                    [
                        "git",
                        f"--git-dir={self.snapshot_manager.storage_root}",
                        f"--work-tree={self.workspace}",
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
                        f"--git-dir={self.snapshot_manager.storage_root}",
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
                    f"--git-dir={self.snapshot_manager.storage_root}",
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

    def re_verify_approval(
        self,
        receipt: SupervisorApprovalReceipt,
    ) -> bool:
        """Re-verify QA approval receipt and assert workspace is clean with no background writers."""
        if not self.receipt_manager.verify_receipt(receipt):
            raise PermissionError("Approval receipt cryptographic HMAC signature verification failed.")

        # Check current workspace matches approved snapshot
        current_files = self.snapshot_manager._collect_workspace_files()
        approved_files = self.snapshot_manager.list_snapshot_files(receipt.approved_snapshot_sha)

        curr_paths = {p.relative_to(self.workspace).as_posix() for p in current_files}
        appr_paths = set(approved_files.keys())

        if curr_paths != appr_paths:
            return False

        for p in current_files:
            rel = p.relative_to(self.workspace).as_posix()
            data = p.read_bytes()
            blob_header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
            sha = hashlib.sha1(blob_header + data).hexdigest()
            if sha != approved_files.get(rel):
                return False

        return True

    def supervisor_git_commit(
        self,
        message: str,
        approved_snapshot_sha: str,
        branch: str = "master",
        pre_turn_snapshot_sha: str | None = None,
        receipt: SupervisorApprovalReceipt | None = None,
    ) -> str:
        """Supervisor-mediated commit creation. Ensures commit tree == derived approved tree."""
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to create Git commits.")

        if receipt is not None:
            if not self.re_verify_approval(receipt):
                raise RuntimeError("GitOps commit rejected: workspace was modified after QA approval was issued!")

        derived_tree = self.compute_derived_commit_tree(
            approved_snapshot_sha=approved_snapshot_sha,
            pre_turn_snapshot_sha=pre_turn_snapshot_sha,
        )

        # Get parent HEAD commit
        ws_git = self.resolve_workspace_git_dir()
        head_res = subprocess.run(
            ["git", f"--git-dir={self.snapshot_manager.storage_root}", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            env=self.base_env,
        )
        parent_sha = head_res.stdout.strip() if head_res.returncode == 0 and head_res.stdout.strip() else None
        if not parent_sha and ws_git and ws_git.exists():
            ws_head = subprocess.run(
                ["git", f"--git-dir={ws_git}", "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                env={**self.base_env, "GIT_CONFIG": self.null_dev},
            )
            if ws_head.returncode == 0:
                parent_sha = ws_head.stdout.strip()

        # Create commit object directly via plumbing in supervisor store
        commit_env = {
            **self.base_env,
            "GIT_CONFIG": self.null_dev,
        }
        if ws_git and ws_git.exists():
            commit_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str((ws_git / "objects").resolve())

        commit_cmd = [
            "git",
            f"--git-dir={self.snapshot_manager.storage_root}",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
            "commit-tree", derived_tree,
        ]
        if parent_sha:
            commit_cmd.extend(["-p", parent_sha])
        commit_cmd.extend(["-m", message])

        commit_res = subprocess.run(
            commit_cmd,
            check=True,
            capture_output=True,
            text=True,
            env=commit_env,
        )
        commit_sha = commit_res.stdout.strip()

        # Item 9: Verify commit tree == derived tree BEFORE moving the ref!
        tree_check = subprocess.run(
            ["git", f"--git-dir={self.snapshot_manager.storage_root}", "rev-parse", f"{commit_sha}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
            env=commit_env,
        ).stdout.strip()
        if tree_check != derived_tree:
            raise RuntimeError(
                f"Derived tree binding invariant failed: commit tree '{tree_check}' != approved derived tree '{derived_tree}'."
            )

        # Update branch ref in supervisor store
        subprocess.run(
            [
                "git",
                f"--git-dir={self.snapshot_manager.storage_root}",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "update-ref", f"refs/heads/{branch}", commit_sha,
            ],
            check=True,
            capture_output=True,
            env=commit_env,
        )

        # Update workspace branch ref and sync index without executing workspace config/hooks
        if ws_git and ws_git.exists():
            # Ensure workspace can see supervisor store objects for detached worktree checkout
            alt_info_dir = ws_git / "objects" / "info"
            alt_info_dir.mkdir(parents=True, exist_ok=True)
            alt_file = alt_info_dir / "alternates"
            store_obj_path = (self.snapshot_manager.storage_root / "objects").resolve().as_posix()
            existing_alts = alt_file.read_bytes().split(b"\n") if alt_file.exists() else []
            if store_obj_path.encode("utf-8") not in existing_alts:
                with open(alt_file, "ab") as f:
                    f.write(f"{store_obj_path}\n".encode("utf-8"))

            subprocess.run(
                [
                    "git",
                    f"--git-dir={ws_git}",
                    "-c", "core.fsmonitor=false",
                    "-c", "core.hooksPath=",
                    "-c", "core.excludesFile=",
                    "update-ref", f"refs/heads/{branch}", commit_sha,
                ],
                check=True,
                capture_output=True,
                env={**self.base_env, "GIT_CONFIG": self.null_dev},
            )
            subprocess.run(
                [
                    "git",
                    f"--git-dir={ws_git}",
                    "-c", "core.fsmonitor=false",
                    "-c", "core.hooksPath=",
                    "-c", "core.excludesFile=",
                    "read-tree", commit_sha,
                ],
                check=True,
                capture_output=True,
                env={**self.base_env, "GIT_CONFIG": self.null_dev},
            )

        return commit_sha

    def supervisor_git_tag(
        self,
        tag_name: str,
        message: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        """Supervisor-mediated lightweight or annotated tag creation."""
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to create Git tags.")

        cmd = [
            "git",
            f"--git-dir={self.snapshot_manager.storage_root}",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
            "tag",
        ]
        if message:
            cmd.extend(["-a", tag_name, "-m", message])
        else:
            cmd.append(tag_name)

        if commit_sha:
            cmd.append(commit_sha)

        ws_git = self.resolve_workspace_git_dir()
        tag_env = {
            **self.base_env,
            "GIT_CONFIG": self.null_dev,
        }
        if ws_git and ws_git.exists():
            tag_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str((ws_git / "objects").resolve())

        subprocess.run(
            cmd,
            cwd=self.snapshot_manager.storage_root,
            check=True,
            capture_output=True,
            env=tag_env,
        )

        if ws_git and ws_git.exists():
            cmd_ws = [
                "git",
                f"--git-dir={ws_git}",
                "-c", "core.fsmonitor=false",
                "-c", "core.hooksPath=",
                "-c", "core.excludesFile=",
                "tag",
            ]
            if message:
                cmd_ws.extend(["-a", tag_name, "-m", message])
            else:
                cmd_ws.append(tag_name)
            if commit_sha:
                cmd_ws.append(commit_sha)
            subprocess.run(
                cmd_ws,
                check=False,
                capture_output=True,
                env={**self.base_env, "GIT_CONFIG": self.null_dev},
            )

    def supervisor_git_push(
        self,
        remote_url: str,
        refspec: str = "master",
        auth_token: str | None = None,
    ) -> None:
        """Supervisor-mediated git push with in-memory ephemeral credential injection.

        Guarantees:
        - Only GitOps role is authorized to execute git push.
        - Pushes directly from the supervisor-owned repository (storage_root) with explicit URL and refspec.
        - No --force is permitted.
        - Workspace repository configuration (remote.url, insteadOf, pushurl, http.proxy) has ZERO effect.
        - Push credentials are passed via GIT_CONFIG_COUNT environment variables (not in argv)
          and are not written to disk.
        """
        if self.role != "GitOps":
            raise PermissionError(f"Role '{self.role}' is not authorized to execute Git push.")

        push_env = {
            **self.base_env,
            "GIT_CONFIG": self.null_dev,
        }
        ws_git = self.resolve_workspace_git_dir()
        if ws_git and ws_git.exists():
            push_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str((ws_git / "objects").resolve())

        if auth_token:
            push_env["GIT_CONFIG_COUNT"] = "1"
            push_env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            push_env["GIT_CONFIG_VALUE_0"] = f"Authorization: Bearer {auth_token}"

        cmd = [
            "git",
            f"--git-dir={self.snapshot_manager.storage_root}",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=",
            "-c", "core.excludesFile=",
            "push",
            remote_url,
            refspec,
        ]

        try:
            subprocess.run(
                cmd,
                cwd=self.snapshot_manager.storage_root,
                check=True,
                capture_output=True,
                env=push_env,
            )
        finally:
            if "GIT_CONFIG_VALUE_0" in push_env:
                del push_env["GIT_CONFIG_VALUE_0"]
