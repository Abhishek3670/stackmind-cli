"""Deterministic security scanner for governed deliverables.

Catches the mechanical, high-confidence findings that a model reviewer
routinely misses — hardcoded secret fallbacks and debug flags left enabled —
so they are enforced by gates (D024 deliverable validation, QA scan evidence)
instead of relying on model eyeballs.

Findings carry stable codes so gates and evidence packets can reference them:
- HARDCODED_SECRET_FALLBACK (critical): a secret-named variable bound to a
  string literal, directly or as an os.environ/os.getenv fallback default.
- DEBUG_MODE_ENABLED (critical): ``debug = True`` left in shipped code.
- DANGEROUS_DYNAMIC_EXEC (medium): use of ``eval``/``exec``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SECRET_NAME_PATTERN = (
    r"(?i)\b[A-Za-z0-9_]*(?:"
    r"secret|token|password|passwd|api[_-]?key|auth[_-]?key"
    r"|private[_-]?key|access[_-]?key|signing[_-]?key"
    r")[A-Za-z0-9_]*\b"
)

# Variable-name suffixes that indicate a non-secret concept even when the name
# contains a keyword (token_url, password_policy, secret_name, ...).
_NON_SECRET_NAME_PATTERN = re.compile(
    r"(?i)^(?:.*_)?(?:url|header|name|policy|rule|prompt|hint|label|placeholder|"
    r"field|column|type|kind|format|file|path|prefix|length|min|max|rotation|"
    r"expiry|ttl|hash|algorithm|help|doc|docstring|message|msg|error|title|"
    r"description|count|list|cache|scope|header_name)$"
)

_COMMENT_LINE_PATTERN = re.compile(r"^\s*(#|//)")

# env-get with a literal fallback: SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
_ENV_FALLBACK_PATTERN = re.compile(
    SECRET_NAME_PATTERN
    + r"\s*=\s*os\s*\.\s*(?:environ(?:\.get)?|getenv)\s*\(\s*['\"][^'\"]*['\"]\s*(?:,\s*['\"][^'\"]+['\"])\s*[\)]",
    re.DOTALL,
)
# direct literal: SECRET_KEY = "dev-secret-key-12345"
_DIRECT_LITERAL_PATTERN = re.compile(
    SECRET_NAME_PATTERN + r"\s*=\s*['\"]([^'\"]{8,})['\"]"
)
_DEBUG_ASSIGN_PATTERN = re.compile(
    r"(?:\bdebug\b\s*[=:]\s*True\b|['\"]debug['\"]\s*:\s*True\b)",
    re.IGNORECASE,
)
_DYNAMIC_EXEC_PATTERN = re.compile(r"\b(eval|exec)\s*\(")


@dataclass(frozen=True)
class SecurityFinding:
    """One deterministic security finding with a stable machine-readable code."""

    code: str
    severity: str
    path: str
    line: int
    message: str
    match: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "match": self.match,
        }

    def format(self) -> str:
        base = (
            f"security finding {self.code} ({self.severity}): "
            f"{self.message} ({self.path}:{self.line})"
        )
        if self.match:
            base += f" -> {self.match}"
        return base


def _is_non_secret_name(name: str) -> bool:
    base = name.lower().strip("_")
    return bool(_NON_SECRET_NAME_PATTERN.match(base))


def scan_file(rel_path: str, content: str, *, allow_debug: bool = False) -> list[SecurityFinding]:
    """Scan one file's content and return deterministic security findings."""
    findings: list[SecurityFinding] = []
    is_python = rel_path.endswith(".py")
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or _COMMENT_LINE_PATTERN.match(line):
            continue
        code_part = line.split("#", 1)[0] if is_python else line

        env_match = _ENV_FALLBACK_PATTERN.search(code_part)
        if env_match:
            findings.append(SecurityFinding(
                code="HARDCODED_SECRET_FALLBACK",
                severity="critical",
                path=rel_path,
                line=line_no,
                message=(
                    "secret variable falls back to a hardcoded literal default; "
                    "require the value from the environment and fail fast when missing"
                ),
                match=line.strip()[:100],
            ))
            continue

        literal_match = _DIRECT_LITERAL_PATTERN.search(code_part)
        if literal_match:
            name = literal_match.group(0).split("=", 1)[0].strip()
            if not _is_non_secret_name(name):
                findings.append(SecurityFinding(
                    code="HARDCODED_SECRET_FALLBACK",
                    severity="critical",
                    path=rel_path,
                    line=line_no,
                    message=(
                        "secret variable is bound to a hardcoded string literal; "
                        "load it from configuration or the environment instead"
                    ),
                    match=line.strip()[:100],
                ))
            continue

        if is_python and _DEBUG_ASSIGN_PATTERN.search(code_part):
            if allow_debug:
                continue
            findings.append(SecurityFinding(
                code="DEBUG_MODE_ENABLED",
                severity="critical",
                path=rel_path,
                line=line_no,
                message=(
                    "debug mode is enabled in shipped code; disable it for "
                    "production (debug=False, driven by configuration)"
                ),
                match=line.strip()[:100],
            ))
            continue

        if _DYNAMIC_EXEC_PATTERN.search(code_part):
            func_name = _DYNAMIC_EXEC_PATTERN.search(code_part).group(1)
            findings.append(SecurityFinding(
                code="DANGEROUS_DYNAMIC_EXEC",
                severity="medium",
                path=rel_path,
                line=line_no,
                message=f"use of {func_name}() on dynamic input; avoid or restrict it",
                match=line.strip()[:100],
            ))
    return findings


def scan_files(entries: Iterable[tuple[str, str]], *, allow_debug: bool = False) -> list[SecurityFinding]:
    """Scan (relative_path, content) pairs and return sorted findings."""
    findings: list[SecurityFinding] = []
    for rel_path, content in entries:
        findings.extend(scan_file(rel_path, content, allow_debug=allow_debug))
    findings.sort(key=lambda f: (f.path, f.line, f.code))
    return findings


def scan_directory(root: Path, target: str = ".", *, allow_debug: bool = False) -> list[SecurityFinding]:
    """Scan Python files under ``root/target`` (directory or single file)."""
    target_path = (Path(root) / target).resolve() if not Path(target).is_absolute() else Path(target)
    if target_path.is_file():
        entries = [(target_path.name, _read(target_path))]
    else:
        entries = []
        for f in sorted(target_path.rglob("*.py")):
            rel = f.relative_to(root).as_posix() if f.is_relative_to(Path(root)) else f.as_posix()
            parts = {p.lower() for p in f.parts}
            if parts & {".git", ".venv", "venv", "__pycache__", "node_modules", ".sync"}:
                continue
            entries.append((rel, _read(f)))
    return scan_files(((p, c) for p, c in entries if c is not None), allow_debug=allow_debug)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


__all__ = [
    "SecurityFinding",
    "scan_directory",
    "scan_file",
    "scan_files",
]
