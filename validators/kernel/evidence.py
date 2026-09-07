"""Authentic per-operation observation model, evidence-derived dimensions, and eligibility gate.

Implements Phase P3 (Verification + Authentic Experience Capture):
1. Complete authentic observation records per operation (outputs, hashes, diffs, timing).
2. Evidence-derived trust and verification dimensions.
3. Deterministic ExperienceEligibilityGate.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.harness.snapshot import TrustLevel, VerificationDimensions, WorkspaceDiff
from validators.kernel.contract import AgentContract
from validators.kernel.sandbox import ProcessSandbox
from validators.kernel.workspace import ScratchWorkspace, WorkspaceEscapeError


def _hash_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, PermissionError):
        return None


@dataclass(frozen=True)
class FileDiffSnapshot:
    """Detailed before-and-after diff telemetry for a single touched file."""

    path: str
    before_hash: str | None
    after_hash: str | None
    diff: str = ""
    status: str = "modified"  # "added", "modified", "deleted", "unchanged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "after_hash": self.after_hash,
            "before_hash": self.before_hash,
            "diff": self.diff,
            "path": self.path,
            "status": self.status,
        }


@dataclass(frozen=True)
class AuthenticObservation:
    """Authentic runtime telemetry captured directly from an operation's execution."""

    operation_id: str
    operation_type: str
    target: str
    command: tuple[str, ...] | str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    files_touched: tuple[str, ...] = ()
    before_hashes: dict[str, str] = field(default_factory=dict)
    after_hashes: dict[str, str] = field(default_factory=dict)
    diff: str = ""
    file_snapshots: tuple[FileDiffSnapshot, ...] = ()
    resource_usage: dict[str, Any] = field(default_factory=dict)
    contract_authorized: bool = True
    contract_reason: str = "authorized"
    verified: bool = False
    verification_details: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_error(self) -> bool:
        if not self.contract_authorized:
            return True
        if self.exit_code is not None and self.exit_code != 0:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "after_hashes": self.after_hashes,
            "before_hashes": self.before_hashes,
            "command": list(self.command) if isinstance(self.command, tuple) else self.command,
            "contract_authorized": self.contract_authorized,
            "contract_reason": self.contract_reason,
            "diff": self.diff,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "file_snapshots": [s.to_dict() for s in self.file_snapshots],
            "files_touched": list(self.files_touched),
            "is_error": self.is_error,
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "recorded_at": self.recorded_at,
            "resource_usage": self.resource_usage,
            "stderr": self.stderr,
            "stdout": self.stdout,
            "target": self.target,
            "verification_details": self.verification_details,
            "verified": self.verified,
        }


