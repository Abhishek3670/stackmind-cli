"""D024 QA Gate: Programmatic enforcement between implementation and GitOps progression.

Phase E implementation preventing GitOps consumption, commit, or promotion
of work orders until the QA role (Gemma) has issued an explicit APPROVED verdict.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from validators.knowledge.contract import ContractAccessDenied


class D024ViolationError(ContractAccessDenied):
    """Raised when a GitOps transition violates the D024 QA Gate."""


WO_REF_PATTERN = re.compile(r"WO-\d{3}")


@dataclass(frozen=True)
class D024GateDecision:
    """Outcome of evaluating a work order against D024 QA Gate requirements."""

    passed: bool
    work_order_id: str
    target_work_orders: tuple[str, ...]
    verdict_status: str  # "APPROVED", "NEEDS_CHANGES", "BLOCKED", "MISSING", "INCOMPLETE"
    verdict_files: tuple[str, ...] = ()
    deliverable_checked: bool = False
    deliverable_exists: bool = False
    deliverable_path: str | None = None
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "work_order_id": self.work_order_id,
            "target_work_orders": list(self.target_work_orders),
            "verdict_status": self.verdict_status,
            "verdict_files": list(self.verdict_files),
            "deliverable_checked": self.deliverable_checked,
            "deliverable_exists": self.deliverable_exists,
            "deliverable_path": self.deliverable_path,
            "reason": self.reason,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
        }


class D024Gate:
    """Evaluator and gatekeeper for D024 Mandatory QA Gate."""

    RE_REJECT = re.compile(
        r"[*_`]*(?:verdict|status)[*_`\s]*[:=]?[*_`\s]*(NEEDS_CHANGES|REJECTED|FAILED|FAIL)",
        re.IGNORECASE,
    )
    RE_BLOCK = re.compile(
        r"[*_`]*(?:verdict|status)[*_`\s]*[:=]?[*_`\s]*BLOCKED",
        re.IGNORECASE,
    )
    RE_APPROVE = re.compile(
        r"[*_`]*(?:verdict|status)[*_`\s]*[:=]?[*_`\s]*(APPROVED|PASS|ACCEPTED)",
        re.IGNORECASE,
    )
    RE_HEADER_APPROVE = re.compile(
        r"#+\s*(?:.*)?verdict\s*[:=]?\s*(APPROVED|PASS|ACCEPTED)",
        re.IGNORECASE,
    )
    RE_HEADER_REJECT = re.compile(
        r"#+\s*(?:.*)?verdict\s*[:=]?\s*(NEEDS_CHANGES|FAIL|REJECTED)",
        re.IGNORECASE,
    )

    def find_work_order_file(self, project_path: Path, work_order_id: str) -> Path | None:
        """Find work order YAML in ACTIVE, COMPLETED, or BLOCKED directories."""
        sync_path = project_path / ".sync"
        for folder in ("ACTIVE", "COMPLETED", "BLOCKED"):
            candidate = sync_path / "work-orders" / folder / f"{work_order_id}.yaml"
            if candidate.exists():
                return candidate
        return None

    def find_companion_test_file(self, project_path: Path, deliverable_path: str) -> Path | None:
        """Locate at least one companion test file for a code deliverable.

        Searches:
        1. Standard candidate paths: tests/test_<name>.py, tests/<subpath>/test_<name>.py
        2. Glob matching across tests/ for test_<name>*.py or *<name>*test*.py
        3. Content inspection of test files referencing the deliverable module
        """
        deliv_p = Path(deliverable_path)
        stem = deliv_p.stem.lower()
        if stem in ("__init__", "main", "index", "app") and len(deliv_p.parts) > 1:
            alt_stem = deliv_p.parts[-2].lower()
        else:
            alt_stem = stem

        tests_dir = project_path / "tests"
        if not tests_dir.is_dir():
            root_test = project_path / f"test_{stem}.py"
            return root_test if (root_test.is_file() and root_test.stat().st_size > 0) else None

        direct_candidates = [
            tests_dir / f"test_{stem}.py",
            tests_dir / f"{stem}_test.py",
            tests_dir / f"test_{alt_stem}.py",
        ]
        parts = [p for p in deliv_p.parts[:-1] if p not in ("src", "lib", "app")]
        if parts:
            sub_dir = tests_dir.joinpath(*parts)
            direct_candidates.extend([
                sub_dir / f"test_{stem}.py",
                sub_dir / f"{stem}_test.py",
            ])

        for candidate in direct_candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate

        for pattern in (f"test_*{stem}*.py", f"*{stem}*_test.py", f"test_*{alt_stem}*.py"):
            for match in tests_dir.rglob(pattern):
                if match.is_file() and match.stat().st_size > 0:
                    return match

        for test_file in tests_dir.rglob("test_*.py"):
            if test_file.is_file() and test_file.stat().st_size > 0:
                try:
                    content = test_file.read_text(encoding="utf-8", errors="ignore")
                    if stem in content or alt_stem in content:
                        return test_file
                except Exception:
                    continue

        return None

    def resolve_target_work_orders(
        self, project_path: Path, work_order_id: str, wo_data: dict[str, Any]
    ) -> tuple[str, ...]:
        """Determine implementation work orders governed by this work order."""
        assigned = [str(a).lower().strip() for a in wo_data.get("assigned_agents", [])]
        is_gitops = (
            "local-llm" in assigned
            or "gitops" in assigned
            or str(wo_data.get("type", "")).upper() in ("RELEASE", "GITOPS")
        )
        if not is_gitops:
            return (work_order_id,)

        targets: set[str] = set()
        for dep in wo_data.get("dependencies", []):
            if isinstance(dep, str) and WO_REF_PATTERN.match(dep):
                targets.add(dep)

        title = str(wo_data.get("title", ""))
        desc = str(wo_data.get("description", ""))
        for match in WO_REF_PATTERN.findall(f"{title} {desc}"):
            if match != work_order_id:
                targets.add(match)

        for explicit_target in wo_data.get("targets", []):
            if isinstance(explicit_target, str) and WO_REF_PATTERN.match(explicit_target):
                targets.add(explicit_target)

        if not targets:
            return (work_order_id,)
        return tuple(sorted(targets))

    def parse_verdict_file(self, verdict_path: Path) -> tuple[str, dict[str, Any]]:
        """Parse QA verdict file content and return (status, evidence_dict)."""
        content = verdict_path.read_text(encoding="utf-8")
        evidence: dict[str, Any] = {"file": verdict_path.name}

        if verdict_path.suffix in (".yaml", ".yml"):
            try:
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    v = str(data.get("verdict") or data.get("status") or "").upper().strip()
                    evidence.update(data)
                    if v in ("APPROVED", "PASS", "ACCEPTED"):
                        return "APPROVED", evidence
                    if v in ("NEEDS_CHANGES", "REJECTED", "FAIL"):
                        return "NEEDS_CHANGES", evidence
                    if v in ("BLOCKED",):
                        return "BLOCKED", evidence
            except Exception:
                pass

        if verdict_path.suffix == ".json":
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    v = str(data.get("verdict") or data.get("status") or "").upper().strip()
                    evidence.update(data)
                    if v in ("APPROVED", "PASS", "ACCEPTED"):
                        return "APPROVED", evidence
                    if v in ("NEEDS_CHANGES", "REJECTED", "FAIL"):
                        return "NEEDS_CHANGES", evidence
                    if v in ("BLOCKED",):
                        return "BLOCKED", evidence
            except Exception:
                pass

        # Markdown parsing: check rejection and blocking verdicts first
        if self.RE_REJECT.search(content):
            return "NEEDS_CHANGES", evidence
        if self.RE_BLOCK.search(content):
            return "BLOCKED", evidence
        if self.RE_APPROVE.search(content):
            return "APPROVED", evidence

        # Header check: e.g. "# WO-024 Verdict: APPROVED"
        if self.RE_HEADER_APPROVE.search(content):
            return "APPROVED", evidence
        if self.RE_HEADER_REJECT.search(content):
            return "NEEDS_CHANGES", evidence

        return "UNKNOWN", evidence

    def find_qa_verdicts(self, project_path: Path, work_order_id: str) -> list[tuple[Path, str, dict[str, Any]]]:
        """Locate all QA verdict files associated with work_order_id."""
        sync_path = project_path / ".sync"
        search_dirs = [
            sync_path / "inbox" / "claude",
            sync_path / "inbox" / "claude" / "_read",
            sync_path / "reviews",
            sync_path / "reviews" / "_read",
            sync_path / "qa" / "verdicts",
        ]

        results: list[tuple[Path, str, dict[str, Any]]] = []
        seen_paths: set[Path] = set()

        for d in search_dirs:
            if not d.is_dir():
                continue
            for file_path in d.glob("*.*"):
                if file_path in seen_paths or file_path.is_dir():
                    continue
                # Match files specifically containing the work order ID
                name = file_path.name
                if work_order_id not in name:
                    continue
                if not any(k in name.lower() for k in ("verdict", "review")):
                    continue
                seen_paths.add(file_path)
                try:
                    status, evidence = self.parse_verdict_file(file_path)
                    results.append((file_path, status, evidence))
                except Exception:
                    continue

        # Sort chronologically by filename and modification time
        results.sort(key=lambda item: (item[0].name, item[0].stat().st_mtime))
        return results

    def evaluate_work_order(self, project_path: Path, work_order_id: str) -> D024GateDecision:
        """Evaluate D024 compliance for a work order before GitOps progression."""
        wo_file = self.find_work_order_file(project_path, work_order_id)
        if not wo_file:
            decision = D024GateDecision(
                passed=False,
                work_order_id=work_order_id,
                target_work_orders=(),
                verdict_status="MISSING",
                reason=f"Work order {work_order_id} file not found in ACTIVE, COMPLETED, or BLOCKED",
            )
            self.log_decision(project_path, decision)
            return decision

        try:
            wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            decision = D024GateDecision(
                passed=False,
                work_order_id=work_order_id,
                target_work_orders=(),
                verdict_status="INCOMPLETE",
                reason=f"Invalid work order YAML for {work_order_id}: {exc}",
            )
            self.log_decision(project_path, decision)
            return decision

        target_wos = self.resolve_target_work_orders(project_path, work_order_id, wo_data)
        all_verdict_files: list[str] = []
        combined_evidence: dict[str, Any] = {}

        for target_wo in target_wos:
            # 1. Implementation deliverable completeness check
            target_file = self.find_work_order_file(project_path, target_wo)
            if target_file:
                try:
                    target_data = yaml.safe_load(target_file.read_text(encoding="utf-8")) or {}
                    # Check deliverable
                    deliv_paths: list[str] = []
                    deliverable = target_data.get("deliverable")
                    if isinstance(deliverable, str) and deliverable.strip():
                        deliv_paths.append(deliverable.strip())
                    elif isinstance(deliverable, dict):
                        p = deliverable.get("path") or deliverable.get("file")
                        if p:
                            deliv_paths.append(str(p).strip())

                    for d in target_data.get("deliverables", []):
                        if isinstance(d, str) and d.strip():
                            deliv_paths.append(d.strip())
                        elif isinstance(d, dict) and (d.get("path") or d.get("file")):
                            deliv_paths.append(str(d.get("path") or d.get("file")).strip())

                    for deliv_path_str in deliv_paths:
                        deliv_file = project_path / deliv_path_str
                        if not deliv_file.exists():
                            decision = D024GateDecision(
                                passed=False,
                                work_order_id=work_order_id,
                                target_work_orders=target_wos,
                                verdict_status="INCOMPLETE",
                                deliverable_checked=True,
                                deliverable_exists=False,
                                deliverable_path=deliv_path_str,
                                reason=f"Deliverable for {target_wo} does not exist: {deliv_path_str}",
                            )
                            self.log_decision(project_path, decision)
                            return decision
                        if deliv_file.is_file() and deliv_file.stat().st_size == 0:
                            decision = D024GateDecision(
                                passed=False,
                                work_order_id=work_order_id,
                                target_work_orders=target_wos,
                                verdict_status="INCOMPLETE",
                                deliverable_checked=True,
                                deliverable_exists=False,
                                deliverable_path=deliv_path_str,
                                reason=f"Deliverable for {target_wo} is empty (0 bytes): {deliv_path_str}",
                            )
                            self.log_decision(project_path, decision)
                            return decision

                        # Quality & Security validations on existing deliverable
                        deliverable_issues: list[str] = []
                        if deliv_file.is_file():
                            from validators.kernel.security import scan_for_credential_leaks
                            content = deliv_file.read_text(encoding="utf-8", errors="replace")
                            leaks = scan_for_credential_leaks(content, file_path=deliv_path_str)
                            if leaks:
                                deliverable_issues.append(f"insecure credential pattern in deliverable {deliv_path_str}: {leaks[0]}")

                        deliv_type = ""
                        if isinstance(deliverable, dict):
                            deliv_type = str(deliverable.get("type") or "").lower().strip()
                        is_code = (
                            (deliv_type == "code" and not deliv_path_str.endswith((".md", ".txt", ".json", ".yaml", ".yml")))
                            or deliv_path_str.endswith((".py", ".ts", ".js", ".go", ".rs", ".dart"))
                        )
                        if is_code:
                            test_file = self.find_companion_test_file(project_path, deliv_path_str)
                            if not test_file or not test_file.exists():
                                deliverable_issues.append(f"no test file found for deliverable {deliv_path_str}")
                            elif test_file.is_file() and test_file.stat().st_size == 0:
                                deliverable_issues.append(f"companion test file is empty (0 bytes): {test_file.relative_to(project_path).as_posix()}")

                        if deliverable_issues:
                            decision = D024GateDecision(
                                passed=False,
                                work_order_id=work_order_id,
                                target_work_orders=target_wos,
                                verdict_status="NEEDS_CHANGES",
                                deliverable_checked=True,
                                deliverable_exists=True,
                                deliverable_path=deliv_path_str,
                                reason="; ".join(deliverable_issues),
                            )
                            self.log_decision(project_path, decision)
                            return decision
                except Exception:
                    pass

            # 2. QA verdict inspection
            verdicts = self.find_qa_verdicts(project_path, target_wo)
            if not verdicts:
                decision = D024GateDecision(
                    passed=False,
                    work_order_id=work_order_id,
                    target_work_orders=target_wos,
                    verdict_status="MISSING",
                    deliverable_checked=True,
                    deliverable_exists=True,
                    reason=(
                        f"No QA verdict found for {target_wo} "
                        f"(D024 protocol violation: Gemma QA approval required before GitOps progression)"
                    ),
                )
                self.log_decision(project_path, decision)
                return decision

            # Process sorted verdicts: newest / most authoritative
            latest_status = "UNKNOWN"
            for v_path, v_status, v_ev in verdicts:
                all_verdict_files.append(v_path.name)
                combined_evidence[v_path.name] = v_ev
                if v_status in ("NEEDS_CHANGES", "BLOCKED", "APPROVED"):
                    latest_status = v_status

            if latest_status == "NEEDS_CHANGES":
                decision = D024GateDecision(
                    passed=False,
                    work_order_id=work_order_id,
                    target_work_orders=target_wos,
                    verdict_status="NEEDS_CHANGES",
                    verdict_files=tuple(all_verdict_files),
                    deliverable_checked=True,
                    deliverable_exists=True,
                    reason=f"QA verdict for {target_wo} is NEEDS_CHANGES (implementation rejected by QA)",
                    evidence=combined_evidence,
                )
                self.log_decision(project_path, decision)
                return decision

            if latest_status == "BLOCKED":
                decision = D024GateDecision(
                    passed=False,
                    work_order_id=work_order_id,
                    target_work_orders=target_wos,
                    verdict_status="BLOCKED",
                    verdict_files=tuple(all_verdict_files),
                    deliverable_checked=True,
                    deliverable_exists=True,
                    reason=f"QA verdict for {target_wo} is BLOCKED (precondition or environment failure)",
                    evidence=combined_evidence,
                )
                self.log_decision(project_path, decision)
                return decision

            if latest_status != "APPROVED":
                decision = D024GateDecision(
                    passed=False,
                    work_order_id=work_order_id,
                    target_work_orders=target_wos,
                    verdict_status="UNKNOWN",
                    verdict_files=tuple(all_verdict_files),
                    deliverable_checked=True,
                    deliverable_exists=True,
                    reason=f"QA verdict for {target_wo} does not contain explicit approval",
                    evidence=combined_evidence,
                )
                self.log_decision(project_path, decision)
                return decision

        # All target work orders have verified deliverables and APPROVED QA verdicts
        decision = D024GateDecision(
            passed=True,
            work_order_id=work_order_id,
            target_work_orders=target_wos,
            verdict_status="APPROVED",
            verdict_files=tuple(all_verdict_files),
            deliverable_checked=True,
            deliverable_exists=True,
            reason="All target work orders have verified deliverables and explicit Gemma QA approval",
            evidence=combined_evidence,
        )
        self.log_decision(project_path, decision)
        return decision

    def verify_gitops_preconditions(self, project_path: Path, work_order_id: str) -> D024GateDecision:
        """Verify D024 gate preconditions and raise D024ViolationError if not passed."""
        decision = self.evaluate_work_order(project_path, work_order_id)
        if not decision.passed:
            raise D024ViolationError(f"D024 QA Gate Blocked: {decision.reason}")
        return decision

    def log_decision(self, project_path: Path, decision: D024GateDecision) -> Path:
        """Persist structured gate decision to audit trail."""
        audit_dir = project_path / ".sync" / "reports"
        try:
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_file = audit_dir / "d024_gate_audit.jsonl"
            with audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(decision.to_dict(), sort_keys=True) + "\n")
            return audit_file
        except Exception:
            return audit_dir / "d024_gate_audit.jsonl"
