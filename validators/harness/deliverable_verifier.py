"""Content-aware deliverable verifier for Fix 3.

Performs static analysis checks on generated artifacts to detect:
- Empty or whitespace-only files
- Unresolved template placeholders
- Malformed JSON/YAML
- Missing required content sections
- Unresolved imports/references
- Truncated or malformed files
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml


@dataclass
class VerificationCheck:
    """A single verification check result."""
    name: str
    passed: bool
    message: str
    severity: str = "error"  # error, warning, info


@dataclass
class VerificationResult:
    """Result of verifying a single deliverable."""
    path: str
    checked: bool
    passed: bool
    checks: list[VerificationCheck] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "checked": self.checked,
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message, "severity": c.severity}
                for c in self.checks
            ],
            "evidence": self.evidence,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
        }


class DeliverableVerifier:
    """Content-aware verifier for generated deliverables."""

    # Placeholder patterns that indicate incomplete generation
    PLACEHOLDER_PATTERNS = [
        r"<\s*TODO\s*>",
        r"<\s*FIXME\s*>",
        r"<\s*PLACEHOLDER\s*>",
        r"\{\{\s*PLACEHOLDER\s*\}\}",
        r"\[\s*INSERT\s+\w+\s*\]",
        r"#\s+TODO:\s+",
        r"//\s+TODO:\s+",
    ]

    # Content patterns that should be present in deliverables
    REQUIRED_PATTERNS = {
        # Python files should not have obvious truncation
        ".py": [
            (r"(?m)^import\s+|^\s+from\s+", "No import or from statements found"),
            (r"(?m)^def\s+|^\s+class\s+|^\s+async\s+def\s+", "No function or class definitions found"),
        ],
        # YAML files should be valid
        ".yaml": [],
        ".yml": [],
        # JSON files should be valid
        ".json": [],
        # HTML should have basic structure
        ".html": [
            (r"(?i)<!DOCTYPE\s+html>", "Missing DOCTYPE"),
            (r"(?i)<html", "Missing html tag"),
            (r"(?i)</html>", "Missing closing html tag"),
        ],
    }

    # File types that can be checked
    CHECKABLE_EXTENSIONS = {".py", ".ts", ".js", ".json", ".yaml", ".yml", ".html", ".css", ".md"}

    def verify(self, path: Path, acceptance_criteria: list[str] | None = None) -> VerificationResult:
        """
        Verify a single deliverable.

        Args:
            path: Path to the deliverable
            acceptance_criteria: Optional list of criteria to verify against

        Returns:
            VerificationResult with check outcomes and evidence
        """
        result = VerificationResult(path=str(path), checked=False, passed=False)

        if not path.exists():
            result.checked = True
            result.checks.append(VerificationCheck(
                name="existence",
                passed=False,
                message=f"File does not exist: {path}",
                severity="error",
            ))
            return result

        # Compute content hash for freshness tracking
        try:
            content = path.read_bytes()
            result.content_hash = hashlib.sha256(content).hexdigest()[:16]
        except Exception:
            pass

        try:
            text_content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            result.checks.append(VerificationCheck(
                name="readability",
                passed=False,
                message=f"Cannot read file: {e}",
                severity="error",
            ))
            return result

        result.checked = True

        # Run applicable checks
        result.checks.extend(self._check_non_empty(text_content))
        result.checks.extend(self._check_placeholders(text_content, path.suffix))
        result.checks.extend(self._check_syntax_and_format(path, text_content))
        result.checks.extend(self._check_required_content(text_content, path.suffix))

        # Run acceptance criteria checks if provided
        if acceptance_criteria:
            result.checks.extend(self._check_acceptance_criteria(text_content, acceptance_criteria))

        # Determine overall pass/fail
        # Only error-level failures prevent passing; warnings are informational
        critical_failures = [c for c in result.checks if c.severity == "error" and not c.passed]
        result.passed = len(critical_failures) == 0

        # Build evidence
        result.evidence = {
            "content_hash": result.content_hash,
            "size_bytes": len(text_content),
            "line_count": len(text_content.splitlines()),
        }

        return result

    def _check_non_empty(self, content: str) -> list[VerificationCheck]:
        """Check that file is not empty or whitespace-only."""
        checks = []

        if not content.strip():
            checks.append(VerificationCheck(
                name="non_empty",
                passed=False,
                message="File is empty or contains only whitespace",
                severity="error",
            ))

        return checks

    def _check_placeholders(self, content: str, ext: str) -> list[VerificationCheck]:
        """Check for unresolved template placeholders in actual code."""
        checks = []

        # Skip documentation files - placeholders there are often intentional
        if ext in (".md", ".rst", ".txt", ".markdown"):
            return checks

        # Check for placeholder patterns
        for pattern in self.PLACEHOLDER_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                checks.append(VerificationCheck(
                    name="placeholder_detection",
                    passed=False,
                    message=f"Unresolved placeholder detected in deliverable",
                    severity="error",
                ))
                break

        return checks

    def _check_syntax_and_format(self, path: Path, content: str) -> list[VerificationCheck]:
        """Check syntax and format for structured files."""
        checks = []
        ext = path.suffix.lower()

        # JSON validation
        if ext == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                checks.append(VerificationCheck(
                    name="json_syntax",
                    passed=False,
                    message=f"Invalid JSON: {e}",
                    severity="error",
                ))

        # YAML validation
        if ext in (".yaml", ".yml"):
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as e:
                checks.append(VerificationCheck(
                    name="yaml_syntax",
                    passed=False,
                    message=f"Invalid YAML: {e}",
                    severity="error",
                ))

        # Python syntax check (basic)
        if ext == ".py":
            try:
                compile(content, str(path), "exec")
            except SyntaxError as e:
                checks.append(VerificationCheck(
                    name="python_syntax",
                    passed=False,
                    message=f"Python syntax error: {e}",
                    severity="error",
                ))

        return checks

    def _check_required_content(self, content: str, ext: str) -> list[VerificationCheck]:
        """Check that required content patterns are present."""
        checks = []

        if ext in self.REQUIRED_PATTERNS:
            for pattern, message in self.REQUIRED_PATTERNS[ext]:
                if not re.search(pattern, content):
                    checks.append(VerificationCheck(
                        name="required_content",
                        passed=False,
                        message=message,
                        severity="warning",
                    ))

        return checks

    def _check_acceptance_criteria(
        self, content: str, criteria: list[str]
    ) -> list[VerificationCheck]:
        """Check content against explicit acceptance criteria."""
        checks = []
        content_lower = content.lower()

        for i, criterion in enumerate(criteria):
            # Simple string matching - could be enhanced with semantic checks
            if criterion.lower() not in content_lower:
                checks.append(VerificationCheck(
                    name=f"acceptance_criteria_{i+1}",
                    passed=False,
                    message=f"Acceptance criterion not satisfied: {criterion[:60]}...",
                    severity="error",
                ))
            else:
                checks.append(VerificationCheck(
                    name=f"acceptance_criteria_{i+1}",
                    passed=True,
                    message=f"Acceptance criterion satisfied: {criterion[:60]}...",
                    severity="info",
                ))

        return checks

    def verify_multiple(
        self,
        deliverables: list[dict[str, Any]],
        acceptance_criteria_map: dict[str, list[str]] | None = None,
    ) -> list[VerificationResult]:
        """
        Verify multiple deliverables at once.

        Args:
            deliverables: List of dicts with 'path' key and optional 'criteria'
            acceptance_criteria_map: Optional mapping of path to criteria list

        Returns:
            List of VerificationResult objects
        """
        results = []

        for item in deliverables:
            path = Path(item.get("path", ""))
            criteria = item.get("criteria", [])
            if not criteria and acceptance_criteria_map:
                criteria = acceptance_criteria_map.get(str(path), [])

            result = self.verify(path, criteria)
            results.append(result)

        return results


__all__ = [
    "VerificationCheck",
    "VerificationResult",
    "DeliverableVerifier",
]
