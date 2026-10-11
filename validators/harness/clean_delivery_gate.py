"""Clean delivery gate - validates that product deliverables contain no internal StackMind references."""

import os
from pathlib import Path
from typing import Optional


class CleanDeliveryGate:
    """Gate that validates clean delivery policy: no StackMind/internal runtime references in product files."""

    # Terms that indicate internal runtime leakage into product deliverables
    PROHIBITED_TERMS = [
        "stackmind",
        "stackmind-cli",
        "managed by stackmind",
        "stackmind runtime",
        "agent: codex",
        "agent: gemma",
        "agent: gemini",
        "agent: claude",
        "agent: local-llm",
    ]

    # File patterns that should not appear in product root
    INTERNAL_FILE_REFERENCES = [
        "AGENTS.md",
        ".sync/",
        ".sync/contracts",
        ".sync/inbox",
        ".sync/work-orders",
        ".sync/runtime",
    ]

    def __init__(self, project_path: Path):
        self.project_path = project_path.resolve()

    def scan(self) -> list[dict]:
        """
        Scan product-deliverable boundary for prohibited references.

        Returns list of findings with keys:
          - file: relative path where the issue was found
          - line: line number (1-indexed) or None for file-level issues
          - issue: description of the prohibited reference
          - severity: "error" or "warning"
        """
        findings: list[dict] = []

        # Track excluded paths
        excluded_dirs = {".sync", "graphify-out", ".git"}

        # Scan tracked files in product boundary
        for py in self._scan_product_boundary():
            findings.extend(self._scan_file(py, excluded_dirs))

        # Check commit messages for internal references
        findings.extend(self._scan_commit_history())

        return findings

    def _scan_product_boundary(self) -> list[Path]:
        """Yield all files in product boundary (excluding internal directories and infrastructure)."""
        if not self.project_path.exists():
            return []

        # Directories that are internal infrastructure, not product deliverables
        internal_dirs = {".sync", "graphify-out", ".git", "tests", "fixtures", ".venv", ".pytest_cache"}

        # Path prefixes that indicate infrastructure, not product files
        internal_prefixes = [
            "tests/",
            "fixtures/",
            "validators/harness/",
            "cli/",
            "templates/",
            ".venv/",
            ".pytest_cache/",
        ]

        for root, dirs, files in os.walk(self.project_path):
            # Filter out internal directories
            dirs[:] = [d for d in dirs if d not in internal_dirs]

            for filename in files:
                # Skip internal runtime files
                if filename.startswith("stackmind") or filename == "AGENTS.md":
                    continue

                # Skip gitignore files (they legitimately contain runtime paths)
                if filename in (".gitignore", ".git/info/exclude"):
                    continue

                full_path = Path(root) / filename
                rel_path = str(full_path.relative_to(self.project_path))

                # Skip infrastructure paths - these are development-time, not product
                if any(rel_path.startswith(prefix) for prefix in internal_prefixes):
                    continue

                yield full_path

    def _scan_file(self, path: Path, excluded_dirs: set[str]) -> list[dict]:
        """Scan a single file for prohibited references."""
        findings: list[dict] = []

        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return findings

        rel_path = str(path.relative_to(self.project_path))

        for line_num, line in enumerate(content.splitlines(), start=1):
            line_lower = line.lower()

            # Check for prohibited terms
            for term in self.PROHIBITED_TERMS:
                if term.lower() in line_lower:
                    findings.append({
                        "file": rel_path,
                        "line": line_num,
                        "issue": f"Prohibited term found: '{term}'",
                        "severity": "error",
                    })
                    break

            # Check for internal file references
            for ref in self.INTERNAL_FILE_REFERENCES:
                if ref in line:
                    findings.append({
                        "file": rel_path,
                        "line": line_num,
                        "issue": f"Internal runtime reference found: '{ref}'",
                        "severity": "error",
                    })
                    break

        return findings

    def _scan_commit_history(self) -> list[dict]:
        """Check git commit messages and trailers for internal references."""
        findings: list[dict] = []

        try:
            import subprocess
            # Get recent commit messages
            result = subprocess.run(
                ["git", "log", "--oneline", "-20"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=10,
            )

            for line in result.stdout.splitlines():
                line_lower = line.lower()
                for term in self.PROHIBITED_TERMS:
                    if term.lower() in line_lower:
                        findings.append({
                            "file": "git-log",
                            "line": None,
                            "issue": f"Commit message contains prohibited term: '{term}'",
                            "severity": "warning",
                        })
                        break
        except Exception:
            pass

        return findings

    def validate(self) -> tuple[bool, list[dict]]:
        """
        Run full clean delivery validation.

        Returns:
            (passed, findings) where passed is True if no errors found.
        """
        findings = self.scan()
        errors = [f for f in findings if f["severity"] == "error"]
        return (len(errors) == 0, findings)
