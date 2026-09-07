"""D025 Destructive Operations Gate and Enforcement Engine (PLANv4 Risk 1 / D025 Protocol).

This module implements programmatic safeguards for agent-proposed command execution:
1. Detects destructive operations (git history rewrites, mass file deletion, docker pruning/removal).
2. Enforces mandatory pre-operation backup steps.
3. Enforces mandatory post-operation verification steps.
4. Blocks execution when safeguards are missing.
5. Emits structured audit events to the observability layer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.knowledge.contract import ContractAccessDenied


class D025ViolationError(ContractAccessDenied):
    """Exception raised when an agent command sequence violates the D025 protocol."""

    pass


@dataclass(frozen=True)
class D025CommandClassification:
    """Classification of a single shell command under D025 rules."""

    command: str
    is_destructive: bool
    destructive_category: str | None = None
    is_backup: bool = False
    is_verification: bool = False
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "destructive_category": self.destructive_category,
            "details": self.details,
            "is_backup": self.is_backup,
            "is_destructive": self.is_destructive,
            "is_verification": self.is_verification,
        }


@dataclass(frozen=True)
class D025GateDecision:
    """Outcome of evaluating a command sequence against D025 safeguards."""

    passed: bool
    destructive_detected: bool
    destructive_commands: tuple[str, ...] = ()
    has_backup: bool = False
    has_verification: bool = False
    reason: str | None = None
    classifications: tuple[D025CommandClassification, ...] = ()
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_count": len(self.classifications),
            "destructive_commands": list(self.destructive_commands),
            "destructive_detected": self.destructive_detected,
            "has_backup": self.has_backup,
            "has_verification": self.has_verification,
            "passed": self.passed,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class D025Gate:
    """Evaluator and gatekeeper for D025 Destructive Operations Protocol."""

    # Patterns for destructive operations
    DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        # Git history rewrite & destructive git ops
        ("git_history_rewrite", re.compile(r"\bgit\s+(reset\s+--hard|filter-repo|filter-branch|rebase\b)", re.IGNORECASE)),
        ("git_force_push", re.compile(r"\bgit\s+push\s+.*(--force|-f\b|--delete\b)", re.IGNORECASE)),
        ("git_destructive_clean", re.compile(r"\bgit\s+clean\s+.*(-[fxd]+|--force)", re.IGNORECASE)),
        ("git_force_branch_delete", re.compile(r"\bgit\s+branch\s+.*(-D\b|--delete\s+--force)", re.IGNORECASE)),
        ("git_hard_checkout", re.compile(r"\bgit\s+checkout\s+.*(-f\b|--force|\.\s*$)", re.IGNORECASE)),
        ("git_hard_restore", re.compile(r"\bgit\s+restore\s+.*(\.\s*$|--staged\s+\.)", re.IGNORECASE)),

        # Mass file / directory deletion
        ("mass_file_deletion", re.compile(r"\b(rm|del|Remove-Item)\s+.*(-[rfRF]+|/s|/f|-Recurse|\*|\.\*)", re.IGNORECASE)),
        ("rmdir_recursive", re.compile(r"\brmdir\s+.*(/[sS]|--ignore-fail-on-non-empty)", re.IGNORECASE)),

        # Docker mass removal / pruning
        ("docker_prune", re.compile(r"\bdocker\s+(system\s+prune|volume\s+prune|image\s+prune|container\s+prune)", re.IGNORECASE)),
        ("docker_force_remove", re.compile(r"\bdocker\s+(rm\s+.*(-f|--force)|rmi\s+.*(-f|--force)|volume\s+rm)", re.IGNORECASE)),

        # Database drops / truncate
        ("database_drop", re.compile(r"\b(drop\s+database|drop\s+table|truncate\s+table)\b", re.IGNORECASE)),
    )

    # Fallback keyword checks for simple or obscure destructive commands
    DESTRUCTIVE_FALLBACK_KEYWORDS: tuple[str, ...] = (
        "rm ", "del ", "git reset", "git push", "git filter-repo",
        "git filter-branch", "docker rm", "docker rmi",
    )

    # Patterns for backup operations
    BACKUP_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\b(cp|copy|xcopy|robocopy)\s+.*(-[raRA]+|--archive|--recursive)?\s+.*(backup|\.bak|archive)", re.IGNORECASE),
        re.compile(r"\b(cp|copy|xcopy|robocopy)\s+.*(\.git|\bdata\b|\bsrc\b)", re.IGNORECASE),
        re.compile(r"\btar\s+-[czvfC]+", re.IGNORECASE),
        re.compile(r"\bzip\s+", re.IGNORECASE),
        re.compile(r"\bgit\s+archive\b", re.IGNORECASE),
        re.compile(r"\bgit\s+(branch|tag)\s+backup", re.IGNORECASE),
        re.compile(r"\bdocker\s+(tag|commit|save|export)\b", re.IGNORECASE),
        re.compile(r"\b(pg_dump|mysqldump|sqlite3\s+.*\.backup)\b", re.IGNORECASE),
        re.compile(r"\b(backup|archive)\b", re.IGNORECASE),
    )

    # Patterns for verification operations
    VERIFY_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bgit\s+status\b", re.IGNORECASE),
        re.compile(r"\bgit\s+log\b", re.IGNORECASE),
        re.compile(r"\b(ls|dir|Get-ChildItem)\b", re.IGNORECASE),
        re.compile(r"\bwc\s+-l\b", re.IGNORECASE),
        re.compile(r"\bpytest\b", re.IGNORECASE),
        re.compile(r"\bstackmind\s+validate\b", re.IGNORECASE),
        re.compile(r"\bdocker\s+(ps|images)\b", re.IGNORECASE),
        re.compile(r"\b(diff|test\s+-[fe])\b", re.IGNORECASE),
    )

    def classify_command(self, cmd: str) -> D025CommandClassification:
        """Classify a single command for destructive, backup, or verify attributes."""
        cmd_stripped = cmd.strip()
        destructive_category: str | None = None
        is_destructive = False

        for category, pattern in self.DESTRUCTIVE_PATTERNS:
            if pattern.search(cmd_stripped):
                is_destructive = True
                destructive_category = category
                break

        if not is_destructive:
            lower_cmd = cmd_stripped.lower()
            for kw in self.DESTRUCTIVE_FALLBACK_KEYWORDS:
                if kw in lower_cmd:
                    is_destructive = True
                    destructive_category = "fallback_keyword_match"
                    break

        is_backup = any(pattern.search(cmd_stripped) for pattern in self.BACKUP_PATTERNS)
        is_verification = any(pattern.search(cmd_stripped) for pattern in self.VERIFY_PATTERNS)

        return D025CommandClassification(
            command=cmd_stripped,
            is_destructive=is_destructive,
            destructive_category=destructive_category,
            is_backup=is_backup,
            is_verification=is_verification,
        )

    def is_destructive(self, cmd: str) -> bool:
        """Return True if command is destructive under D025."""
        return self.classify_command(cmd).is_destructive

    def is_backup(self, cmd: str) -> bool:
        """Return True if command performs backup."""
        return self.classify_command(cmd).is_backup

    def is_verification(self, cmd: str) -> bool:
        """Return True if command performs verification."""
        return self.classify_command(cmd).is_verification

    def evaluate_sequence(self, commands: Sequence[str]) -> D025GateDecision:
        """Evaluate an entire sequence of commands under D025 rules."""
        if not commands:
            return D025GateDecision(
                passed=True,
                destructive_detected=False,
                reason="No commands to evaluate",
            )

        classifications = [self.classify_command(cmd) for cmd in commands]
        destructive_indices = [i for i, c in enumerate(classifications) if c.is_destructive]

        if not destructive_indices:
            return D025GateDecision(
                passed=True,
                destructive_detected=False,
                classifications=tuple(classifications),
                reason="No destructive commands detected",
            )

        destructive_cmds = tuple(classifications[i].command for i in destructive_indices)

        # For every destructive command, check for backup before and verification after
        all_passed = True
        missing_reasons: list[str] = []
        has_any_backup = False
        has_any_verify = False

        for idx in destructive_indices:
            cmd = classifications[idx].command
            category = classifications[idx].destructive_category

            # Check backup before idx
            backup_before = any(classifications[j].is_backup for j in range(idx))
            if backup_before:
                has_any_backup = True
            else:
                missing_reasons.append(
                    f"Destructive op '{cmd}' ({category}) lacks required pre-operation backup step"
                )
                all_passed = False

            # Check verification after idx
            verify_after = any(classifications[k].is_verification for k in range(idx + 1, len(classifications)))
            if verify_after:
                has_any_verify = True
            else:
                missing_reasons.append(
                    f"Destructive op '{cmd}' ({category}) lacks required post-operation verification step"
                )
                all_passed = False

        if all_passed:
            reason = "All D025 destructive safeguards (backup and verify) satisfied"
        else:
            reason = "; ".join(missing_reasons)

        return D025GateDecision(
            passed=all_passed,
            destructive_detected=True,
            destructive_commands=destructive_cmds,
            has_backup=has_any_backup,
            has_verification=has_any_verify,
            reason=reason,
            classifications=tuple(classifications),
        )

    def log_decision(
        self,
        project_path: Path,
        agent: str,
        decision: D025GateDecision,
        task_id: str | None = None,
    ) -> Path:
        """Record D025 decision event to observability logs (.sync/state/harness/d025_events.jsonl)."""
        log_dir = project_path / ".sync" / "state" / "harness"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "d025_events.jsonl"

        payload = {
            "agent": agent,
            "decision": decision.to_dict(),
            "event": "harness.d025_gate",
            "task_id": task_id,
        }

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")

        return log_file