class AuthenticEvidenceTracer:
    """Tracer executing and capturing authentic telemetry across operations."""

    def __init__(self, workspace: ScratchWorkspace) -> None:
        self.workspace = workspace
        self.sandbox = ProcessSandbox(workspace)
        self._observations: list[AuthenticObservation] = []

    @property
    def observations(self) -> tuple[AuthenticObservation, ...]:
        return tuple(self._observations)

    def trace_write(
        self,
        target: str,
        content: str,
        *,
        operation_id: str = "",
        contract_authorized: bool = True,
        contract_reason: str = "authorized",
    ) -> AuthenticObservation:
        """Trace an authentic file write operation with before/after diff telemetry."""
        if not contract_authorized:
            obs = AuthenticObservation(
                operation_id=operation_id,
                operation_type="write_file",
                target=f"workspace/{target}",
                contract_authorized=False,
                contract_reason=contract_reason,
                exit_code=-1,
                stderr=contract_reason,
            )
            self._observations.append(obs)
            return obs

        path = self.workspace.path_for(target)
        before_text = ""
        before_hash = None
        status = "added"
        if path.exists():
            before_text = path.read_text(encoding="utf-8", errors="replace")
            before_hash = _hash_file(path)
            status = "modified"

        t0 = time.perf_counter()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        t1 = time.perf_counter()

        after_hash = _hash_file(path)
        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"before/{target}",
                tofile=f"after/{target}",
            )
        )
        diff_str = "".join(diff_lines)

        file_snap = FileDiffSnapshot(
            path=target,
            before_hash=before_hash,
            after_hash=after_hash,
            diff=diff_str,
            status=status,
        )

        obs = AuthenticObservation(
            operation_id=operation_id,
            operation_type="write_file",
            target=f"workspace/{target}",
            exit_code=0,
            stdout=f"Wrote {len(content)} characters to {target}",
            duration_ms=max(1, int((t1 - t0) * 1000)),
            files_touched=(target,),
            before_hashes={target: before_hash or ""},
            after_hashes={target: after_hash or ""},
            diff=diff_str,
            file_snapshots=(file_snap,),
            contract_authorized=True,
            contract_reason="authorized",
            verified=True,
        )
        self._observations.append(obs)
        return obs

    def trace_command(
        self,
        command: Sequence[str],
        *,
        operation_id: str = "",
        contract_authorized: bool = True,
        contract_reason: str = "authorized",
    ) -> AuthenticObservation:
        """Trace a sandboxed shell/subprocess command with authentic stdout, stderr, and hashes."""
        if not contract_authorized:
            obs = AuthenticObservation(
                operation_id=operation_id,
                operation_type="run_command",
                target="workspace/command",
                command=tuple(command),
                contract_authorized=False,
                contract_reason=contract_reason,
                exit_code=-1,
                stderr=contract_reason,
            )
            self._observations.append(obs)
            return obs

        # Capture before hashes of workspace files
        before_hashes: dict[str, str] = {}
        for p in self.workspace.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.workspace.root).as_posix()
                h = _hash_file(p)
                if h:
                    before_hashes[rel] = h

        t0 = time.perf_counter()
        try:
            result = self.sandbox.run(command)
            t1 = time.perf_counter()
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except WorkspaceEscapeError as ex:
            t1 = time.perf_counter()
            obs = AuthenticObservation(
                operation_id=operation_id,
                operation_type="run_command",
                target="workspace/command",
                command=tuple(command),
                contract_authorized=False,
                contract_reason=f"WorkspaceEscapeError: {ex}",
                exit_code=-1,
                stderr=f"WorkspaceEscapeError: {ex}",
                duration_ms=max(1, int((t1 - t0) * 1000)),
            )
            self._observations.append(obs)
            return obs


        # Capture after hashes
        after_hashes: dict[str, str] = {}
        for p in self.workspace.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.workspace.root).as_posix()
                h = _hash_file(p)
                if h:
                    after_hashes[rel] = h

        all_keys = sorted(set(before_hashes.keys()) | set(after_hashes.keys()))
        touched: list[str] = []
        snapshots: list[FileDiffSnapshot] = []

        for rel in all_keys:
            b_h = before_hashes.get(rel)
            a_h = after_hashes.get(rel)
            if b_h != a_h:
                touched.append(rel)
                status = "added" if b_h is None else ("deleted" if a_h is None else "modified")
                snapshots.append(FileDiffSnapshot(path=rel, before_hash=b_h, after_hash=a_h, status=status))

        obs = AuthenticObservation(
            operation_id=operation_id,
            operation_type="run_command",
            target="workspace/command",
            command=tuple(command),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=max(1, int((t1 - t0) * 1000)),
            files_touched=tuple(touched),
            before_hashes=before_hashes,
            after_hashes=after_hashes,
            file_snapshots=tuple(snapshots),
            contract_authorized=True,
            contract_reason="authorized",
            verified=(exit_code == 0),
        )
        self._observations.append(obs)
        return obs


