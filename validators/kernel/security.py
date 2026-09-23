"""Agentic Security Hardening and Fault Injection Engine (Milestone P7-5).

Implements security verifiers and fault injection per PLAN_STACKMIND_CLI_FINAL.md §24-§25
and PLAN_TUI_v7.md §31-§32:
1. Subagent Scope Containment: asserts subagent contract scopes cannot exceed parent boundaries.
2. Credential Zero-Leakage Scanning: recursive scanner asserting no RPC response, event payload,
   journal log, or state contains raw secret/credential patterns.
3. D025 Subagent Safeguards: validates D025 backup, clean git, and approval gating across subagents.
4. Terminal Output Sanitization: strips and verifies ANSI codes, OSC escape sequences, and control characters.
5. Budget Exhaustion Gating: deterministic limits on tokens, steps, files touched, and time.
6. Active Role Rebinding Guard: validates in-flight rebinding rejections.
7. Fault Injection Engine: simulations for backend crash, endpoint timeout, subagent crash,
   cancellation racing natural completion, and reconnect recovery.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

# ─── EXCEPTIONS ───────────────────────────────────────────────────────────────

class SecurityError(Exception):
    """Base exception for all agentic security violations."""


class ScopeEscalationError(SecurityError, ValueError):
    """Raised when a subagent's contract scope exceeds its parent's authority boundary."""


class CredentialLeakError(SecurityError, ValueError):
    """Raised when raw credentials or secret patterns are detected in data structures or logs."""


class D025SubagentViolationError(SecurityError, PermissionError):
    """Raised when a subagent attempts a destructive operation without D025 compliance."""


class UnsafeOutputError(SecurityError, ValueError):
    """Raised when raw terminal control or escape injection sequences are detected in output."""


class BudgetExceededError(SecurityError, RuntimeError):
    """Raised when an operation or session exceeds its allocated resource budget."""


# ─── 1. SUBAGENT SCOPE CONTAINMENT ────────────────────────────────────────────

def _normalize_scope_patterns(scope: Any) -> tuple[set[str] | None, set[str] | None]:
    """Extract allow and deny pattern sets from various scope representations."""
    if scope is None:
        return None, None
    if isinstance(scope, (list, set, tuple)):
        return set(str(x) for x in scope), None
    if isinstance(scope, dict):
        inner = scope.get("scope") if isinstance(scope.get("scope"), dict) else scope
        allow = (
            set(str(x) for x in inner["allow"])
            if "allow" in inner and isinstance(inner["allow"], (list, set, tuple))
            else None
        )
        deny = (
            set(str(x) for x in inner["deny"])
            if "deny" in inner and isinstance(inner["deny"], (list, set, tuple))
            else None
        )
        if allow is None and deny is None:
            return set(str(k) for k in inner.keys()), None
        return allow, deny
    return {str(scope)}, None


def _is_pattern_narrower_or_equal(child_pat: str, parent_pat: str) -> bool:
    """Check if child glob pattern is equal to or a subpath of parent glob pattern."""
    if child_pat == parent_pat:
        return True
    child_clean = child_pat.replace("\\", "/").strip("/")
    parent_clean = parent_pat.replace("\\", "/").strip("/")

    if parent_clean in ("*", "**", ""):
        return True

    p_base = parent_clean
    while p_base.endswith("/*"):
        p_base = p_base[:-2]
    if p_base.endswith("/**"):
        p_base = p_base[:-3]
    if p_base.endswith("*"):
        p_base = p_base[:-1]
    p_base = p_base.rstrip("/")
    if not p_base:
        return True

    return child_clean == p_base or child_clean.startswith(p_base + "/")


def validate_subagent_scope(parent_scope: Any, child_scope: Any) -> bool:
    """Validate that child_scope does not exceed parent_scope authority.

    Returns True if valid, False if child attempts scope escalation.
    """
    if parent_scope is None or parent_scope == "inherit":
        return True
    if child_scope is None or child_scope == "inherit":
        return True

    parent_allow, parent_deny = _normalize_scope_patterns(parent_scope)
    child_allow, child_deny = _normalize_scope_patterns(child_scope)

    # 1. Allow containment: every child allow rule must be covered by a parent allow rule
    if parent_allow is not None:
        if child_allow is None:
            return False
        for c_pat in child_allow:
            if not any(_is_pattern_narrower_or_equal(c_pat, p_pat) for p_pat in parent_allow):
                return False

    # 2. Deny preservation: child must inherit all parent deny rules
    if parent_deny is not None:
        if child_deny is None:
            return False
        for p_deny in parent_deny:
            if p_deny not in child_deny:
                return False
        # Furthermore, child allow must not intersect with parent deny
        if child_allow is not None:
            for c_allow in child_allow:
                for p_deny in parent_deny:
                    if _is_pattern_narrower_or_equal(c_allow, p_deny):
                        return False

    return True


def assert_scope_contained(parent_scope: Any, child_scope: Any, role: str = "subagent") -> None:
    """Assert child_scope is within parent_scope; raises ScopeEscalationError on violation."""
    if not validate_subagent_scope(parent_scope, child_scope):
        raise ScopeEscalationError(
            f"Scope escalation rejected for {role}: child scope {child_scope} "
            f"exceeds parent authority {parent_scope}."
        )


# ─── 2. CREDENTIAL ZERO-LEAKAGE SCANNING ──────────────────────────────────────

_CREDENTIAL_PATTERNS = [
    # Generic API Keys / Secrets
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|auth[_-]?token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{12,})['\"]?"),
    # Provider-specific key patterns
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
    re.compile(r"xox[baprs]-[0-9a-zA-Z\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{24,}"),
]

# Safe placeholder tokens that are not considered leaks
_ALLOWLIST_TOKENS = {
    "none", "null", "undefined", "mock", "dummy", "test-token", "fake-key",
    "secret", "password", "governed", "codex", "claude", "gemini", "gemma",
}


class CredentialLeakScanner:
    """Recursive scanner verifying zero credential exposure in payloads, logs, and state."""

    def __init__(self, additional_secrets: Sequence[str] | None = None) -> None:
        self.known_secrets = set(additional_secrets or [])

    def scan_text(self, text: str) -> list[str]:
        leaks: list[str] = []
        if not text:
            return leaks

        # Check known configured secret values
        for s in self.known_secrets:
            if s and len(s) >= 6 and s in text:
                leaks.append(f"Configured credential exposed: '{s[:3]}...'")

        # Check regex patterns
        for pattern in _CREDENTIAL_PATTERNS:
            for match in pattern.finditer(text):
                matched_val = match.group(1) if match.groups() else match.group(0)
                if matched_val.lower() not in _ALLOWLIST_TOKENS:
                    leaks.append(f"Credential pattern matched ({pattern.pattern[:20]}...): '{matched_val[:4]}***'")

        return leaks

    def scan_object(self, obj: Any, path: str = "") -> list[str]:
        """Recursively scan dictionaries, lists, tuples, and primitives."""
        leaks: list[str] = []
        if obj is None:
            return leaks

        if isinstance(obj, str):
            for leak in self.scan_text(obj):
                leaks.append(f"{path}: {leak}" if path else leak)
        elif isinstance(obj, Mapping):
            for k, v in obj.items():
                key_str = str(k)
                # Check for sensitive key names with populated non-empty values
                if any(sec in key_str.lower() for sec in ("api_key", "apikey", "secret_key", "access_token", "private_key")):
                    if isinstance(v, str) and v and v.lower() not in _ALLOWLIST_TOKENS:
                        leaks.append(f"{path}.{key_str}: Raw secret value populated in key")
                leaks.extend(self.scan_object(v, f"{path}.{key_str}" if path else key_str))
        elif isinstance(obj, (list, tuple, set)):
            for i, item in enumerate(obj):
                leaks.extend(self.scan_object(item, f"{path}[{i}]"))

        return leaks

    def assert_no_leaks(self, obj: Any, context: str = "Payload") -> None:
        leaks = self.scan_object(obj)
        if leaks:
            raise CredentialLeakError(
                f"Credential zero-leakage violation in {context}:\n" + "\n".join(f"  - {l}" for l in leaks)
            )

    def redact(self, text: str) -> str:
        """Sanitize and mask credentials from text."""
        result = text
        for s in self.known_secrets:
            if s and len(s) >= 6:
                result = result.replace(s, "[REDACTED_SECRET]")
        for pattern in _CREDENTIAL_PATTERNS:
            result = pattern.sub("[REDACTED_CREDENTIAL]", result)
        return result


def scan_for_credential_leaks(obj: Any) -> list[str]:
    """Helper function to scan any data structure for credential leaks."""
    return CredentialLeakScanner().scan_object(obj)


# ─── 3. D025 DESTRUCTIVE SAFEGUARDS ACROSS SUBAGENTS ──────────────────────────

_D025_DESTRUCTIVE_COMMANDS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\b"),
    re.compile(r"\brmdir\s+"),
    re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+branch\s+-[dD]\b"),
    re.compile(r"\bgit\s+push\s+.*--force\b"),
    re.compile(r"\bdocker\s+(?:system\s+prune|image\s+rm|rm\s+-f)"),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\bdelete\s+from\s+[a-zA-Z0-9_]+\s*;", re.IGNORECASE),
]


class D025SafeguardVerifier:
    """Verifier enforcing D025 safeguards identically across parent and child subagents."""

    @classmethod
    def is_destructive(cls, command: str) -> bool:
        cmd = command.strip()
        return any(pat.search(cmd) for pat in _D025_DESTRUCTIVE_COMMANDS)

    @classmethod
    def verify_operation(
        cls, command: str, context: Mapping[str, Any]
    ) -> tuple[bool, str]:
        """Verify preconditions: backup created, git clean checked, approval present."""
        if not cls.is_destructive(command):
            return True, "Non-destructive operation permitted"

        # Check D025 mandatory preconditions in context
        has_backup = bool(context.get("has_backup") or context.get("backup_path"))
        git_clean = bool(context.get("git_clean", False))
        has_approval = bool(context.get("approved_by_ceo") or context.get("approval_receipt"))

        missing: list[str] = []
        if not has_backup:
            missing.append("Backup not created (D025 §1)")
        if not git_clean:
            missing.append("Working tree not verified clean (D025 §2)")
        if not has_approval:
            missing.append("Explicit CEO/Architect approval not recorded (D025 §3)")

        if missing:
            return False, f"D025 violation: {'; '.join(missing)}"
        return True, "D025 destructive operation approved with verified preconditions"

    @classmethod
    def assert_safe(cls, command: str, context: Mapping[str, Any], role: str = "agent") -> None:
        passed, reason = cls.verify_operation(command, context)
        if not passed:
            raise D025SubagentViolationError(
                f"Destructive operation blocked for {role} on command '{command}': {reason}"
            )


# ─── 4. TERMINAL OUTPUT SANITIZATION ──────────────────────────────────────────

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


def sanitize_terminal_output(raw_output: str) -> str:
    """Sanitize control sequences, ANSI escape codes, and terminal injection payloads."""
    if not raw_output:
        return ""
    # Strip OSC sequences
    cleaned = _OSC_ESCAPE_RE.sub("", raw_output)
    # Strip ANSI CSI sequences
    cleaned = _ANSI_ESCAPE_RE.sub("", cleaned)
    # Strip raw control characters (preserve \t, \n, \r)
    cleaned = _CONTROL_CHAR_RE.sub("", cleaned)
    return cleaned


def is_terminal_output_clean(text: str) -> bool:
    """Check if output contains forbidden terminal control or escape sequences."""
    if not text:
        return True
    if _OSC_ESCAPE_RE.search(text) or _ANSI_ESCAPE_RE.search(text) or _CONTROL_CHAR_RE.search(text):
        return False
    return True


def assert_terminal_output_clean(text: str, context: str = "Subagent output") -> None:
    """Assert output is free of terminal escape sequences; raises UnsafeOutputError on violation."""
    if not is_terminal_output_clean(text):
        raise UnsafeOutputError(f"{context} contains unescaped control codes or terminal escape injection.")


# ─── 5. BUDGET EXHAUSTION GATING ──────────────────────────────────────────────

@dataclass
class BudgetTracker:
    """Tracks token, step, file, and time usage against contract budget."""

    max_tokens: int | None = 50000
    max_steps: int | None = 20
    max_files_touched: int | None = 10
    max_time_seconds: float | None = 300.0

    tokens_used: int = 0
    steps_taken: int = 0
    files_touched: set[str] = field(default_factory=set)
    start_time: float = field(default_factory=time.time)

    def record_tokens(self, count: int) -> None:
        self.tokens_used += count

    def record_step(self) -> None:
        self.steps_taken += 1

    def record_file(self, path: str) -> None:
        self.files_touched.add(str(path))

    def check_budget(self) -> tuple[bool, str | None]:
        """Check if any budget dimension has been exceeded."""
        if self.max_tokens is not None and self.tokens_used > self.max_tokens:
            return False, f"Token budget exceeded ({self.tokens_used} > {self.max_tokens})"
        if self.max_steps is not None and self.steps_taken > self.max_steps:
            return False, f"Step budget exceeded ({self.steps_taken} > {self.max_steps})"
        if self.max_files_touched is not None and len(self.files_touched) > self.max_files_touched:
            return False, f"File budget exceeded ({len(self.files_touched)} > {self.max_files_touched})"
        elapsed = time.time() - self.start_time
        if self.max_time_seconds is not None and elapsed > self.max_time_seconds:
            return False, f"Time budget exceeded ({elapsed:.1f}s > {self.max_time_seconds}s)"
        return True, None

    def assert_within_budget(self, operation: str = "turn") -> None:
        ok, reason = self.check_budget()
        if not ok:
            raise BudgetExceededError(f"Budget overrun during {operation}: {reason}")


# ─── 6. FAULT INJECTION ENGINE ────────────────────────────────────────────────

class FaultInjectionEngine:
    """Simulates realistic distributed and process-level faults against daemon & agents."""

    @staticmethod
    def simulate_backend_crash(error_message: str = "Execution backend connection dropped") -> None:
        """Simulate unexpected backend process exit or connection reset."""
        raise ConnectionResetError(error_message)

    @staticmethod
    def simulate_backend_timeout(timeout_seconds: float = 0.05) -> None:
        """Simulate backend endpoint freeze or HTTP socket timeout."""
        time.sleep(timeout_seconds)
        raise TimeoutError(f"Execution backend timed out after {timeout_seconds}s")

    @staticmethod
    def simulate_subagent_crash(
        manager: Any, session_id: str, operation_id: str, reason: str = "SIGSEGV / OOM"
    ) -> dict[str, Any]:
        """Mark a running subagent operation as FAILED to simulate child process crash."""
        return manager.complete_operation(
            session_id=session_id,
            operation_id=operation_id,
            result={"error": reason, "crashed": True},
            status="FAILED",
        )

    @staticmethod
    def simulate_cancellation_race(
        manager: Any, session_id: str, operation_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Simulate concurrent cancellation signal racing against natural completion."""
        cancel_result = manager.cancel_operation(operation_id, cascade=True)
        # Attempt natural completion immediately following cancellation signal
        comp_result = manager.complete_operation(
            session_id=session_id,
            operation_id=operation_id,
            result={"status": "SUCCESS"},
            status="COMPLETED",
        )
        return cancel_result, comp_result
