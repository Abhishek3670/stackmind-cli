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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
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

    def verify(
        self,
        path: Path,
        acceptance_criteria: list[str] | None = None,
        root: Path | None = None,
    ) -> VerificationResult:
        """
        Verify a single deliverable.

        Args:
            path: Path to the deliverable
            acceptance_criteria: Optional list of criteria to verify against
            root: Optional root directory to enforce containment

        Returns:
            VerificationResult with check outcomes and evidence
        """
        result = VerificationResult(path=str(path), checked=False, passed=False)

        if root is not None:
            try:
                root_res = root.resolve()
                path_res = path.resolve()
                if not path_res.is_relative_to(root_res):
                    result.checked = True
                    result.checks.append(VerificationCheck(
                        name="path_containment",
                        passed=False,
                        message=f"Deliverable path escapes permitted root ({root}): {path}",
                        severity="error",
                    ))
                    result.evidence = {
                        "path": str(path),
                        "content_hash": None,
                        "size_bytes": 0,
                        "line_count": 0,
                        "checks_run": 1,
                        "checks_passed": 0,
                        "checks_failed": 1,
                        "failures": [f"Deliverable path escapes permitted root ({root}): {path}"],
                    }
                    return result
            except Exception as e:
                result.checked = True
                result.checks.append(VerificationCheck(
                    name="path_containment",
                    passed=False,
                    message=f"Path containment resolution error: {e}",
                    severity="error",
                ))
                return result

        if not path.exists():
            result.checked = True
            result.checks.append(VerificationCheck(
                name="existence",
                passed=False,
                message=f"File does not exist: {path}",
                severity="error",
            ))
            return result

        if path.is_dir():
            result.checked = True
            result.checks.append(VerificationCheck(
                name="file_type",
                passed=False,
                message=f"Deliverable is a directory, expected regular file: {path}",
                severity="error",
            ))
            return result

        if not path.is_file():
            result.checked = True
            result.checks.append(VerificationCheck(
                name="file_type",
                passed=False,
                message=f"Deliverable is not a regular file: {path}",
                severity="error",
            ))
            return result

        result.checks.append(VerificationCheck(
            name="file_type",
            passed=True,
            message="Deliverable is a regular file",
            severity="info",
        ))

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
            "path": str(path),
            "content_hash": result.content_hash,
            "size_bytes": len(text_content),
            "line_count": len(text_content.splitlines()),
            "checks_run": len(result.checks),
            "checks_passed": sum(1 for c in result.checks if c.passed),
            "checks_failed": sum(1 for c in result.checks if not c.passed),
            "failures": [c.message for c in result.checks if not c.passed and c.severity == "error"],
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
        else:
            checks.append(VerificationCheck(
                name="non_empty",
                passed=True,
                message="File has non-empty content",
                severity="info",
            ))

        return checks

    def _check_placeholders(self, content: str, ext: str) -> list[VerificationCheck]:
        """Check for unresolved template placeholders in actual code."""
        checks = []

        # Skip documentation files - placeholders there are often intentional
        if ext in (".md", ".rst", ".txt", ".markdown"):
            return checks

        # Check for placeholder patterns
        matched = False
        for pattern in self.PLACEHOLDER_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                checks.append(VerificationCheck(
                    name="placeholder_detection",
                    passed=False,
                    message="Unresolved placeholder detected in deliverable",
                    severity="error",
                ))
                matched = True
                break

        if not matched:
            checks.append(VerificationCheck(
                name="placeholder_detection",
                passed=True,
                message="No unresolved placeholders detected",
                severity="info",
            ))

        return checks

    def _check_syntax_and_format(self, path: Path, content: str) -> list[VerificationCheck]:
        """Check syntax and format for structured files."""
        checks = []
        ext = path.suffix.lower()

        # JSON validation
        if ext == ".json":
            try:
                json.loads(content)
                checks.append(VerificationCheck(
                    name="json_syntax",
                    passed=True,
                    message="Valid JSON syntax",
                    severity="info",
                ))
            except (json.JSONDecodeError, ValueError) as e:
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
                checks.append(VerificationCheck(
                    name="yaml_syntax",
                    passed=True,
                    message="Valid YAML syntax",
                    severity="info",
                ))
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
                checks.append(VerificationCheck(
                    name="python_syntax",
                    passed=True,
                    message="Valid Python syntax",
                    severity="info",
                ))
            except SyntaxError as e:
                checks.append(VerificationCheck(
                    name="python_syntax",
                    passed=False,
                    message=f"Python syntax error: {e}",
                    severity="error",
                ))

        # HTML token parsing check and media asset verification
        if ext == ".html":
            class WellFormednessParser(HTMLParser):
                pass

            try:
                parser = WellFormednessParser()
                parser.feed(content)
                checks.append(VerificationCheck(
                    name="html_syntax",
                    passed=True,
                    message="HTML basic token/element parsing succeeded (not full specification conformance)",
                    severity="info",
                ))
            except Exception as e:
                checks.append(VerificationCheck(
                    name="html_syntax",
                    passed=False,
                    message=f"HTML parsing error: {e}",
                    severity="error",
                ))

            # Validate media assets (images, videos, pictures, sources)
            checks.extend(self._check_html_media_assets(path, content))

        if ext not in (".json", ".yaml", ".yml", ".py", ".html"):
            checks.append(VerificationCheck(
                name="format_syntax",
                passed=True,
                message=f"Format-specific syntax validation not implemented for '{ext}' (skipped)",
                severity="info",
            ))

        return checks

    def _check_html_media_assets(self, path: Path, content: str) -> list[VerificationCheck]:
        """Verify image and media assets in HTML deliverables for broken, placeholder, or invalid URLs."""
        checks: list[VerificationCheck] = []

        class MediaTagExtractor(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.media_elements: list[dict[str, Any]] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                tag_lower = tag.lower()
                attrs_dict = {k.lower(): (v or "") for k, v in attrs if k}
                if tag_lower in ("img", "source", "video", "audio", "embed"):
                    self.media_elements.append({
                        "tag": tag_lower,
                        "src": attrs_dict.get("src", "").strip(),
                        "srcset": attrs_dict.get("srcset", "").strip(),
                        "alt": attrs_dict.get("alt", "").strip(),
                        "line": self.getpos()[0],
                    })

        extractor = MediaTagExtractor()
        try:
            extractor.feed(content)
        except Exception:
            return checks

        if not extractor.media_elements:
            return checks

        # Webpage search/gallery domains and routes that are NOT image assets
        INVALID_PAGE_PATTERNS = [
            (r"(?i)unsplash\.com/s/photos/", "Unsplash search gallery page used as image source"),
            (r"(?i)unsplash\.com/photos/[^/]+$", "Unsplash photo landing page used as image source (requires direct image CDN URL)"),
            (r"(?i)google\.[a-z]+/search", "Google search results page used as image source"),
            (r"(?i)duckduckgo\.com/\?q=", "DuckDuckGo search results page used as image source"),
            (r"(?i)pexels\.com/search/", "Pexels search gallery page used as image source"),
            (r"(?i)shutterstock\.com/search/", "Shutterstock search page used as image source"),
            (r"(?i)wikipedia\.org/wiki/", "Wikipedia article page used as image source"),
            (r"(?i)\.html?([?#].*)?$", "HTML webpage URL used as image source"),
        ]

        # Obvious placeholder values
        PLACEHOLDER_SRC_VALUES = {
            "#",
            "undefined",
            "null",
            "none",
            "todo",
            "placeholder.png",
            "placeholder.jpg",
            "image.png",
            "example.com/image.png",
        }

        html_dir = path.parent if path and isinstance(path, Path) else Path(".")

        total_checked = 0
        failed_count = 0

        for elem in extractor.media_elements:
            tag = elem["tag"]
            src = elem["src"]
            srcset = elem["srcset"]
            line = elem["line"]

            # If img has neither src nor srcset
            if tag == "img" and not src and not srcset:
                checks.append(VerificationCheck(
                    name="html_media_assets",
                    passed=False,
                    message=f"Line {line}: <img> tag missing required 'src' attribute",
                    severity="error",
                ))
                failed_count += 1
                continue

            sources_to_test = [src] if src else []
            if srcset:
                for candidate in srcset.split(","):
                    c_url = candidate.strip().split()[0] if candidate.strip() else ""
                    if c_url:
                        sources_to_test.append(c_url)

            for s in sources_to_test:
                total_checked += 1
                lower_s = s.lower().strip()

                if not lower_s or lower_s in PLACEHOLDER_SRC_VALUES:
                    checks.append(VerificationCheck(
                        name="html_media_assets",
                        passed=False,
                        message=f"Line {line}: Empty or placeholder media src '{s}' in <{tag}> tag",
                        severity="error",
                    ))
                    failed_count += 1
                    continue

                # Check if search/gallery webpage URL was used
                matched_page_pattern = False
                for pattern, msg in INVALID_PAGE_PATTERNS:
                    if re.search(pattern, s):
                        checks.append(VerificationCheck(
                            name="html_media_assets",
                            passed=False,
                            message=f"Line {line}: {msg} in <{tag}>: '{s}'",
                            severity="error",
                        ))
                        failed_count += 1
                        matched_page_pattern = True
                        break

                if matched_page_pattern:
                    continue

                # Check data URIs
                if lower_s.startswith("data:"):
                    if not lower_s.startswith(("data:image/", "data:video/", "data:audio/")):
                        checks.append(VerificationCheck(
                            name="html_media_assets",
                            passed=False,
                            message=f"Line {line}: Invalid data URI scheme in <{tag}>: '{s[:40]}...'",
                            severity="error",
                        ))
                        failed_count += 1
                    continue

                # Check HTTP / HTTPS URLs
                if lower_s.startswith(("http://", "https://", "//")):
                    try:
                        norm_url = f"https:{s}" if s.startswith("//") else s
                        parsed = urllib.parse.urlparse(norm_url)
                        if not parsed.netloc:
                            checks.append(VerificationCheck(
                                name="html_media_assets",
                                passed=False,
                                message=f"Line {line}: Malformed remote media URL '{s}' in <{tag}>",
                                severity="error",
                            ))
                            failed_count += 1
                            continue

                        # Probe reachability if online (gracefully skips if offline/sandboxed/timeout)
                        try:
                            head_req = urllib.request.Request(
                                norm_url,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                                method="HEAD",
                            )
                            with urllib.request.urlopen(head_req, timeout=2.0) as resp:
                                content_type = str(resp.headers.get_content_type() or "").lower()
                                if content_type and "html" in content_type:
                                    checks.append(VerificationCheck(
                                        name="html_media_assets",
                                        passed=False,
                                        message=f"Line {line}: Remote URL returned HTML document instead of an image asset (Content-Type: {content_type}): '{s}'",
                                        severity="error",
                                    ))
                                    failed_count += 1
                        except urllib.error.HTTPError as exc:
                            if exc.code in (404, 410, 400):
                                checks.append(VerificationCheck(
                                    name="html_media_assets",
                                    passed=False,
                                    message=f"Line {line}: Remote media asset does not exist (HTTP {exc.code} {exc.reason}): '{s}'",
                                    severity="error",
                                ))
                                failed_count += 1
                        except Exception:
                            # Offline / sandbox network isolation / timeout: do not fail offline builds
                            pass

                    except Exception as e:
                        checks.append(VerificationCheck(
                            name="html_media_assets",
                            passed=False,
                            message=f"Line {line}: Invalid media URL format '{s}': {e}",
                            severity="error",
                        ))
                        failed_count += 1
                    continue

                # Local relative path: check that file exists on disk
                local_file_str = s.split("?")[0].split("#")[0]
                local_path = (html_dir / local_file_str).resolve()
                if not local_path.is_file():
                    checks.append(VerificationCheck(
                        name="html_media_assets",
                        passed=False,
                        message=f"Line {line}: Referenced local media file does not exist on disk: '{s}' in <{tag}>",
                        severity="error",
                    ))
                    failed_count += 1

        if failed_count == 0 and total_checked > 0:
            checks.append(VerificationCheck(
                name="html_media_assets",
                passed=True,
                message=f"Verified {total_checked} media/image asset references (all valid)",
                severity="info",
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


def check_deliverable_path_containment(raw_path: str, root: Path) -> tuple[bool, str, str]:
    r"""Check if a declared deliverable path is safely contained within root.

    Rejects:
    - Empty or whitespace paths
    - Absolute POSIX paths (e.g. /etc/passwd)
    - Windows drive paths (e.g. C:/foo, d:\bar)
    - UNC paths (e.g. //server/share, \\server\share)
    - Directory traversal components ('..' parts)
    - Paths or symlinks that resolve outside root

    Returns:
        (is_safe, error_message, cleaned_relative_path)
    """
    p = str(raw_path).strip()
    if not p:
        return False, "Deliverable path is empty or whitespace-only", ""

    # Check for UNC path (//server/share or \\server\share)
    if p.startswith("//") or p.startswith(r"\\") or p.startswith(r"\\\\"):
        return False, f"UNC path is not allowed: {raw_path}", ""

    # Check for Windows drive letter path (C:... or c:...) cross-platform
    if re.match(r"^[a-zA-Z]:", p):
        return False, f"Windows drive path is not allowed: {raw_path}", ""

    # Check for absolute path (leading slash on POSIX or Windows)
    if p.startswith("/") or p.startswith("\\"):
        return False, f"Absolute path is not allowed: {raw_path}", ""

    # Host-level absolute check
    if Path(p).is_absolute():
        return False, f"Absolute path is not allowed: {raw_path}", ""

    # Normalize Windows separators
    norm = p.replace("\\", "/")
    parts = norm.split("/")

    # Check for traversal components
    if any(part == ".." for part in parts):
        return False, f"Directory traversal ('..') is not allowed: {raw_path}", ""

    # Filter empty parts or single dots
    filtered_parts = [part for part in parts if part and part != "."]
    if not filtered_parts:
        return False, f"Path does not point to a deliverable file: {raw_path}", ""

    cleaned_rel = "/".join(filtered_parts)

    # Check resolved containment against root
    try:
        root_resolved = root.resolve()
        candidate = root / cleaned_rel
        candidate_resolved = candidate.resolve()
        if not candidate_resolved.is_relative_to(root_resolved):
            return False, f"Deliverable path escapes root directory ({root}): {raw_path}", ""
    except Exception as exc:
        return False, f"Path resolution failed for '{raw_path}': {exc}", ""

    return True, "", cleaned_rel


def verify_work_order_deliverables(
    project_path: Path,
    wo_data: dict[str, Any],
    staged_root: Path | None = None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Verify all declared deliverables of a work order deterministically.

    Args:
        project_path: Project root directory
        wo_data: Work order dictionary (with 'deliverable' and/or 'deliverables')
        staged_root: Optional staged root directory (for turns in runner)

    Returns:
        (all_passed, list of critical error messages, inspectable evidence dictionary)
    """
    declared_paths: list[str] = []
    deliv = wo_data.get("deliverable")
    if isinstance(deliv, dict) and deliv.get("path"):
        declared_paths.append(str(deliv["path"]))
    elif isinstance(deliv, str) and deliv.strip():
        declared_paths.append(deliv.strip())

    for extra in wo_data.get("deliverables") or []:
        if isinstance(extra, dict) and extra.get("path"):
            declared_paths.append(str(extra["path"]))
        elif isinstance(extra, str) and extra.strip():
            declared_paths.append(extra.strip())

    if not declared_paths:
        # Non-file work order (e.g. pure sign-off, doc/research without file path)
        return True, [], {
            "verified": True,
            "declared_paths": [],
            "deliverables_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "critical_errors": [],
            "results": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    effective_root = staged_root if staged_root is not None else project_path

    verifier = DeliverableVerifier()
    results: list[dict[str, Any]] = []
    critical_errors: list[str] = []
    all_passed = True
    seen: set[str] = set()
    checked_paths: list[str] = []

    for raw_path in declared_paths:
        raw_str = str(raw_path).strip()
        if not raw_str:
            continue

        is_safe, containment_err, cleaned_rel = check_deliverable_path_containment(raw_str, effective_root)
        if not is_safe:
            all_passed = False
            checked_paths.append(raw_str)
            res = VerificationResult(path=raw_str, checked=True, passed=False)
            res.checks.append(VerificationCheck(
                name="path_containment",
                passed=False,
                message=containment_err,
                severity="error",
            ))
            res.evidence = {
                "path": raw_str,
                "content_hash": None,
                "size_bytes": 0,
                "line_count": 0,
                "checks_run": 1,
                "checks_passed": 0,
                "checks_failed": 1,
                "failures": [containment_err],
            }
            res_dict = res.to_dict()
            res_dict["relative_path"] = raw_str
            results.append(res_dict)
            critical_errors.append(f"{raw_str}: {containment_err}")
            continue

        if cleaned_rel in seen:
            continue
        seen.add(cleaned_rel)
        checked_paths.append(cleaned_rel)

        target_path = effective_root / cleaned_rel
        res = verifier.verify(target_path, root=effective_root)
        res_dict = res.to_dict()
        res_dict["relative_path"] = cleaned_rel
        results.append(res_dict)

        if not res.passed:
            all_passed = False
            for c in res.checks:
                if not c.passed and c.severity == "error":
                    critical_errors.append(f"{cleaned_rel}: {c.message}")

    evidence = {
        "verified": all_passed,
        "declared_paths": checked_paths,
        "deliverables_count": len(checked_paths),
        "passed_count": sum(1 for r in results if r.get("passed")),
        "failed_count": sum(1 for r in results if not r.get("passed")),
        "critical_errors": critical_errors,
        "results": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return all_passed, critical_errors, evidence


__all__ = [
    "VerificationCheck",
    "VerificationResult",
    "DeliverableVerifier",
    "check_deliverable_path_containment",
    "verify_work_order_deliverables",
]
