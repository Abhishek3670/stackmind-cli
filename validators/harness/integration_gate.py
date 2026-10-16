"""Integration Review Gate: Deterministic evidence-based validation before release progression.

Ensures integration approval depends on deterministic, inspectable evidence:
1. All declared deliverables pass content verification and content hashes match current disk state (no stale evidence).
2. Mandatory test executions are recorded and exit codes are 0 (no failed tests allowed, no fabrication).
3. Mandatory build steps are recorded when project defines build configuration (no build failures).
4. Cross-artifact route / API contracts are consistent between frontend and backend.
5. Inapplicable checks are explicitly recorded as skipped with rationale rather than falsely marked as passed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from validators.harness.deliverable_verifier import verify_work_order_deliverables


@dataclass(frozen=True)
class IntegrationCheck:
    """Result of a single deterministic integration check."""

    name: str
    passed: bool
    status: str  # "passed", "failed", "skipped"
    message: str
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
            "message": self.message,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class IntegrationGateDecision:
    """Overall outcome of the deterministic integration gate."""

    passed: bool
    issues: tuple[str, ...] = ()
    blocker_details: tuple[dict[str, str], ...] = ()
    checks: tuple[IntegrationCheck, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": list(self.issues),
            "blocker_details": list(self.blocker_details),
            "checks": [c.to_dict() for c in self.checks],
            "evidence": self.evidence,
            "timestamp": self.timestamp,
        }


class IntegrationGate:
    """Deterministic integration review gate evaluating empirical telemetry and artifacts."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    def evaluate(
        self,
        state: Any = None,
        integration_op: dict[str, Any] | None = None,
        extra_telemetry: list[dict[str, Any]] | None = None,
    ) -> IntegrationGateDecision:
        """Run all deterministic integration checks.

        Returns an IntegrationGateDecision indicating whether the project satisfies
        all mandatory evidence requirements for release readiness.
        """
        checks: list[IntegrationCheck] = []
        issues: list[str] = []
        blocker_details: list[dict[str, str]] = []
        evidence_dict: dict[str, Any] = {
            "workspace": str(self.workspace),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1. Artifact Verification & Freshness
        art_check, art_issues, art_blockers, art_evidence = self._check_artifacts_and_freshness(
            state, integration_op, extra_telemetry
        )
        checks.append(art_check)
        issues.extend(art_issues)
        blocker_details.extend(art_blockers)
        evidence_dict["artifacts"] = art_evidence

        # 2. Test Execution & Result Integrity
        test_check, test_issues, test_blockers, test_evidence = self._check_test_execution_integrity(
            state, integration_op, extra_telemetry
        )
        checks.append(test_check)
        issues.extend(test_issues)
        blocker_details.extend(test_blockers)
        evidence_dict["tests"] = test_evidence

        # 3. Build Execution Integrity
        build_check, build_issues, build_blockers, build_evidence = self._check_build_execution_integrity(
            state, integration_op, extra_telemetry
        )
        checks.append(build_check)
        issues.extend(build_issues)
        blocker_details.extend(build_blockers)
        evidence_dict["build"] = build_evidence

        # 4. Cross-Artifact Route / API Contract Consistency
        route_check, route_issues, route_blockers, route_evidence = self._check_route_api_consistency()
        checks.append(route_check)
        issues.extend(route_issues)
        blocker_details.extend(route_blockers)
        evidence_dict["routes"] = route_evidence

        # 5. Anti-Fabrication Rule: LLM claim vs Deterministic reality
        op_res = (integration_op or {}).get("result") or {}
        llm_claimed_completed = str(op_res.get("status", "")).lower() == "completed"
        deterministic_failed = any(not c.passed and c.status == "failed" for c in checks)

        if llm_claimed_completed and deterministic_failed:
            fab_msg = "Fabricated LLM review claim: review reported completed status, but mandatory deterministic checks failed"
            issues.append(fab_msg)
            blocker_details.append({
                "finding": fab_msg,
                "remediation": "Resolve all deterministic build, test, and route failures before approval",
                "path": "PLAN.md",
            })
            checks.append(IntegrationCheck(
                name="anti_fabrication",
                passed=False,
                status="failed",
                message=fab_msg,
                reason="LLM claimed completed despite deterministic gate failures",
            ))
        else:
            checks.append(IntegrationCheck(
                name="anti_fabrication",
                passed=True,
                status="passed",
                message="Review claims conform to deterministic evidence",
            ))

        overall_passed = not any(not c.passed and c.status == "failed" for c in checks)
        evidence_dict["passed"] = overall_passed
        evidence_dict["issues_count"] = len(issues)
        evidence_dict["skipped_checks"] = [c.to_dict() for c in checks if c.status == "skipped"]

        return IntegrationGateDecision(
            passed=overall_passed,
            issues=tuple(issues),
            blocker_details=tuple(blocker_details),
            checks=tuple(checks),
            evidence=evidence_dict,
        )

    # ── Sub-checks ────────────────────────────────────────────────────────────

    def _check_artifacts_and_freshness(
        self,
        state: Any,
        integration_op: dict[str, Any] | None,
        extra_telemetry: list[dict[str, Any]] | None,
    ) -> tuple[IntegrationCheck, list[str], list[dict[str, str]], dict[str, Any]]:
        """Verify all declared deliverables exist, pass content verification, and hashes match."""
        issues: list[str] = []
        blockers: list[dict[str, str]] = []
        deliverables_evidence: list[dict[str, Any]] = []

        # Discover all work orders in COMPLETED or ACTIVE, avoiding duplicates
        wo_map: dict[str, Path] = {}
        for sub in ("COMPLETED", "ACTIVE"):
            d = self.workspace / ".sync" / "work-orders" / sub
            if d.is_dir():
                for p in sorted(d.glob("*.yaml")):
                    wo_map.setdefault(p.stem, p)

        wo_files = list(wo_map.values())

        if not wo_files:
            return (
                IntegrationCheck(
                    name="artifacts_verification",
                    passed=True,
                    status="skipped",
                    message="No work orders found in workspace",
                    reason="Empty workspace",
                ),
                [],
                [],
                {"deliverables": []},
            )

        excluded_ids = {"WO-000"}
        if state:
            for attr in ("planning_wo_id", "integration_wo_id", "gitops_wo_id"):
                v = getattr(state, attr, None)
                if v:
                    excluded_ids.add(v)
            for s_id in getattr(state, "superseded_wo_ids", []):
                excluded_ids.add(s_id)

        # Collect baseline evidence hashes from telemetry
        recorded_hashes: dict[str, str] = {}
        telemetry_pool: list[dict[str, Any]] = []
        if integration_op and isinstance(integration_op.get("result"), dict):
            telemetry_pool.append(integration_op["result"])
        if extra_telemetry:
            telemetry_pool.extend(extra_telemetry)

        for tel in telemetry_pool:
            v_ev = tel.get("verification_evidence") or {}
            for res in v_ev.get("results") or []:
                rel_p = res.get("relative_path")
                c_hash = res.get("content_hash")
                if rel_p and c_hash:
                    recorded_hashes[rel_p.replace("\\", "/")] = c_hash
            # Also inspect recorded deliverable_hashes if explicitly provided
            deliv_hashes = tel.get("deliverable_hashes") or {}
            for rel_p, c_hash in deliv_hashes.items():
                if rel_p and c_hash:
                    recorded_hashes[str(rel_p).replace("\\", "/")] = str(c_hash)

        all_verified = True
        checked_count = 0

        for wo_file in wo_files:
            try:
                wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8")) or {}
            except Exception:
                continue

            wo_id = wo_data.get("id", wo_file.stem)
            if wo_id in excluded_ids:
                continue

            # Skip pure sign-off doc deliverables without declared files
            deliv = wo_data.get("deliverable") or {}
            delivs = wo_data.get("deliverables") or []
            if not deliv.get("path") and not any(d.get("path") for d in delivs if isinstance(d, dict)):
                continue

            passed, errs, ev = verify_work_order_deliverables(self.workspace, wo_data)
            checked_count += 1

            if not passed:
                all_verified = False
                for err in errs:
                    issues.append(f"{wo_id}: {err}")
                    blockers.append({
                        "finding": f"Deliverable verification failed: {err}",
                        "remediation": "Re-author deliverable to satisfy existence and content verification",
                        "path": str(deliv.get("path") or ""),
                    })

            # Check freshness against recorded hashes
            for item in ev.get("results") or []:
                rel_path = item.get("relative_path")
                curr_hash = item.get("content_hash")
                deliverables_evidence.append({
                    "work_order_id": wo_id,
                    "path": rel_path,
                    "passed": item.get("passed", False),
                    "content_hash": curr_hash,
                })
                norm_p = str(rel_path).replace("\\", "/")
                if norm_p in recorded_hashes and curr_hash:
                    rec_hash = str(recorded_hashes[norm_p])
                    hashes_match = (
                        rec_hash == curr_hash
                        or (len(curr_hash) == 16 and rec_hash.startswith(curr_hash))
                        or (len(rec_hash) == 16 and curr_hash.startswith(rec_hash))
                    )
                    if not hashes_match:
                        all_verified = False
                        stale_msg = (
                            f"Stale evidence for '{rel_path}': file was modified after test/review evidence "
                            f"was recorded (recorded hash {rec_hash}, current hash {curr_hash})"
                        )
                        issues.append(stale_msg)
                        blockers.append({
                            "finding": stale_msg,
                            "remediation": f"Re-execute tests and verification against updated {rel_path}",
                            "path": rel_path,
                        })

        if not all_verified:
            return (
                IntegrationCheck(
                    name="artifacts_verification",
                    passed=False,
                    status="failed",
                    message=f"Artifact verification failed: {len(issues)} issue(s) detected",
                    evidence={"checked_count": checked_count, "deliverables": deliverables_evidence},
                ),
                issues,
                blockers,
                {"checked_count": checked_count, "deliverables": deliverables_evidence},
            )

        return (
            IntegrationCheck(
                name="artifacts_verification",
                passed=True,
                status="passed",
                message=f"All {checked_count} deliverable artifact(s) verified and fresh",
                evidence={"checked_count": checked_count, "deliverables": deliverables_evidence},
            ),
            [],
            [],
            {"checked_count": checked_count, "deliverables": deliverables_evidence},
        )

    def _check_test_execution_integrity(
        self,
        state: Any,
        integration_op: dict[str, Any] | None,
        extra_telemetry: list[dict[str, Any]] | None,
    ) -> tuple[IntegrationCheck, list[str], list[dict[str, str]], dict[str, Any]]:
        """Verify that required test suites have recorded successful test executions."""
        issues: list[str] = []
        blockers: list[dict[str, str]] = []

        # Find test files
        test_files: list[Path] = []
        tests_dir = self.workspace / "tests"
        if tests_dir.is_dir():
            test_files.extend([p for p in tests_dir.rglob("*.py") if p.is_file()])
            test_files.extend([p for p in tests_dir.rglob("*.test.*") if p.is_file()])
            test_files.extend([p for p in tests_dir.rglob("*.spec.*") if p.is_file()])

        test_dir_alt = self.workspace / "test"
        if test_dir_alt.is_dir():
            test_files.extend([p for p in test_dir_alt.rglob("*.py") if p.is_file()])

        # Also search workspace root for test files
        for ext in ("*.py", "*.test.js", "*.test.ts"):
            test_files.extend([p for p in self.workspace.glob(f"test_{ext}") if p.is_file()])

        # Deduplicate
        unique_test_files = sorted(set(test_files))

        if not unique_test_files:
            return (
                IntegrationCheck(
                    name="test_execution",
                    passed=True,
                    status="skipped",
                    message="No test suites detected in workspace",
                    reason="No test files found",
                ),
                [],
                [],
                {"test_files": [], "runs": []},
            )

        # Collect command audits from integration op and telemetry
        audit_entries: list[dict[str, Any]] = []
        if integration_op and isinstance(integration_op.get("result"), dict):
            for cmd in integration_op["result"].get("commands_audit") or []:
                if isinstance(cmd, dict):
                    audit_entries.append(cmd)
            for call in integration_op["result"].get("tool_calls_audit") or []:
                if isinstance(call, dict):
                    audit_entries.append(call)
        if extra_telemetry:
            for tel in extra_telemetry:
                for cmd in tel.get("commands_audit") or []:
                    if isinstance(cmd, dict):
                        audit_entries.append(cmd)
                for call in tel.get("tool_calls_audit") or []:
                    if isinstance(call, dict):
                        audit_entries.append(call)

        # Check for test execution records using anchored command parsing
        test_runs: list[dict[str, Any]] = []
        for entry in audit_entries:
            cmd_str, rc = self._extract_command_info(entry)
            tokens = self._parse_command_tokens(cmd_str)
            tool_name = entry.get("tool") or entry.get("name")
            if self._is_test_command(tokens) or tool_name in ("run_tests", "pytest", "test"):
                test_runs.append({"command": cmd_str or str(tool_name), "tokens": tokens, "returncode": rc})

        rel_test_paths = [p.relative_to(self.workspace).as_posix() for p in unique_test_files]

        if not test_runs:
            msg = f"Missing mandatory test execution evidence: test suite(s) exist ({', '.join(rel_test_paths[:3])}) but no test execution was recorded"
            issues.append(msg)
            blockers.append({
                "finding": msg,
                "remediation": "Execute test suite using run_tests or pytest and capture telemetry",
                "path": rel_test_paths[0],
            })
            return (
                IntegrationCheck(
                    name="test_execution",
                    passed=False,
                    status="failed",
                    message=msg,
                    evidence={"test_files": rel_test_paths, "runs": []},
                ),
                issues,
                blockers,
                {"test_files": rel_test_paths, "runs": []},
            )

        # Retain the latest execution for each distinct test suite/command so that
        # successful rework re-executions supersede earlier failed attempts without
        # dropping distinct test suites that all must pass.
        latest_runs: dict[str, dict[str, Any]] = {}
        for r in test_runs:
            norm_key = " ".join(r["tokens"]) if r["tokens"] else r["command"]
            latest_runs[norm_key] = r

        evaluated_runs = list(latest_runs.values())
        failed_runs = [r for r in evaluated_runs if r.get("returncode", 0) != 0]
        if failed_runs:
            for r in failed_runs:
                msg = f"Deterministic test failure: command '{r.get('command')}' failed with exit code {r.get('returncode')}"
                issues.append(msg)
                blockers.append({
                    "finding": msg,
                    "remediation": "Fix failing tests or code under test",
                    "path": rel_test_paths[0],
                })
            return (
                IntegrationCheck(
                    name="test_execution",
                    passed=False,
                    status="failed",
                    message=f"Test execution failed: {len(failed_runs)} command(s) returned non-zero exit code",
                    evidence={"test_files": rel_test_paths, "runs": evaluated_runs},
                ),
                issues,
                blockers,
                {"test_files": rel_test_paths, "runs": evaluated_runs},
            )

        return (
            IntegrationCheck(
                name="test_execution",
                passed=True,
                status="passed",
                message=f"Recorded {len(evaluated_runs)} successful test execution(s) with exit code 0",
                evidence={"test_files": rel_test_paths, "runs": evaluated_runs},
            ),
            [],
            [],
            {"test_files": rel_test_paths, "runs": evaluated_runs},
        )

    def _check_build_execution_integrity(
        self,
        state: Any,
        integration_op: dict[str, Any] | None,
        extra_telemetry: list[dict[str, Any]] | None,
    ) -> tuple[IntegrationCheck, list[str], list[dict[str, str]], dict[str, Any]]:
        """Verify that mandatory build commands succeeded when project defines build configuration."""
        issues: list[str] = []
        blockers: list[dict[str, str]] = []

        # Detect build requirements
        build_manifest: str | None = None
        has_build_script = False

        pkg_json = self.workspace / "package.json"
        if pkg_json.is_file():
            try:
                pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
                if isinstance(pkg_data, dict) and "build" in (pkg_data.get("scripts") or {}):
                    build_manifest = "package.json"
                    has_build_script = True
            except Exception:
                pass

        if not has_build_script:
            makefile = self.workspace / "Makefile"
            if makefile.is_file():
                try:
                    content = makefile.read_text(encoding="utf-8")
                    if re.search(r"(?m)^build\s*:", content):
                        build_manifest = "Makefile"
                        has_build_script = True
                except Exception:
                    pass

        if not has_build_script:
            return (
                IntegrationCheck(
                    name="build_execution",
                    passed=True,
                    status="skipped",
                    message="No build configuration or build script detected in project",
                    reason="No build manifest or build script declared",
                ),
                [],
                [],
                {"has_build_script": False},
            )

        # Collect command audits
        audit_entries: list[dict[str, Any]] = []
        if integration_op and isinstance(integration_op.get("result"), dict):
            for cmd in integration_op["result"].get("commands_audit") or []:
                if isinstance(cmd, dict):
                    audit_entries.append(cmd)
            for call in integration_op["result"].get("tool_calls_audit") or []:
                if isinstance(call, dict):
                    audit_entries.append(call)
        if extra_telemetry:
            for tel in extra_telemetry:
                for cmd in tel.get("commands_audit") or []:
                    if isinstance(cmd, dict):
                        audit_entries.append(cmd)
                for call in tel.get("tool_calls_audit") or []:
                    if isinstance(call, dict):
                        audit_entries.append(call)

        build_runs: list[dict[str, Any]] = []
        for entry in audit_entries:
            cmd_str, rc = self._extract_command_info(entry)
            tokens = self._parse_command_tokens(cmd_str)
            if self._is_build_command(tokens):
                build_runs.append({"command": cmd_str, "tokens": tokens, "returncode": rc})

        if not build_runs:
            msg = f"Missing mandatory build evidence: project defines build in {build_manifest} but no build execution was recorded"
            issues.append(msg)
            blockers.append({
                "finding": msg,
                "remediation": f"Execute build script defined in {build_manifest} and record telemetry",
                "path": build_manifest,
            })
            return (
                IntegrationCheck(
                    name="build_execution",
                    passed=False,
                    status="failed",
                    message=msg,
                    evidence={"manifest": build_manifest, "runs": []},
                ),
                issues,
                blockers,
                {"manifest": build_manifest, "runs": []},
            )

        latest_builds: dict[str, dict[str, Any]] = {}
        for r in build_runs:
            norm_key = " ".join(r["tokens"]) if r["tokens"] else r["command"]
            latest_builds[norm_key] = r

        evaluated_builds = list(latest_builds.values())
        failed_builds = [r for r in evaluated_builds if r.get("returncode", 0) != 0]
        if failed_builds:
            for r in failed_builds:
                msg = f"Deterministic build failure: command '{r.get('command')}' failed with exit code {r.get('returncode')}"
                issues.append(msg)
                blockers.append({
                    "finding": msg,
                    "remediation": "Fix compilation or bundling errors in build",
                    "path": build_manifest,
                })
            return (
                IntegrationCheck(
                    name="build_execution",
                    passed=False,
                    status="failed",
                    message=f"Build execution failed: {len(failed_builds)} command(s) returned non-zero exit code",
                    evidence={"manifest": build_manifest, "runs": evaluated_builds},
                ),
                issues,
                blockers,
                {"manifest": build_manifest, "runs": evaluated_builds},
            )

        return (
            IntegrationCheck(
                name="build_execution",
                passed=True,
                status="passed",
                message=f"Recorded successful build execution with exit code 0",
                evidence={"manifest": build_manifest, "runs": evaluated_builds},
            ),
            [],
            [],
            {"manifest": build_manifest, "runs": evaluated_builds},
        )

    def _check_route_api_consistency(
        self,
    ) -> tuple[IntegrationCheck, list[str], list[dict[str, str]], dict[str, Any]]:
        """Verify that frontend API calls match declared backend routes, including HTTP methods."""
        issues: list[str] = []
        blockers: list[dict[str, str]] = []

        backend_routes: list[dict[str, Any]] = []
        frontend_calls: list[dict[str, Any]] = []

        # 1. Scan backend routes
        for p in self.workspace.rglob("*.py"):
            if any(part in p.parts for part in (".git", ".sync", "__pycache__", "tests", "test", ".venv", "venv")):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                rel_f = p.relative_to(self.workspace).as_posix()

                # Specific HTTP method decorators: @app.get('/path'), @router.post('/path')
                method_matches = re.findall(
                    r"""@(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['"](/[^'"]*)['"]""",
                    text,
                    re.IGNORECASE,
                )
                for meth, path in method_matches:
                    backend_routes.append({
                        "path": self._normalize_route(path),
                        "methods": {meth.upper()},
                        "file": rel_f,
                        "raw_route": path,
                    })

                # Flask @app.route('/path', methods=['GET', 'POST'])
                route_matches = re.finditer(
                    r"""@(?:app|router)\.route\s*\(\s*['"](/[^'"]*)['"]([^)]*)\)""",
                    text,
                )
                for rm in route_matches:
                    path = rm.group(1)
                    args = rm.group(2)
                    methods = {"GET"}
                    meth_arg = re.search(r"""methods\s*=\s*\[([^\]]*)\]""", args, re.IGNORECASE)
                    if meth_arg:
                        found_methods = re.findall(r"""['"]([a-zA-Z]+)['"]""", meth_arg.group(1))
                        if found_methods:
                            methods = {m.upper() for m in found_methods}
                    backend_routes.append({
                        "path": self._normalize_route(path),
                        "methods": methods,
                        "file": rel_f,
                        "raw_route": path,
                    })

                # Flask-RESTful api.add_resource(ResourceClass, '/path')
                restful_matches = re.findall(
                    r"""api\.add_resource\s*\(\s*[a-zA-Z0-9_]+\s*,\s*['"](/[^'"]*)['"]""",
                    text,
                )
                for path in restful_matches:
                    backend_routes.append({
                        "path": self._normalize_route(path),
                        "methods": {"*"},
                        "file": rel_f,
                        "raw_route": path,
                    })
            except Exception:
                pass

        # Also scan JS/TS backend files
        for p in list(self.workspace.rglob("*.js")) + list(self.workspace.rglob("*.ts")):
            if any(part in p.parts for part in (".git", ".sync", "node_modules", "tests", "test", "public", "static", "dist")):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                rel_f = p.relative_to(self.workspace).as_posix()

                # app.get('/path'), router.post('/path')
                matches = re.findall(
                    r"""(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['"](/[^'"]*)['"]""",
                    text,
                    re.IGNORECASE,
                )
                for meth, path in matches:
                    backend_routes.append({
                        "path": self._normalize_route(path),
                        "methods": {meth.upper()},
                        "file": rel_f,
                        "raw_route": path,
                    })

                # app.all('/path'), app.use('/path')
                wildcard_matches = re.findall(
                    r"""(?:app|router)\.(all|use)\s*\(\s*['"](/[^'"]*)['"]""",
                    text,
                    re.IGNORECASE,
                )
                for _, path in wildcard_matches:
                    backend_routes.append({
                        "path": self._normalize_route(path),
                        "methods": {"*"},
                        "file": rel_f,
                        "raw_route": path,
                    })
            except Exception:
                pass

        # 2. Scan frontend API calls
        fe_exts = ("*.html", "*.htm", "*.js", "*.jsx", "*.ts", "*.tsx")
        for ext in fe_exts:
            for p in self.workspace.rglob(ext):
                if any(part in p.parts for part in (".git", ".sync", "node_modules", "tests", "test", "__pycache__")):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    rel_f = p.relative_to(self.workspace).as_posix()

                    # axios.get('/api/...'), axios.post('/api/...')
                    axios_meth = re.findall(
                        r"""axios\.(get|post|put|delete|patch)\s*\(\s*['"`](/api/[^'"`?#\s]+)""",
                        text,
                        re.IGNORECASE,
                    )
                    for meth, path in axios_meth:
                        frontend_calls.append({
                            "path": self._normalize_route(path),
                            "method": meth.upper(),
                            "file": rel_f,
                            "raw_endpoint": path,
                        })

                    # $.get('/api/...'), $.post('/api/...')
                    jq_meth = re.findall(
                        r"""\$\.(get|post)\s*\(\s*['"`](/api/[^'"`?#\s]+)""",
                        text,
                        re.IGNORECASE,
                    )
                    for meth, path in jq_meth:
                        frontend_calls.append({
                            "path": self._normalize_route(path),
                            "method": meth.upper(),
                            "file": rel_f,
                            "raw_endpoint": path,
                        })

                    # Form action="/api/..."
                    for form_match in re.finditer(r"""<form([^>]+action\s*=\s*['"](/api/[^'"?#\s]+)['"][^>]*)>""", text, re.IGNORECASE):
                        attrs = form_match.group(1)
                        path = form_match.group(2)
                        meth = "GET"
                        m = re.search(r"""method\s*=\s*['"]([a-zA-Z]+)['"]""", attrs, re.IGNORECASE)
                        if m:
                            meth = m.group(1).upper()
                        frontend_calls.append({
                            "path": self._normalize_route(path),
                            "method": meth,
                            "file": rel_f,
                            "raw_endpoint": path,
                        })

                    # fetch('/api/...', { method: 'POST' }) or fetch('/api/...')
                    for fetch_match in re.finditer(
                        r"""fetch\s*\(\s*['"`](/api/[^'"`?#\s]+)['"`](?:\s*,\s*\{([^}]*)\})?""",
                        text,
                    ):
                        path = fetch_match.group(1)
                        opts = fetch_match.group(2) or ""
                        meth = "GET"
                        m = re.search(r"""method\s*:\s*['"]([a-zA-Z]+)['"]""", opts, re.IGNORECASE)
                        if m:
                            meth = m.group(1).upper()
                        frontend_calls.append({
                            "path": self._normalize_route(path),
                            "method": meth,
                            "file": rel_f,
                            "raw_endpoint": path,
                        })

                    # axios('/api/...') generic or $.ajax({url: '/api/...'})
                    generic_axios = re.findall(
                        r"""axios\s*\(\s*['"`](/api/[^'"`?#\s]+)['"`]""",
                        text,
                    )
                    for path in generic_axios:
                        frontend_calls.append({
                            "path": self._normalize_route(path),
                            "method": "*",
                            "file": rel_f,
                            "raw_endpoint": path,
                        })
                except Exception:
                    pass

        if not frontend_calls or not backend_routes:
            return (
                IntegrationCheck(
                    name="route_consistency",
                    passed=True,
                    status="skipped",
                    message="No cross-artifact frontend-to-backend API routes detected",
                    reason="No route pair detected in workspace",
                ),
                [],
                [],
                {"backend_routes": [r["raw_route"] for r in backend_routes], "frontend_calls": []},
            )

        # Match each frontend call against backend routes
        for fe in frontend_calls:
            matching_backend = [
                be for be in backend_routes
                if self._route_matches(fe["path"], [be["path"]])
            ]
            if not matching_backend:
                msg = f"Route contract mismatch: frontend '{fe['file']}' calls endpoint '{fe['raw_endpoint']}', but backend does not declare a matching route"
                issues.append(msg)
                blockers.append({
                    "finding": msg,
                    "remediation": f"Declare endpoint '{fe['raw_endpoint']}' in backend route definitions or update frontend call in {fe['file']}",
                    "path": fe["file"],
                })
            else:
                # Check method compatibility
                all_supported_methods: set[str] = set()
                for be in matching_backend:
                    all_supported_methods.update(be["methods"])

                fe_meth = fe["method"]
                if fe_meth != "*" and "*" not in all_supported_methods and fe_meth not in all_supported_methods:
                    sorted_methods = sorted(all_supported_methods)
                    msg = (
                        f"Route method mismatch: frontend '{fe['file']}' calls {fe_meth} '{fe['raw_endpoint']}', "
                        f"but backend route only supports {sorted_methods}"
                    )
                    issues.append(msg)
                    blockers.append({
                        "finding": msg,
                        "remediation": f"Add {fe_meth} handler for '{fe['raw_endpoint']}' in backend or adjust frontend request method in {fe['file']}",
                        "path": fe["file"],
                    })

        if issues:
            return (
                IntegrationCheck(
                    name="route_consistency",
                    passed=False,
                    status="failed",
                    message=f"Route contract consistency check failed: {len(issues)} issue(s) detected",
                    evidence={
                        "backend_routes": [f"{sorted(r['methods'])} {r['path']}" for r in backend_routes],
                        "frontend_calls": [f"{c['method']} {c['raw_endpoint']}" for c in frontend_calls],
                        "issues": issues,
                    },
                ),
                issues,
                blockers,
                {
                    "backend_routes": [f"{sorted(r['methods'])} {r['path']}" for r in backend_routes],
                    "frontend_calls": [f"{c['method']} {c['raw_endpoint']}" for c in frontend_calls],
                    "issues": issues,
                },
            )

        return (
            IntegrationCheck(
                name="route_consistency",
                passed=True,
                status="passed",
                message=f"All {len(frontend_calls)} frontend API call(s) match declared backend routes and methods",
                evidence={
                    "backend_routes": [f"{sorted(r['methods'])} {r['path']}" for r in backend_routes],
                    "matched_calls": [f"{c['method']} {c['raw_endpoint']}" for c in frontend_calls],
                },
            ),
            [],
            [],
            {
                "backend_routes": [f"{sorted(r['methods'])} {r['path']}" for r in backend_routes],
                "matched_calls": [f"{c['method']} {c['raw_endpoint']}" for c in frontend_calls],
            },
        )

    @staticmethod
    def _normalize_route(route: str) -> str:
        """Normalize endpoint route path for consistent comparison."""
        r = str(route).strip().split("?")[0].split("#")[0].rstrip("/")
        if not r.startswith("/"):
            r = "/" + r
        # Replace variable segments <param>, {param}, :param with wildcard segment
        r = re.sub(r"/<[^>]+>", "/{param}", r)
        r = re.sub(r"/\{[^}]+\}", "/{param}", r)
        r = re.sub(r"/:[a-zA-Z0-9_]+", "/{param}", r)
        return r

    @staticmethod
    def _route_matches(call: str, declared_routes: list[str]) -> bool:
        """Check if a normalized frontend call matches any declared backend route pattern."""
        call_parts = [p for p in call.split("/") if p]
        for declared in declared_routes:
            decl_parts = [p for p in declared.split("/") if p]
            if len(call_parts) != len(decl_parts):
                continue
            match = True
            for cp, dp in zip(call_parts, decl_parts):
                if dp == "{param}":
                    continue
                if cp != dp:
                    match = False
                    break
            if match:
                return True
        return False

    @staticmethod
    def _parse_command_tokens(cmd: str | list[str]) -> list[str]:
        """Parse command string or list into normalized token list, stripping leading env vars and wrappers."""
        if isinstance(cmd, list):
            tokens = [str(t) for t in cmd]
        elif isinstance(cmd, str):
            cmd_clean = cmd.strip()
            if not cmd_clean:
                return []
            try:
                tokens = shlex.split(cmd_clean, posix=False)
            except Exception:
                tokens = cmd_clean.split()
            tokens = [t.strip("\"'") for t in tokens if t.strip("\"'")]
        else:
            return []

        # Strip leading environment variable assignments (e.g. CI=1, FOO=bar) and env wrappers
        idx = 0
        while idx < len(tokens):
            tok = tokens[idx]
            if "=" in tok and not tok.startswith("-"):
                idx += 1
                continue
            if tok.lower() in ("env", "nohup", "time") and idx + 1 < len(tokens):
                idx += 1
                continue
            break
        tokens = tokens[idx:]

        # Handle shell wrappers: cmd.exe /c, powershell -c / -Command
        if tokens:
            exe_stem = Path(tokens[0]).stem.lower()
            if exe_stem in ("cmd",):
                sub = [t for t in tokens[1:] if t.lower() not in ("/c", "/k", "/q")]
                if sub:
                    if len(sub) == 1 and " " in sub[0]:
                        return IntegrationGate._parse_command_tokens(sub[0])
                    tokens = sub
            elif exe_stem in ("powershell", "pwsh"):
                sub = [t for t in tokens[1:] if t.lower() not in ("-c", "-command", "-noprofile", "-noninteractive")]
                if sub:
                    if len(sub) == 1 and " " in sub[0]:
                        return IntegrationGate._parse_command_tokens(sub[0])
                    tokens = sub

        return tokens

    @staticmethod
    def _is_test_command(tokens: list[str]) -> bool:
        """Anchored check whether command tokens represent a test execution invocation."""
        if not tokens:
            return False

        first_tok = Path(tokens[0]).stem.lower()

        # Direct test runner binaries
        direct_runners = {
            "pytest",
            "jest",
            "vitest",
            "mocha",
            "karma",
            "ava",
            "tap",
            "playwright",
            "cypress",
            "run_tests",
            "ctest",
        }
        if first_tok in direct_runners:
            return True

        # Python test module invocations: python -m pytest / unittest
        if first_tok in ("python", "python3", "py"):
            for i in range(1, len(tokens) - 1):
                if tokens[i] == "-m" and tokens[i + 1].lower() in ("pytest", "unittest", "trial"):
                    return True
            return False

        # Package manager test commands: npm test, pnpm test, yarn test, bun test
        if first_tok in ("npm", "pnpm", "yarn", "bun"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub in ("test", "t"):
                    return True
                if sub == "run" and len(tokens) > 2 and tokens[2].lower().startswith("test"):
                    return True
            return False

        # npx / pnpx / bunx test runner invocation: npx jest, npx vitest, npx mocha, npx playwright test
        if first_tok in ("npx", "pnpx", "bunx"):
            if len(tokens) > 1:
                sub = Path(tokens[1]).stem.lower()
                if sub in direct_runners:
                    return True
            return False

        # Language specific test tools: cargo test, go test, dotnet test
        if first_tok in ("cargo", "go", "dotnet") and len(tokens) > 1 and tokens[1].lower() == "test":
            return True

        return False

    @staticmethod
    def _is_build_command(tokens: list[str]) -> bool:
        """Anchored check whether command tokens represent a build execution invocation."""
        if not tokens:
            return False

        first_tok = Path(tokens[0]).stem.lower()

        # Direct build tools
        direct_builders = {
            "webpack",
            "vite",
            "esbuild",
            "rollup",
            "tsc",
            "parcel",
            "turbo",
        }
        if first_tok in direct_builders:
            return True

        # Make
        if first_tok == "make":
            if len(tokens) == 1 or any(t.lower() in ("build", "all", "compile") for t in tokens[1:]):
                return True
            return False

        # Package managers: npm run build, pnpm build, yarn build, bun run build
        if first_tok in ("npm", "pnpm", "yarn", "bun"):
            if len(tokens) > 1:
                sub = tokens[1].lower()
                if sub == "build":
                    return True
                if sub == "run" and len(tokens) > 2 and tokens[2].lower().startswith("build"):
                    return True
            return False

        # npx / pnpx / bunx build invocation
        if first_tok in ("npx", "pnpx", "bunx") and len(tokens) > 1:
            sub = Path(tokens[1]).stem.lower()
            if sub in direct_builders:
                return True
            if sub == "vite" and len(tokens) > 2 and tokens[2].lower() == "build":
                return True
            return False

        # Language specific: cargo build, go build, dotnet build
        if first_tok in ("cargo", "go", "dotnet") and len(tokens) > 1 and tokens[1].lower() == "build":
            return True

        # Python build module: python -m build, python setup.py build
        if first_tok in ("python", "python3", "py"):
            if len(tokens) > 2 and tokens[1] == "-m" and tokens[2].lower() == "build":
                return True
            if len(tokens) > 2 and "setup.py" in tokens[1].lower() and tokens[2].lower() == "build":
                return True
            return False

        return False

    @staticmethod
    def _extract_command_info(entry: dict[str, Any]) -> tuple[str, int]:
        """Extract command string and returncode from a command audit or tool call entry."""
        cmd = ""
        rc = 0
        tool_name = entry.get("tool") or entry.get("name")
        if "command" in entry:
            cmd = str(entry["command"])
            rc = int(entry.get("returncode", entry.get("exit_code", 0)))
        elif "CommandLine" in entry:
            cmd = str(entry["CommandLine"])
            rc = int(entry.get("returncode", entry.get("exit_code", 0)))
        elif tool_name in ("run_command", "bash", "execute_command"):
            args = entry.get("args") or entry.get("arguments") or {}
            if isinstance(args, dict):
                cmd = str(args.get("CommandLine") or args.get("command") or "")
            res = entry.get("result") or {}
            if isinstance(res, dict):
                rc = int(res.get("returncode", res.get("exit_code", 0)))
        elif tool_name in ("run_tests", "pytest", "test"):
            cmd = "run_tests"
            rc = 0 if entry.get("status") != "error" and not entry.get("failed") else 1
        return cmd, rc


__all__ = [
    "IntegrationCheck",
    "IntegrationGateDecision",
    "IntegrationGate",
]
