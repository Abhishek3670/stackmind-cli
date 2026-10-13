"""Execution verification contracts for Fix 4.

Defines explicit contracts for runtime acceptance verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class VerificationStatus(Enum):
    """Status of a verification check."""
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"  # Could not run
    STALE = "STALE"  # Evidence outdated
    PENDING = "PENDING"


class VerificationMethod(Enum):
    """Method used for verification."""
    STATIC = "STATIC"
    EXECUTION = "EXECUTION"
    HTTP = "HTTP"
    IMPORT = "IMPORT"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class ExecutionContract:
    """Explicit contract for runtime behavior verification.

    Defines what needs to be verified, how, and what constitutes success.
    """
    requirement_id: str
    criterion_id: str

    # What to execute
    working_directory: str
    command: tuple[str, ...]
    env_vars: dict[str, str] = field(default_factory=dict)

    # Expected results
    expected_exit_code: int = 0
    expected_stdout_contains: tuple[str, ...] = field(default_factory=tuple)
    expected_stderr_contains: tuple[str, ...] = field(default_factory=tuple)
    expected_files_created: tuple[str, ...] = field(default_factory=tuple)

    # Constraints
    timeout_seconds: float = 30.0
    requires_dependencies: tuple[str, ...] = field(default_factory=tuple)

    # Verification method
    method: VerificationMethod = VerificationMethod.EXECUTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "criterion_id": self.criterion_id,
            "working_directory": self.working_directory,
            "command": list(self.command),
            "env_vars": self.env_vars,
            "expected_exit_code": self.expected_exit_code,
            "expected_stdout_contains": list(self.expected_stdout_contains),
            "expected_stderr_contains": list(self.expected_stderr_contains),
            "expected_files_created": list(self.expected_files_created),
            "timeout_seconds": self.timeout_seconds,
            "requires_dependencies": list(self.requires_dependencies),
            "method": self.method.value,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a verification contract.

    Contains all evidence needed for audit and review.
    """
    requirement_id: str
    criterion_id: str
    contract_hash: str

    # Execution details
    command: tuple[str, ...]
    working_directory: str
    timeout_seconds: float

    # Results
    status: VerificationStatus
    exit_code: int | None
    timed_out: bool
    duration_ms: float

    # Output (sanitized)
    stdout: str
    stderr: str

    # File changes
    files_created: tuple[str, ...]
    files_modified: tuple[str, ...]

    # Fingerprinting
    source_fingerprint: str | None = None
    config_fingerprint: str | None = None

    # Metadata
    verified_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    runner_id: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "criterion_id": self.criterion_id,
            "contract_hash": self.contract_hash,
            "command": list(self.command),
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "files_created": list(self.files_created),
            "files_modified": list(self.files_modified),
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "verified_at": self.verified_at,
            "runner_id": self.runner_id,
        }

    @property
    def is_success(self) -> bool:
        return self.status == VerificationStatus.PASS


@dataclass
class RequirementVerification:
    """Aggregate verification result for a requirement."""
    requirement_id: str
    criteria: list[ExecutionResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "criteria": [c.to_dict() for c in self.criteria],
            "all_passed": all(c.is_success for c in self.criteria),
            "any_blocked": any(c.status == VerificationStatus.BLOCKED for c in self.criteria),
        }