def derive_verification_dimensions(
    observations: Sequence[AuthenticObservation],
    contract: AgentContract | None = None,
    workspace_root: Path | str | None = None,
) -> VerificationDimensions:
    """Derive multi-dimensional verification flags directly from authentic telemetry."""
    if not observations:
        return VerificationDimensions()

    # 1. Scope verified
    scope_verified = True
    for obs in observations:
        if not obs.contract_authorized:
            scope_verified = False
            break
        if contract is not None:
            # Check files touched against contract allow/deny
            for f in obs.files_touched:
                target_path = f"workspace/{f}".replace("\\", "/")
                if any(fnmatch.fnmatch(target_path, rule) for rule in contract.deny):
                    scope_verified = False
                    break
                if not any(fnmatch.fnmatch(target_path, rule) for rule in contract.allow):
                    scope_verified = False
                    break

    # 2. State verified: Files touched have valid before/after hashes
    state_verified = True
    has_modifications = False
    for obs in observations:
        for snap in obs.file_snapshots:
            has_modifications = True
            if snap.status in ("added", "modified") and not snap.after_hash:
                state_verified = False
            if snap.status == "modified" and snap.before_hash == snap.after_hash:
                state_verified = False

    # 3. Code verified: Python files touched must parse cleanly
    code_verified = True
    if workspace_root is not None:
        root_path = Path(workspace_root)
        for obs in observations:
            for f in obs.files_touched:
                if f.endswith(".py"):
                    full_p = root_path / f
                    if full_p.exists():
                        try:
                            ast.parse(full_p.read_text(encoding="utf-8"))
                        except Exception:
                            code_verified = False

    # Check test command exits
    for obs in observations:
        if obs.operation_type == "run_command" and obs.command:
            cmd_str = " ".join(obs.command) if isinstance(obs.command, tuple) else str(obs.command)
            if "pytest" in cmd_str or "test" in cmd_str:
                if obs.exit_code != 0:
                    code_verified = False

    # 4. Behavioral verified: All executed actions completed cleanly with returncode 0
    behavioral_verified = all(
        obs.exit_code == 0 for obs in observations if obs.exit_code is not None
    )

    # 5. Security verified: No escapes, no denied ops, no forbidden path traversal
    security_verified = True
    for obs in observations:
        if not obs.contract_authorized:
            security_verified = False
        if ".." in obs.target or obs.target.startswith(("/", "\\")):
            security_verified = False
        if "WorkspaceEscapeError" in obs.stderr:
            security_verified = False

    # 6. Outcome verified: At least one action succeeded and no errors
    outcome_verified = len(observations) > 0 and not any(obs.is_error for obs in observations)

    return VerificationDimensions(
        scope_verified=scope_verified,
        state_verified=state_verified,
        code_verified=code_verified,
        behavioral_verified=behavioral_verified,
        security_verified=security_verified,
        outcome_verified=outcome_verified,
    )


@dataclass(frozen=True)
class EligibilityDecision:
    """Authoritative decision on whether an execution is eligible for procedural learning."""

    eligible: bool
    trust_level: TrustLevel
    reasons: tuple[str, ...]
    dimensions: VerificationDimensions
    authentic_evidence_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "authentic_evidence_count": self.authentic_evidence_count,
            "dimensions": self.dimensions.to_dict(),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "trust_level": self.trust_level.value,
        }


class ExperienceEligibilityGate:
    """Gating mechanism qualifying ExperienceRecords as LEARNING_ELIGIBLE."""

    @classmethod
    def evaluate(
        cls,
        observations: Sequence[AuthenticObservation],
        dimensions: VerificationDimensions,
        *,
        canary_passed: bool = True,
        has_blockers: bool = False,
    ) -> EligibilityDecision:
        reasons: list[str] = []

        if not observations:
            reasons.append("No authentic observations recorded.")

        if not dimensions.all_passed:
            failed = [k for k, v in dimensions.to_dict().items() if not v and k != "all_passed"]
            reasons.append(f"Verification dimensions failed: {', '.join(failed)}")

        if not canary_passed:
            reasons.append("Sandbox canary execution failed.")

        if has_blockers:
            reasons.append("Unhandled blockers present.")

        # Ensure all observations contain authentic telemetry
        authentic_count = sum(
            1 for o in observations
            if o.duration_ms >= 0 and (o.stdout or o.stderr or o.diff or o.files_touched or o.file_snapshots)
        )
        if authentic_count < len(observations):
            reasons.append(
                f"{len(observations) - authentic_count} operation(s) lack authentic telemetry."
            )

        eligible = len(reasons) == 0
        if eligible:
            trust_level = TrustLevel.LEARNING_ELIGIBLE
        elif dimensions.all_passed:
            trust_level = TrustLevel.VERIFIED
        else:
            trust_level = TrustLevel.OBSERVABLE

        return EligibilityDecision(
            eligible=eligible,
            trust_level=trust_level,
            reasons=tuple(reasons),
            dimensions=dimensions,
            authentic_evidence_count=authentic_count,
        )
