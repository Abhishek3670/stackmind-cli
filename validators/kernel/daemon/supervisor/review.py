"""Integration review, final review, QA defect routing and readiness/deliverable verification.

Part of the :mod:`validators.kernel.daemon.supervisor` package.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from validators.clock import now as _now

from .models import (
    _OPERATION_TERMINAL,
    AdvanceResult,
    OperationContentionError,
    Phase,
    RunState,
    _strip_code_fences,
)
from .release import (
    archive_completed_work_orders,
)

logger = logging.getLogger("stackmind.supervisor")

class _ReviewMixin:
    """Integration review, final review, QA defect routing and readiness/deliverable verification."""

    # ── QA defect routing ──────────────────────────────────────────────

    _WO_ID_RE = re.compile(r"WO-\d{3,}")

    _PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_][\w./\\-]*\.[A-Za-z0-9]{1,6}")

    def _route_qa_defect_to_developers(
        self, state: RunState, ws: Path, qa_wo_id: str, op: dict[str, Any],
        err_msg: str, blocker_text: str,
    ) -> AdvanceResult | None:
        """Dispatch developer rework for a QA defect finding instead of retrying QA.

        Returns WAITING_FOR_OPERATION when rework turns were dispatched, or
        None when no reworkable target could be identified (the caller falls
        through to the generic blocked-WO classification).
        """
        verdict_hits = self._find_qa_defect_verdicts(ws, qa_wo_id, state=state)
        targets: dict[str, str] = {}
        for target, text, _path in verdict_hits:
            if self._is_reworkable_target(state, ws, qa_wo_id, target):
                targets.setdefault(target, text)
        if not targets:
            evidence_text = f"{blocker_text}\n{err_msg}"
            search_dirs = [
                ws / ".sync" / "inbox" / "claude",
                ws / ".sync" / "inbox" / "claude" / "_read",
                ws / ".sync" / "reviews",
                ws / ".sync" / "qa" / "verdicts",
            ]
            for d in search_dirs:
                if d.is_dir():
                    for f in d.iterdir():
                        if f.is_file() and any(k in f.name.lower() for k in ("verdict", "review")):
                            try:
                                evidence_text += "\n" + f.read_text(encoding="utf-8")
                            except Exception:
                                pass
            seen: set[str] = set()
            for ref in self._PATH_TOKEN_RE.findall(evidence_text):
                ref = ref.rstrip(".,;:)'\"").replace("\\", "/")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                owner = self._reworkable_wo_for_path(state, ws, qa_wo_id, ref)
                if owner is not None:
                    snippet = next(
                        (ln.strip() for ln in evidence_text.splitlines() if ref in ln and ln.strip()),
                        f"QA reported a failing check against '{ref}'",
                    )
                    targets.setdefault(owner, snippet)
        if not targets:
            return None

        feedback = (blocker_text or err_msg or "").strip() or "QA reported failing tests"

        # The blocked QA turn is consumed as a defect finding: ignore its
        # operation and keep the work order ACTIVE for re-dispatch later.
        op_id = op.get("operation_id")
        if op_id and op_id not in state.ignored_operation_ids:
            state.ignored_operation_ids.append(str(op_id))
        if qa_wo_id in state.blocked_wo_ids:
            state.blocked_wo_ids.remove(qa_wo_id)
        self._mark_wo_status_on_disk(ws, qa_wo_id, "ACTIVE", clear_error=True)
        state.error = None
        # Set before dispatching so an OperationContentionError mid-loop cannot
        # leave the QA work order eligible for premature re-dispatch.
        state.qa_rework_targets[qa_wo_id] = list(targets.keys())

        dispatched: list[str] = []
        for target_id, finding in list(targets.items()):
            # Re-open the target: removed from the completed set so dependency
            # gating holds the QA work order back, and its pre-rework operation
            # ignored so the old completed result cannot re-complete it.
            if target_id in state.completed_wo_ids:
                state.completed_wo_ids.remove(target_id)
            if target_id in state.blocked_wo_ids:
                state.blocked_wo_ids.remove(target_id)
            if target_id in state.failed_wo_ids:
                state.failed_wo_ids.remove(target_id)
            rework_count = state.qa_reworks.get(target_id, 0) + 1
            state.qa_reworks[target_id] = rework_count
            if rework_count > 1:
                # If worker already attempted rework once and QA is still failing,
                # escalate to the Lead Architect for direct resolution fix!
                self.escalate_wo_to_recovery(state, target_id, reason=f"QA defect unresolved after worker rework: {finding}")
                self._transition(state, Phase.ARCHITECT_RECOVERY_DECISION)
                return AdvanceResult.TRANSITIONED
            # Rework phase grants a fresh retry budget for fixing defects
            state.retry_counts[target_id] = 0
            target_op = self._find_latest_wo_operation(state, target_id)
            if target_op and target_op.get("operation_id"):
                target_op_id = str(target_op["operation_id"])
                if target_op_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(target_op_id)
            self._unarchive_work_order(ws, target_id)
            if target_id not in state.worker_wo_ids:
                state.worker_wo_ids.append(target_id)
            if state.published_wo_ids and target_id not in state.published_wo_ids:
                state.published_wo_ids.append(target_id)

            def _rework_prompt(n: int, agent: str, _t=target_id, _f=finding) -> str:
                return (
                    f"Rework work order {_t} after a QA defect finding (attempt {n}). "
                    f"Gemma's QA suite reports the following failing check — fix the "
                    f"application deliverable so the test passes; do NOT modify the "
                    f"test suite (QA owns it):\n{_f}\n\nQA finding context:\n{feedback}"
                )

            try:
                if self._retry_worker(state, ws, target_id, _rework_prompt):
                    dispatched.append(target_id)
                    self._archive_verdicts_for_wo(target_id, ws)
            except OperationContentionError:
                pass

        # Consume the verdict files that named or contributed to the targets so the stale
        # NEEDS_CHANGES cannot re-trigger rework or poison D024's
        # rejection-first verdict ordering after the fix lands.
        to_archive = list(verdict_hits)
        if not to_archive:
            search_dirs = [
                ws / ".sync" / "inbox" / "claude",
                ws / ".sync" / "inbox" / "claude" / "_read",
                ws / ".sync" / "reviews",
                ws / ".sync" / "qa" / "verdicts",
            ]
            for d in search_dirs:
                if d.is_dir():
                    for f in d.iterdir():
                        if f.is_file() and any(k in f.name.lower() for k in ("verdict", "review")):
                            try:
                                txt = f.read_text(encoding="utf-8")
                                if "NEEDS_CHANGES" in txt.upper() or "NEEDS CHANGES" in txt.upper():
                                    to_archive.append(("", "", f))
                            except Exception:
                                pass
        self._archive_qa_defect_verdicts(to_archive, ws)

        if not dispatched:
            # Target retry budgets exhausted or dispatch deferred due to contention
            has_pending = any(
                state.retry_counts.get(t, 0) < state.max_retries
                for t in targets
            )
            if has_pending:
                state.qa_rework_targets[qa_wo_id] = list(targets.keys())
                return AdvanceResult.WAITING_FOR_OPERATION
            state.qa_rework_targets.pop(qa_wo_id, None)
            return None

        state.qa_rework_targets[qa_wo_id] = dispatched
        return AdvanceResult.WAITING_FOR_OPERATION

    def _find_qa_defect_verdicts(
        self, ws: Path, qa_wo_id: str, state: RunState | None = None,
    ) -> list[tuple[str, str, Path]]:
        """Find NEEDS_CHANGES verdict files naming another work order or deliverable path.

        Each hit is (target_wo_id, verdict_text, path). Files are left in
        place; the caller archives them only when rework is actually routed.
        """
        hits: list[tuple[str, str, Path]] = []
        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                name = f.name
                if not any(k in name.lower() for k in ("verdict", "review")):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "NEEDS_CHANGES" not in text.upper() and "NEEDS CHANGES" not in text.upper():
                    continue
                # 1. Check for deliverable path references in the verdict first.
                # Concrete deliverable ownership takes precedence over LLM-hallucinated
                # work order IDs in the review text or filename.
                deliverable_targets: list[str] = []
                if state is not None:
                    seen_refs: set[str] = set()
                    for ref in self._PATH_TOKEN_RE.findall(text):
                        ref = ref.rstrip(".,;:)'\"").replace("\\", "/")
                        if not ref or ref in seen_refs:
                            continue
                        seen_refs.add(ref)
                        owner = self._reworkable_wo_for_path(state, ws, qa_wo_id, ref)
                        if owner is not None and owner not in deliverable_targets:
                            deliverable_targets.append(owner)

                # 2. Target named in filename (e.g. WO-001-qa-verdict.md)
                named_target = None
                for target in self._WO_ID_RE.findall(name):
                    if target != qa_wo_id and target != "WO-000":
                        named_target = target
                        break
                # 3. Target named in text body (e.g. file named WO-004-qa-verdict.md
                # but text discusses WO-001)
                if not named_target:
                    for target in self._WO_ID_RE.findall(text):
                        if target != qa_wo_id and target != "WO-000":
                            named_target = target
                            break

                found_targets: list[str] = []
                if deliverable_targets:
                    # If the named target owns one of the failing deliverables, honor it.
                    if named_target and named_target in deliverable_targets:
                        found_targets.append(named_target)
                    else:
                        # The named target does NOT own the deliverable (LLM misattribution);
                        # route to the true deliverable owner(s).
                        found_targets.extend(deliverable_targets)
                elif named_target:
                    found_targets.append(named_target)

                for found_target in found_targets:
                    hits.append((found_target, text.strip(), f))
        return hits

    def _archive_qa_defect_verdicts(self, hits: list[tuple[str, str, Path]], ws: Path) -> None:
        """Move consumed NEEDS_CHANGES verdicts out of every verdict search path."""
        import shutil
        import uuid
        archive = ws / ".sync" / "reviews" / "_consumed"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for _target, _text, path in hits:
            if not path.is_file():
                continue
            try:
                archive.mkdir(parents=True, exist_ok=True)
                dest = archive / f"{path.stem}-{stamp}{path.suffix}"
                if dest.exists():
                    dest = archive / f"{path.stem}-{stamp}-{uuid.uuid4().hex[:6]}{path.suffix}"
                shutil.move(str(path), str(dest))
            except Exception:
                pass

    def _archive_verdicts_for_wo(self, wo_id: str, ws: Path) -> None:
        """Archive all NEEDS_CHANGES verdict files matching wo_id once rework is dispatched."""
        import shutil
        import uuid
        archive = ws / ".sync" / "reviews" / "_consumed"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        search_dirs = [
            ws / ".sync" / "inbox" / "claude",
            ws / ".sync" / "inbox" / "claude" / "_read",
            ws / ".sync" / "reviews",
            ws / ".sync" / "qa" / "verdicts",
        ]
        deliv_path = self._get_wo_deliverable_path(wo_id, ws)
        deliv_name = Path(deliv_path).name if deliv_path else None
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in list(d.iterdir()):
                if not f.is_file() or not any(k in f.name.lower() for k in ("verdict", "review")):
                    continue
                try:
                    txt = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "NEEDS_CHANGES" not in txt.upper() and "NEEDS CHANGES" not in txt.upper():
                    continue

                wos_in_name = self._WO_ID_RE.findall(f.name)
                if wos_in_name and wo_id not in wos_in_name:
                    continue
                header_wos = self._WO_ID_RE.findall(txt[:300])
                if header_wos and wo_id not in header_wos:
                    continue

                matches_wo = wo_id in f.name or bool(re.search(rf"\b{re.escape(wo_id)}\b", txt))
                if not matches_wo:
                    other_wos = [wid for wid in self._WO_ID_RE.findall(txt) if wid != wo_id and wid != "WO-000"]
                    if not other_wos:
                        if deliv_path and str(deliv_path).replace("\\", "/") in txt.replace("\\", "/"):
                            matches_wo = True
                        elif deliv_name and len(deliv_name) > 6 and deliv_name in txt:
                            matches_wo = True
                if matches_wo:
                    try:
                        archive.mkdir(parents=True, exist_ok=True)
                        dest = archive / f"{f.stem}-{stamp}{f.suffix}"
                        if dest.exists():
                            dest = archive / f"{f.stem}-{stamp}-{uuid.uuid4().hex[:6]}{f.suffix}"
                        shutil.move(str(f), str(dest))
                    except Exception:
                        pass

    def _is_reworkable_target(self, state: RunState, ws: Path, qa_wo_id: str, wo_id: str) -> bool:
        """A rework target must be a completed, non-QA, non-gitops work order."""
        if wo_id in (qa_wo_id, state.planning_wo_id, "WO-000"):
            return False
        if wo_id in state.failed_wo_ids and wo_id not in state.completed_wo_ids:
            return False
        if self._role_for_agent(self._agent_for_wo(wo_id, ws)) in ("qa", "gitops"):
            return False
        if self._is_wo_running(state, wo_id):
            return False
        if wo_id in state.completed_wo_ids:
            return True
        # _agent_for_wo defaults unknown IDs to "codex", so require the work
        # order to exist on disk before re-dispatching a turn for it.
        return (ws / ".sync" / "work-orders" / "COMPLETED" / f"{wo_id}.yaml").is_file()

    def _reworkable_wo_for_path(
        self, state: RunState, ws: Path, qa_wo_id: str, ref: str,
    ) -> str | None:
        """Find the completed work order that owns a referenced deliverable path."""
        ref_norm = ref.strip("./").lower()
        if not ref_norm or ref_norm.startswith(".sync/"):
            return None
        candidates: list[str] = []
        for cand in list(state.completed_wo_ids):
            if cand not in candidates:
                candidates.append(cand)
        for wo_file in sorted((ws / ".sync" / "work-orders" / "COMPLETED").glob("*.yaml")):
            if wo_file.stem not in candidates:
                candidates.append(wo_file.stem)
        for cand in candidates:
            if not self._is_reworkable_target(state, ws, qa_wo_id, cand):
                continue
            wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{cand}.yaml"
            if not wo_file.is_file():
                wo_file = ws / ".sync" / "work-orders" / "COMPLETED" / f"{cand}.yaml"
            if not wo_file.is_file():
                continue
            try:
                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            deliv = data.get("deliverable")
            delivs = data.get("deliverables")
            deliv_paths: list[str] = []
            if isinstance(deliv, dict) and deliv.get("path"):
                deliv_paths.append(str(deliv["path"]))
            elif isinstance(deliv, str) and deliv:
                deliv_paths.append(deliv)
            elif isinstance(deliv, list):
                for item in deliv:
                    if isinstance(item, dict) and item.get("path"):
                        deliv_paths.append(str(item["path"]))
                    elif isinstance(item, str) and item:
                        deliv_paths.append(item)
            if isinstance(delivs, list):
                for item in delivs:
                    if isinstance(item, dict) and item.get("path"):
                        deliv_paths.append(str(item["path"]))
                    elif isinstance(item, str) and item:
                        deliv_paths.append(item)
            elif isinstance(delivs, dict) and delivs.get("path"):
                deliv_paths.append(str(delivs["path"]))
            contract_file = ws / ".sync" / "contracts" / f"{cand}.yaml"
            if contract_file.is_file():
                try:
                    cdata = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                    if isinstance(cdata, dict):
                        for entry in cdata.get("scope", {}).get("allow", []):
                            if isinstance(entry, dict) and entry.get("module"):
                                m = str(entry["module"])
                                if not m.endswith("*"):
                                    deliv_paths.append(m)
                            elif isinstance(entry, str) and not entry.endswith("*"):
                                deliv_paths.append(entry)
                except Exception:
                    pass
            for p in deliv_paths:
                p_norm = str(p).replace("\\", "/").strip("./").lower()
                if not p_norm:
                    continue
                if p_norm == ref_norm or p_norm.endswith("/" + ref_norm) or ref_norm.endswith("/" + p_norm):
                    return cand
        return None

    def _advance_integration_review(self, state: RunState) -> AdvanceResult:
        """INTEGRATION_REVIEW: dispatch Architecture with dedicated review scope to review deliverables."""
        ws = Path(state.workspace)
        # Defensively reconcile planning WO-000 to COMPLETED
        for p_id in (state.planning_wo_id, "WO-000"):
            if p_id:
                if p_id not in state.completed_wo_ids:
                    state.completed_wo_ids.insert(0, p_id)
                self._mark_wo_status_on_disk(ws, p_id, "COMPLETED")
        archive_completed_work_orders(ws, [state.planning_wo_id or "WO-000", "WO-000"])
        if not state.integration_operation_id:
            adopted = self._adopt_running_integration_operation(state, state.integration_wo_id)
            if adopted is not None:
                return self._advance_integration_review(state)

        if state.integration_operation_id:
            if state.integration_operation_id in getattr(state, "ignored_operation_ids", []):
                state.integration_operation_id = None
                return self._advance_integration_review(state)
            op = self._get_operation(state.integration_operation_id)
            if op is None:
                state.integration_operation_id = None
                return self._advance_integration_review(state)
            status = str(op.get("status", "")).upper()
            if status not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION

            op_result = op.get("result", {})
            result_status = str(op_result.get("status", "")).lower()

            if status == "FAILED":
                retries = getattr(state, "integration_retries", 0)
                max_retries = getattr(state, "max_integration_retries", 2)
                if retries < max_retries:
                    setattr(state, "integration_retries", retries + 1)
                    if op.get("operation_id"):
                        state.ignored_operation_ids.append(op["operation_id"])
                    state.integration_operation_id = None
                    return self._advance_integration_review(state)
                state.error = f"Integration review failed: {op.get('result', {}).get('error', 'unknown')}"
                self._transition(state, Phase.FAILED)
                return AdvanceResult.FAILED

            # Evaluate deterministic integration gate
            from validators.harness.integration_gate import IntegrationGate
            gate = IntegrationGate(ws)
            session_ops = []
            if hasattr(self.manager, "list_operations"):
                try:
                    session_ops = self.manager.list_operations(state.session_id)
                except Exception:
                    session_ops = []
            ignored_ops = set(getattr(state, "ignored_operation_ids", []) or [])
            valid_session_ops = [
                o for o in session_ops
                if isinstance(o, dict)
                and str(o.get("operation_id", "")) not in ignored_ops
            ]
            extra_telemetry = [
                o.get("result", {})
                for o in valid_session_ops
                if isinstance(o.get("result"), dict)
            ]
            gate_decision = gate.evaluate(state=state, integration_op=op, extra_telemetry=extra_telemetry)
            state.integration_evidence = gate_decision.to_dict()

            llm_blocked = (status == "BLOCKED" or result_status == "blocked")
            deterministic_blocked = not gate_decision.passed

            if llm_blocked or deterministic_blocked:
                # A valid blocked review or deterministic failure routes its blockers — with the
                # prescribed fixes when provided — back to the implementation and QA workers for
                # bounded rework; the review re-runs after they complete. Terminal BLOCKED only when the
                # rework rounds are exhausted.
                blockers: list[str] = []
                remediations: dict[str, str] = {}
                detail_paths: list[str] = []

                if llm_blocked:
                    # When the Architect review explicitly reports blockers, use its specific findings
                    for item in op_result.get("blocker_details") or []:
                        if not isinstance(item, dict):
                            continue
                        finding = str(item.get("finding", "")).strip()
                        if not finding:
                            continue
                        if finding not in blockers:
                            blockers.append(finding)
                        remediation = str(item.get("remediation", "")).strip()
                        if remediation:
                            remediations[finding] = remediation
                        if item.get("path"):
                            detail_paths.append(str(item["path"]))

                    for raw in op_result.get("blockers") or []:
                        if str(raw) not in blockers:
                            blockers.append(str(raw))
                    if not blockers and op_result.get("reason"):
                        blockers = [op_result["reason"]]

                    # Waive unfulfillable build execution blockers when dependencies are not installed
                    # in workspace and deterministic gate skipped build execution:
                    build_chk = next((c for c in gate_decision.checks if c.name == "build_execution"), None)
                    if build_chk and build_chk.status == "skipped" and build_chk.passed:
                        def _is_unfulfillable_build_blocker(b_text: str) -> bool:
                            b_lower = b_text.lower()
                            return (
                                "build execution" in b_lower
                                or "record of build" in b_lower
                                or "vite build" in b_lower
                                or "npm run build" in b_lower
                                or "no build execution" in b_lower
                            )
                        filtered_blockers = [b for b in blockers if not _is_unfulfillable_build_blocker(b)]
                        if len(filtered_blockers) < len(blockers):
                            blockers = filtered_blockers
                            detail_paths = [p for p in detail_paths if not (p == "package.json" and not blockers)]

                    if not blockers:
                        state.integration_blockers = []
                        state.integration_evidence = gate_decision.to_dict()
                        self._transition(state, Phase.FINAL_REVIEW)
                        return AdvanceResult.TRANSITIONED
                else:
                    # When the review claimed completion but deterministic checks failed,
                    # enforce deterministic blockers (anti-fabrication, test/build/route failures)
                    for item in gate_decision.blocker_details:
                        finding = str(item.get("finding", "")).strip()
                        if finding and finding not in blockers:
                            blockers.append(finding)
                        remediation = str(item.get("remediation", "")).strip()
                        if remediation:
                            remediations[finding] = remediation
                        if item.get("path"):
                            detail_paths.append(str(item["path"]))

                    for issue in gate_decision.issues:
                        if str(issue) not in blockers:
                            blockers.append(str(issue))

                # Auto-remediation for non-code governance, release packaging, and runtime retry artifacts
                if blockers:
                    def _is_governance_or_packaging_blocker(b_text: str) -> bool:
                        b_lower = b_text.lower()
                        return (
                            "changelog" in b_lower
                            or "readme" in b_lower
                            or "wo-000" in b_lower
                            or "worker_timeout" in b_lower
                            or "timeout (retry" in b_lower
                            or "packaging" in b_lower
                            or "board inconsistency" in b_lower
                            or "active on board" in b_lower
                            or "release-packaging" in b_lower
                            or "documentation gap" in b_lower
                        )

                    gov_blockers = [b for b in blockers if _is_governance_or_packaging_blocker(b)]
                    if gov_blockers:
                        for p_id in (state.planning_wo_id, "WO-000"):
                            if p_id:
                                if p_id not in state.completed_wo_ids:
                                    state.completed_wo_ids.insert(0, p_id)
                                self._mark_wo_status_on_disk(ws, p_id, "COMPLETED")
                        archive_completed_work_orders(ws, [state.planning_wo_id or "WO-000", "WO-000"])
                        self._ensure_release_documentation(ws, state)
                        blockers = [b for b in blockers if not _is_governance_or_packaging_blocker(b)]
                        detail_paths = [p for p in detail_paths if not any(
                            doc in p.lower() for doc in ("changelog", "readme", "wo-000")
                        )]

                if not blockers:
                    state.integration_blockers = []
                    state.integration_evidence = gate_decision.to_dict()
                    self._transition(state, Phase.FINAL_REVIEW)
                    return AdvanceResult.TRANSITIONED

                state.integration_blockers = list(blockers)
                err_msg = (
                    "; ".join(blockers)
                    if blockers
                    else _strip_code_fences(str(op_result.get("summary", "Blockers reported during integration review")))
                )

                reworkable = self._reworkable_integration_wos(
                    state, ws, blockers + detail_paths
                )
                if state.integration_rework_rounds < state.max_retries and reworkable:
                    state.integration_rework_rounds += 1
                    if state.integration_operation_id and state.integration_operation_id not in state.ignored_operation_ids:
                        state.ignored_operation_ids.append(state.integration_operation_id)
                    state.integration_operation_id = None
                    # Per-blocker feedback with the Architect's prescribed fix.
                    feedback_lines = []
                    for b in blockers:
                        line = f"- Blocker: {b}"
                        if b in remediations:
                            line += f"\n  Prescribed fix: {remediations[b]}"
                        feedback_lines.append(line)
                    feedback = (
                        "\n".join(feedback_lines)
                        if feedback_lines
                        else "review could not verify the deliverables"
                    )
                    # S6 concurrency: multiple rework turns dispatch under a
                    # shared batch parent so they don't contend for the root
                    # operation slot.
                    parent_op_id = None
                    if len(reworkable) > 1 and hasattr(self.manager, "begin_operation"):
                        try:
                            _, parent_op_id = self.manager.begin_operation(
                                state.session_id,
                                "parallel_dispatch",
                                {"work_orders": reworkable, "rework_round": state.integration_rework_rounds},
                                role="supervisor",
                            )
                            state.batch_operation_id = parent_op_id
                        except Exception:
                            parent_op_id = None
                    for wo_id in reworkable:
                        target_op = self._find_latest_wo_operation(state, wo_id)
                        if target_op and target_op.get("operation_id"):
                            target_op_id = str(target_op["operation_id"])
                            if target_op_id not in state.ignored_operation_ids:
                                state.ignored_operation_ids.append(target_op_id)
                        self._unarchive_work_order(ws, wo_id)
                        if wo_id not in state.worker_wo_ids:
                            state.worker_wo_ids.append(wo_id)
                        if state.published_wo_ids and wo_id not in state.published_wo_ids:
                            state.published_wo_ids.append(wo_id)
                        if wo_id in state.completed_wo_ids:
                            state.completed_wo_ids.remove(wo_id)
                        if wo_id in state.failed_wo_ids:
                            state.failed_wo_ids.remove(wo_id)
                        if wo_id in state.blocked_wo_ids:
                            state.blocked_wo_ids.remove(wo_id)
                        state.retry_counts[wo_id] = 0
                        self._archive_verdicts_for_wo(wo_id, ws)
                        agent = self._agent_for_wo(wo_id, ws)
                        role = self._role_for_agent(agent)
                        rework_prompt = (
                            f"Rework work order {wo_id} (integration review round "
                            f"{state.integration_rework_rounds}/{state.max_retries}): the "
                            f"integration review blocked release with these findings "
                            f"and prescribed fixes:\n"
                            f"{feedback}\n"
                            f"Apply each prescribed fix to your deliverable so every "
                            f"blocker is resolved, then resubmit."
                        )
                        if role == "qa" or agent == "gemma":
                            rework_prompt += (
                                "\n\nNOTE: You are the QA agent. You must re-verify deliverables using your assigned "
                                "test suite. Do NOT attempt to author or modify application files, documentation, "
                                "or manifests outside your assigned test contract scope."
                            )
                        try:
                            self.manager.start_turn(
                                state.session_id,
                                rework_prompt,
                                role=role,
                                agent_id=agent,
                                work_order_id=wo_id,
                                parent_operation_id=parent_op_id,
                            )
                        except OperationContentionError:
                            pass
                        except Exception:
                            state.failed_wo_ids.append(wo_id)
                    state.error = None
                    self._transition(state, Phase.EXECUTING)
                    return AdvanceResult.WAITING_FOR_OPERATION

                state.error = f"Integration review blocked: {err_msg}"
                self._transition(state, Phase.BLOCKED)
                return AdvanceResult.BLOCKED

            # Both deterministic checks and review completed cleanly
            state.integration_blockers = []
            state.integration_evidence = gate_decision.to_dict()
            self._transition(state, Phase.FINAL_REVIEW)
            return AdvanceResult.TRANSITIONED

        if not state.integration_wo_id:
            from ..authoring import get_next_work_order_int
            review_wo_id = f"WO-{get_next_work_order_int(ws):03d}"
            state.integration_wo_id = review_wo_id
        else:
            review_wo_id = state.integration_wo_id

        deliverables = self._collect_completed_deliverables(state, ws)
        self._synthesize_integration_review_artifacts(state, ws, review_wo_id, deliverables)
        review_prompt = self._integration_review_prompt(state, review_wo_id, deliverables)

        try:
            op = self.manager.start_turn(
                state.session_id,
                review_prompt,
                role="architecture",
                agent_id="claude",
                work_order_id=review_wo_id,
            )
            state.integration_operation_id = op.get("operation_id")
            state.contention_count = 0
            try:
                self.save_run_state(state, ws)
            except Exception:
                pass
            return AdvanceResult.WAITING_FOR_OPERATION
        except OperationContentionError:
            adopted = self._adopt_running_integration_operation(state, review_wo_id)
            if adopted is not None:
                state.contention_count = 0
                try:
                    self.save_run_state(state, ws)
                except Exception:
                    pass
                return AdvanceResult.WAITING_FOR_OPERATION
            raise

    def _adopt_running_integration_operation(
        self, state: RunState, fallback_wo_id: str | None,
    ) -> str | None:
        """Find a non-terminal architecture operation for the integration review.

        Crash-recovery and contention-fallback scan: returns the operation id
        (recorded on the run state) or None when nothing is running.
        """
        try:
            for op in reversed(self.manager.list_operations(state.session_id)):
                op_id = op.get("operation_id")
                if op_id and op_id in getattr(state, "ignored_operation_ids", []):
                    continue
                op_wo = op.get("work_order_id")
                op_role = str(op.get("role", "")).lower()
                if (op_wo == fallback_wo_id or op_role == "architecture") and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                    state.integration_operation_id = op_id
                    state.integration_wo_id = op_wo or fallback_wo_id
                    return op_id
        except Exception:
            pass
        return None

    def _collect_completed_deliverables(self, state: RunState, ws: Path) -> list[str]:
        """Collect all declared deliverable paths across completed work orders."""
        deliverables: list[str] = []
        for wo_id in state.completed_wo_ids:
            for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
                wo_file = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
                if wo_file.is_file():
                    try:
                        wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                        if isinstance(wo_data, dict):
                            deliv = wo_data.get("deliverable")
                            if isinstance(deliv, dict) and deliv.get("path"):
                                p = str(deliv["path"]).replace("\\", "/").strip().lstrip("/")
                                if p and p not in deliverables:
                                    deliverables.append(p)
                    except Exception:
                        pass
                    break
        return deliverables

    def _synthesize_integration_review_artifacts(
        self, state: RunState, ws: Path, review_wo_id: str, deliverables: list[str],
    ) -> None:
        """Synthesize the dedicated read-only review work order, contract, and INDEX entry."""
        review_wo_record = {
            "id": review_wo_id,
            "type": "VALIDATION",
            "title": "Integration Review",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["claude"],
            "dependencies": list(state.completed_wo_ids),
            "deliverable": {
                "type": "doc",
                "description": "Integration review report and decision",
            },
            "description": f"Perform integration review of all completed deliverables for '{state.product_goal}'.",
            "created": _now(),
            "updated": _now(),
        }

        allow_rules: list[dict[str, Any]] = [
            {"module": "PLAN.md"},
            {"module": "README.md"},
            {"module": "CHANGELOG.md"},
            {"module": "VERSION.md"},
            {"module": "VERSION"},
            {"module": ".sync/work-orders/**"},
            {"module": ".sync/contracts/**"},
            {"module": ".sync/inbox/claude/**"},
            {"module": ".sync/decisions/**"},
        ]
        for d in deliverables:
            allow_rules.append({"module": d})
        allow_rules.extend([
            {"module": "*.html"},
            {"module": "*.css"},
            {"module": "*.js"},
            {"module": "templates/**"},
            {"module": "static/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
        ])

        seen_mods = set()
        unique_allow: list[dict[str, Any]] = []
        for r in allow_rules:
            mod = r.get("module")
            if mod and mod not in seen_mods:
                seen_mods.add(mod)
                unique_allow.append(r)

        deny_rules = [
            {"module": ".git/**"},
            {"module": ".env"},
            {"module": ".env.*"},
            {"module": ".sync/runtime/**"},
            {"module": ".sync/knowledge/**"},
            {"module": ".sync/snapshots/**"},
            {"module": ".sync/outbox/**"},
            {"module": ".sync/agents/**"},
            {"module": ".sync/inbox/CEO/**"},
            {"module": "__pycache__/**"},
            {"module": ".venv/**"},
            {"module": "node_modules/**"},
        ]

        review_contract_record = {
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": review_wo_id,
            "identity": {
                "role": "architecture",
                "reports_to": "ceo",
            },
            "scope": {
                "allow": unique_allow,
                "deny": deny_rules,
                "write": "read-only",
            },
            "budget": {
                "max_files_touched": 1,
                "max_tokens": 50000,
            },
        }

        # Write dedicated review artifacts to disk
        try:
            from validators.harness.authoring_gate import AuthoringGate
            gate = AuthoringGate(project_root=ws)
            wo_yaml = yaml.safe_dump(review_wo_record, sort_keys=False)
            contract_yaml = yaml.safe_dump(review_contract_record, sort_keys=False)
            rel_wo_path = f".sync/work-orders/ACTIVE/{review_wo_id}.yaml"
            rel_contract_path = f".sync/contracts/{review_wo_id}.yaml"
            gate.validate_artifact_content(rel_wo_path, wo_yaml, agent="architecture", project_root=ws)
            gate.validate_artifact_content(rel_contract_path, contract_yaml, agent="architecture", project_root=ws)

            wo_file = ws / ".sync" / "work-orders" / "ACTIVE" / f"{review_wo_id}.yaml"
            wo_file.parent.mkdir(parents=True, exist_ok=True)
            wo_file.write_text(wo_yaml, encoding="utf-8")

            contract_file = ws / ".sync" / "contracts" / f"{review_wo_id}.yaml"
            contract_file.parent.mkdir(parents=True, exist_ok=True)
            contract_file.write_text(contract_yaml, encoding="utf-8")

            # Update INDEX.yaml
            idx_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
            if idx_file.is_file():
                idx_data = yaml.safe_load(idx_file.read_text(encoding="utf-8"))
                if isinstance(idx_data, dict):
                    orders = idx_data.setdefault("orders", [])
                    if not any(o.get("id") == review_wo_id for o in orders if isinstance(o, dict)):
                        orders.append({
                            "id": review_wo_id,
                            "title": review_wo_record["title"],
                            "status": "ACTIVE",
                            "priority": "P0",
                            "dependencies": list(state.completed_wo_ids),
                            "assigned_agents": ["claude"],
                        })
                    idx_data["next_id"] = max(idx_data.get("next_id", 1), int(review_wo_id.replace("WO-", "")) + 1)
                    temp_idx = idx_file.with_name(f"{idx_file.name}.tmp.{int(time.time() * 1000)}")
                    temp_idx.write_text(yaml.safe_dump(idx_data, sort_keys=False), encoding="utf-8")
                    temp_idx.replace(idx_file)
        except Exception:
            pass

    def _integration_review_prompt(
        self, state: RunState, review_wo_id: str, deliverables: list[str],
    ) -> str:
        """Explicit JSON-format review prompt (Requirement 2)."""
        ws = Path(state.workspace)
        cfg_file = ws / ".sync" / "config.yaml"
        env = "development"
        if cfg_file.is_file():
            try:
                cfg_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
                if isinstance(cfg_data, dict) and cfg_data.get("environment"):
                    env = str(cfg_data["environment"]).lower().strip()
            except Exception:
                pass

        deliv_lines = "\n".join(f"- {d}" for d in deliverables) if deliverables else "- (none declared)"
        completed_summary = ", ".join(state.completed_wo_ids)

        if env == "development":
            security_checklist = (
                "SECURITY & QUALITY CHECKLIST (ENVIRONMENT: DEVELOPMENT):\n"
                "- Development mode: Developer conveniences (such as `debug=True` and dev fallback secrets "
                "like `os.environ.get('SECRET_KEY', 'dev-...')` for local testing) are permitted and must NOT block release.\n"
                "- Inspect developer completion notices in `.sync/inbox/claude/` for implementation rationales and design decisions before blocking.\n"
                "- Password verification does not re-derive hashes unnecessarily; salts are stored combined with the hash.\n"
                "- Client code calls the real API endpoints (no mock/placeholder flows left behind)."
            )
        else:
            security_checklist = (
                "SECURITY & QUALITY CHECKLIST (ENVIRONMENT: PRODUCTION) — verify each item and list any unmet item as a blocker:\n"
                "- No hardcoded secrets and no hardcoded env-fallback defaults (e.g. SECRET_KEY = "
                "os.environ.get(..., 'literal') must fail fast instead).\n"
                "- Debug flags are disabled for production (debug=True / DEBUG = True must not appear).\n"
                "- Password verification does not re-derive hashes unnecessarily; salts are stored combined "
                "with the hash (single field), not as separate columns.\n"
                "- State-changing forms/endpoints are protected against CSRF, and authentication endpoints "
                "have rate limiting or lockout.\n"
                "- Client code calls the real API endpoints (no mock/placeholder flows left behind)."
            )

        return (
            f"All implementation work orders have been completed and QA-approved: {completed_summary}.\n\n"
            f"Project Environment: {env.upper()}\n\n"
            "As Senior Architect, perform the final integration review:\n"
            "1. Read PLAN.md for the original product requirements.\n"
            f"2. Read each declared deliverable file to verify completeness and correctness:\n{deliv_lines}\n"
            "3. Inspect developer completion notices in `.sync/inbox/claude/` to understand implementation rationales and design decisions.\n"
            "4. Verify that all components integrate properly and test coverage is satisfactory.\n"
            "5. Build Verification: If package.json or Makefile defines a build command AND dependencies are installed (e.g. node_modules exists, build was run), verify that build execution succeeded without errors. If dependencies are not installed or workers do not have shell command permissions in their contracts, verify code correctness and imports statically and approve with a note, rather than blocking delivery.\n"
            "6. Release Packaging & Documentation Authority: As Lead Architect, you are authorized to update or author README.md and CHANGELOG.md directly using write_file to document 0.1.0 release notes and architecture. If CHANGELOG.md is pending release notes or README.md is template boilerplate, author them directly or permit the upcoming GitOps release phase to finalize them. Do NOT block integration review when core application deliverables are verified and functional.\n"
            "7. Work-Order Board & WO-000: WO-000 was the root planning order for PLAN.md. Its completion is governed automatically; an ACTIVE status on WO-000 or board reconciliation is handled by the supervisor and must NOT block delivery.\n"
            "8. Runtime Fault-Tolerance & Retries: Historical supervisor retry anomalies (e.g. WORKER_TIMEOUT retry 1/2) that subsequently succeeded are internal runtime recovery events. When all 25/25 QA assertions pass, historical retries are NOT release blockers.\n\n"
            "OUTPUT FORMAT INSTRUCTIONS:\n"
            "Your output must be a single JSON object with these exact fields:\n"
            "If review passes:\n"
            "{\n"
            '  "status": "completed",\n'
            '  "summary": "Integration review passed: all deliverables verified against requirements.",\n'
            '  "report_markdown": "Integration review passed: all deliverables verified against requirements.",\n'
            '  "blockers": []\n'
            "}\n\n"
            "If changes are needed or deliverables cannot be verified:\n"
            "{\n"
            '  "status": "blocked",\n'
            '  "summary": "Unable to verify a deliverable.",\n'
            '  "report_markdown": "Detailed explanation of which deliverable could not be verified and why.",\n'
            '  "blockers": ["Read access to app/rate_limiter.py was denied."],\n'
            '  "blocker_details": [\n'
            '    {"finding": "Hardcoded secret key fallback in src/backend.py",\n'
            '     "remediation": "Replace with SECRET_KEY = os.environ[\'SECRET_KEY\'] so a missing variable fails fast",\n'
            '     "path": "src/backend.py"}\n'
            '  ]\n'
            "}\n\n"
            "CRITICAL RULES:\n"
            "- When status is 'blocked', 'blockers' MUST be a non-empty string array listing each blocker. "
            "Do NOT rely on prose in the summary to satisfy the schema.\n"
            "- When status is 'blocked', 'blocker_details' SHOULD accompany each blocker with a "
            "'remediation': the concrete fix the worker should apply (you are the prescribing "
            "architect — prescribe precisely; workers apply your prescription verbatim).\n"
            "- When status is 'completed', 'blockers' MUST be an empty array [].\n"
            "- Your contract scope is strictly read-only. Do NOT attempt to write or edit application or test files.\n\n"
            f"{security_checklist}"
        )

    def _reworkable_integration_wos(
        self, state: RunState, ws: Path, blockers: list[str],
    ) -> list[str]:
        """Implementation, GitOps, and QA work orders that rework should re-run.

        When the blockers reference deliverable paths, accused work orders (plus QA,
        which must re-verify after code changes) are selected; unmappable blockers fall
        back to code-producing and GitOps work orders.
        """
        excluded = {
            state.planning_wo_id,
            "WO-000",
            state.integration_wo_id,
        }
        blocker_text = " ".join(str(b) for b in blockers).lower()
        gitops_keywords = ("version", "changelog", "package.json", "release", "manifest", "gitops", "tag")
        is_gitops_blocker = any(kw in blocker_text for kw in gitops_keywords)
        if state.gitops_wo_id and not is_gitops_blocker:
            excluded.add(state.gitops_wo_id)

        code_wos: list[str] = []
        qa_wos: list[str] = []
        gitops_wos: list[str] = []
        accused: list[str] = []

        candidate_wos: list[str] = []
        for wid in list(dict.fromkeys(state.completed_wo_ids + getattr(state, "worker_wo_ids", []))):
            if wid and wid not in candidate_wos and wid not in excluded:
                candidate_wos.append(wid)

        for wo_id in candidate_wos:
            agent = self._agent_for_wo(wo_id, ws)
            role = self._role_for_agent(agent)
            if agent == "gemma" or role == "qa":
                qa_wos.append(wo_id)
                continue
            if role == "gitops" or agent == "local-llm":
                gitops_wos.append(wo_id)
            else:
                code_wos.append(wo_id)

            # Rework candidates are archived — resolve the deliverable from
            # ACTIVE, COMPLETED, or BLOCKED.
            deliv = ""
            for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
                wo_file = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
                if not wo_file.is_file():
                    continue
                try:
                    wdata = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                    d = wdata.get("deliverable") if isinstance(wdata, dict) else None
                    if isinstance(d, dict) and d.get("path"):
                        deliv = str(d["path"]).replace("\\", "/").strip().lstrip("/").lower()
                except Exception:
                    pass
                break
            if not deliv:
                if role != "gitops":
                    accused.append(wo_id)  # no mappable footprint — include defensively
                continue
            stem = Path(deliv).stem.lower()
            if (deliv in blocker_text) or (stem and stem in blocker_text):
                accused.append(wo_id)
        if accused:
            return list(dict.fromkeys(accused + qa_wos))
        return list(dict.fromkeys(code_wos + qa_wos + (gitops_wos if is_gitops_blocker else [])))

    def _unarchive_work_order(self, ws: Path, wo_id: str) -> bool:
        """Return a completed or blocked work order to ACTIVE so execution can re-dispatch it."""
        active = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
        if active.is_file():
            return self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
        for sub in ("COMPLETED", "BLOCKED"):
            cand = ws / ".sync" / "work-orders" / sub / f"{wo_id}.yaml"
            if cand.is_file():
                try:
                    data = yaml.safe_load(cand.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data["status"] = "ACTIVE"
                        data["updated"] = _now()
                        data.pop("error", None)
                        data.pop("blocked_reason", None)
                        active.parent.mkdir(parents=True, exist_ok=True)
                        active.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
                        cand.unlink()
                        self._mark_wo_status_on_disk(ws, wo_id, "ACTIVE", clear_error=True)
                        return True
                except Exception:
                    pass
        return False

    # ── Architect final acceptance (mandatory, plan-bound) ────────────

    def _final_acceptance_status(self, state: RunState, ws: Path) -> tuple[bool, Any, str]:
        """Deterministic + persisted evidence check at the run-completion boundary."""
        from validators.harness import review_gate

        report = review_gate.evaluate_final_acceptance(state, ws)
        if not report.passed:
            return False, report, "; ".join(report.reasons)
        verdict = review_gate.load_final_verdict(ws, state.run_id)
        ok, why = review_gate.validate_final_verdict(
            verdict,
            ws,
            run_id=state.run_id,
            plan_id=state.plan_id,
            required_wo_ids=review_gate.required_wo_ids_for_run(state, ws),
        )
        if not ok:
            return False, report, why
        return True, report, "final acceptance evidence is valid"

    def _advance_final_review(self, state: RunState) -> AdvanceResult:
        """FINAL_REVIEW: Architect audits the implementation against PLAN.md."""
        ws = Path(state.workspace)
        from validators.harness import review_gate

        report = review_gate.evaluate_final_acceptance(state, ws)
        if not report.passed:
            state.error = "FINAL_ACCEPTANCE_BLOCKED: " + "; ".join(report.reasons)
            state.final_review_history.append(
                {
                    "event": "prerequisites_unmet",
                    "attempt": state.final_review_rounds,
                    "reasons": list(report.reasons),
                    "timestamp": _now(),
                }
            )
            actionable = bool(
                report.missing_wo_ids or report.blocked_wo_ids or state.qa_rework_targets
            )
            if actionable and state.final_prerequisite_rounds < state.max_final_review_rounds + 2:
                state.final_prerequisite_rounds += 1
                self._transition(state, Phase.EXECUTING)
                return AdvanceResult.TRANSITIONED
            if actionable:
                state.error = (
                    "FINAL_ACCEPTANCE_BLOCKED: prerequisites remained unmet after "
                    f"{state.final_prerequisite_rounds} execution rounds: "
                    + "; ".join(report.reasons)
                )
                state.exit_code = 3
            self._transition(state, Phase.BLOCKED)
            return AdvanceResult.BLOCKED

        required = review_gate.required_wo_ids_for_run(state, ws)
        verdict = review_gate.load_final_verdict(ws, state.run_id)
        ok, why = review_gate.validate_final_verdict(
            verdict, ws, run_id=state.run_id, plan_id=state.plan_id, required_wo_ids=required,
        )
        if ok:
            state.error = None
            state.final_review_operation_id = None
            self._transition(state, Phase.PRODUCT_READY)
            return AdvanceResult.TRANSITIONED

        if state.final_review_operation_id:
            op = self._get_operation(state.final_review_operation_id)
            if op is not None and str(op.get("status", "")).upper() not in _OPERATION_TERMINAL:
                return AdvanceResult.WAITING_FOR_OPERATION
            state.final_review_operation_id = None
            verdict = review_gate.load_final_verdict(ws, state.run_id)
            ok, why = review_gate.validate_final_verdict(
                verdict, ws, run_id=state.run_id, plan_id=state.plan_id, required_wo_ids=required,
            )
            if ok:
                state.error = None
                self._transition(state, Phase.PRODUCT_READY)
                return AdvanceResult.TRANSITIONED

        if verdict is not None:
            # A rejected (or stale) final review routes bounded corrective work
            # through the existing rework mechanisms; it is never ignored.
            outcome = self._route_final_review_failure(state, ws, verdict, why)
            if outcome is not None:
                return outcome

        return self._dispatch_final_review(state, ws, required, report)

    def _route_final_review_failure(
        self, state: RunState, ws: Path, verdict: dict[str, Any], why: str,
    ) -> AdvanceResult | None:
        """Route Architect rejection through authorized, bounded corrective work."""
        from validators.harness import review_gate

        findings = list(review_gate._findings_of(verdict))
        actions = list(review_gate._actions_of(verdict))
        blockers = findings + actions or [why]
        state.final_review_history.append(
            {
                "event": "rejected",
                "attempt": state.final_review_rounds,
                "status": verdict.get("status"),
                "reason": why,
                "findings": findings,
                "required_actions": actions,
                "timestamp": _now(),
            }
        )
        review_gate.archive_final_verdict(ws, state.run_id, suffix="rejected")
        state.error = "FINAL_REVIEW_REJECTED: " + ("; ".join(blockers) or why)

        if state.final_review_rounds >= state.max_final_review_rounds:
            state.error = (
                f"FINAL_REVIEW_BUDGET_EXHAUSTED: {state.final_review_rounds}/"
                f"{state.max_final_review_rounds} corrective rounds used: {state.error}"
            )
            state.exit_code = 3
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN
        state.final_review_rounds += 1

        reworkable = self._reworkable_integration_wos(state, ws, blockers)
        reworkable = [
            w for w in reworkable
            if w not in (state.integration_wo_id, state.gitops_wo_id)
        ]
        if not reworkable:
            state.error += " (no authorized work order could be linked to the findings)"
            state.exit_code = 3
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

        feedback = "\n".join(f"- {b}" for b in blockers)
        parent_op_id = None
        if len(reworkable) > 1 and hasattr(self.manager, "begin_operation"):
            try:
                _, parent_op_id = self.manager.begin_operation(
                    state.session_id,
                    "parallel_dispatch",
                    {"work_orders": reworkable, "final_review_round": state.final_review_rounds},
                    role="supervisor",
                )
                state.batch_operation_id = parent_op_id
            except Exception:
                parent_op_id = None
        for wo_id in reworkable:
            # Any prior QA verdict for a corrected deliverable must be invalidated;
            # the fresh review is the only thing that can re-authorise acceptance.
            review_gate.archive_qa_verdict(ws, wo_id, suffix="invalidated-final-review")
            self._unarchive_work_order(ws, wo_id)
            if wo_id not in state.worker_wo_ids:
                state.worker_wo_ids.append(wo_id)
            if state.published_wo_ids and wo_id not in state.published_wo_ids:
                state.published_wo_ids.append(wo_id)
            for bucket in (state.completed_wo_ids, state.failed_wo_ids, state.blocked_wo_ids):
                if wo_id in bucket:
                    bucket.remove(wo_id)
            state.retry_counts[wo_id] = 0
            agent = self._agent_for_wo(wo_id, ws)
            role = self._role_for_agent(agent)
            prompt = (
                f"Corrective work for work order {wo_id} (Architect final review round "
                f"{state.final_review_rounds}/{state.max_final_review_rounds}).\n"
                f"The Architect rejected the completed implementation with these findings "
                f"and required corrections:\n{feedback}\n"
                "Apply each correction to the deliverables, then resubmit. The deliverables "
                "must pass deterministic verification and mandatory QA again."
            )
            if role == "qa":
                prompt += (
                    "\n\nYou are the QA agent: re-verify with your assigned suite and "
                    "write a fresh "
                    "structured verdict; do not modify application code."
                )
            try:
                self.manager.start_turn(
                    state.session_id,
                    prompt,
                    role=role,
                    agent_id=agent,
                    work_order_id=wo_id,
                    parent_operation_id=parent_op_id,
                )
            except OperationContentionError:
                pass
            except Exception:
                if wo_id not in state.failed_wo_ids:
                    state.failed_wo_ids.append(wo_id)
        self._transition(state, Phase.EXECUTING)
        return AdvanceResult.WAITING_FOR_OPERATION

    def _dispatch_final_review(
        self, state: RunState, ws: Path, required: list[str], report: Any,
    ) -> AdvanceResult:
        """Dispatch the Architect's plan-to-implementation acceptance audit."""
        from validators.harness import review_gate

        if state.final_review_rounds > state.max_final_review_rounds:
            state.error = "FINAL_REVIEW_BUDGET_EXHAUSTED: no final-review rounds remain"
            state.exit_code = 3
            self._transition(state, Phase.WAITING_FOR_HUMAN)
            return AdvanceResult.WAITING_FOR_HUMAN

        verdict_path = review_gate.final_verdict_path(ws, state.run_id)
        try:
            rel_verdict = verdict_path.relative_to(ws).as_posix()
        except Exception:
            rel_verdict = str(verdict_path)
        plan_digest = report.plan_hash_value or ""
        reviewed = ",\n".join(
            f'    {{"id": "{wo_id}", "subject_hash": "{review_gate.subject_hash(ws, wo_id)}"}}'
            for wo_id in required
        )
        prompt = (
            "Architect final acceptance review.\n\n"
            f"The approved plan is `PLAN.md` (plan id {state.plan_id!r}, sha256 {plan_digest}).\n"
            "All required work orders are complete, deterministically verified and QA-approved. "
            "Audit the actual implementation against the authoritative approved plan before the "
            "run may be declared successful.\n\n"
            "Evaluate:\n"
            "1. Coverage of every required milestone and acceptance criterion in PLAN.md.\n"
            "2. Completion and verification of every required work order listed below.\n"
            "3. QA verdicts for all applicable work orders.\n"
            "4. Existence and validity of the required deliverables.\n"
            "5. Backend/frontend integration and end-to-end behaviour.\n"
            "6. Test, build and dependency evidence (inspect the recorded evidence; "
            "do not assume).\n"
            "7. Unresolved blockers, defects and known limitations.\n"
            "8. Scope deviations or conflicting implementations.\n"
            "9. Consistency between the declared result and the repository state.\n\n"
            "Derive requirements from PLAN.md and the work-order contracts only. If any mandatory "
            "criterion is unmet, set status FAIL with reproducible findings and "
            "required corrections.\n\n"
            f"Persist the verdict as JSON to `${rel_verdict}` using write_file, "
            "in exactly this shape:\n"
            "{\n"
            f'  "schema": "{review_gate.FINAL_REVIEW_SCHEMA}",\n'
            f'  "run_id": "{state.run_id}",\n'
            f'  "plan_id": "{state.plan_id}",\n'
            f'  "plan_hash": "{plan_digest}",\n'
            '  "status": "PASS" | "FAIL",\n'
            '  "reviewer": "claude",\n'
            '  "reviewer_role": "architecture",\n'
            '  "timestamp": "<ISO-8601 UTC>",\n'
            '  "reviewed_work_orders": [\n'
            f"{reviewed}\n"
            '  ],\n'
            '  "acceptance_criteria": [{"id": "...", "description": "...", '
            '"outcome": "PASS|FAIL", "evidence": "..."}],\n'
            '  "evidence": [{"kind": "test|build|inspection", "command": "...", '
            '"result": "...", "passed": true}],\n'
            '  "findings": [{"severity": "critical|major|minor", '
            '"detail": "..."}],\n'
            '  "required_actions": ["..."],\n'
            f'  "attempt": {state.final_review_rounds + 1}\n'
            "}\n\n"
            "Do not modify application code or work-order state during this audit. Finish by "
            "declaring status 'completed' after writing the verdict."
        )
        try:
            # Reviewers run under their agent contract, not a work-order
            # contract, so the review channel is authorized independently of
            # the deliverables being audited.
            op = self.manager.start_turn(
                state.session_id,
                prompt,
                role="architecture",
                agent_id="claude",
            )
            state.final_review_operation_id = (op or {}).get("operation_id")
            state.error = None
            try:
                self.save_run_state(state, ws)
            except Exception:
                pass
            return AdvanceResult.WAITING_FOR_OPERATION
        except OperationContentionError:
            return AdvanceResult.WAITING_FOR_OPERATION
        except Exception as exc:
            state.error = f"Final review dispatch failed: {exc}"
            state.final_review_rounds += 1
            if state.final_review_rounds > state.max_final_review_rounds:
                state.exit_code = 3
                self._transition(state, Phase.WAITING_FOR_HUMAN)
                return AdvanceResult.WAITING_FOR_HUMAN
            return AdvanceResult.TRANSITIONED

    @staticmethod
    def _sha256_file(path: Path) -> str | None:
        import hashlib
        if not path.is_file():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            return None

    def _run_readiness_gate(self, state: RunState, ws: Path) -> Any:
        """Compile the authored set, then run the readiness gate on the canonical result.

        The deterministic compiler runs first: it injects system-owned
        invariants (QA verdict channel, milestone identity) and routes
        non-normalizable intent straight to repair.  The readiness gate then
        validates the canonical set and remains authoritative.
        """
        from validators.harness.authoring_compiler import compile_authoring_artifacts
        from validators.harness.authoring_readiness import (
            AuthoringReadinessResult,
            load_plan_milestones,
            milestone_matches,
            validate_authoring_readiness,
        )
        plan_milestones = load_plan_milestones(ws)

        # Deterministic preflight/normalization pass (SYSTEM-NORMALIZABLE
        # fixes are applied in place; ARCHITECT-DECISION-REQUIRED findings
        # short-circuit into a targeted repair turn).
        compile_result = compile_authoring_artifacts(ws, plan_milestones, planning_wo_id=state.planning_wo_id)
        if compile_result.decision_required:
            result = AuthoringReadinessResult(
                ready=False,
                plan_id=state.plan_id or "",
                issues=tuple(compile_result.decision_required),
            )
            self._persist_readiness_report(state, ws, result)
            return result

        expected = plan_milestones
        if expected and state.milestone_exemptions:
            expected = [
                ref for ref in expected
                if not any(
                    milestone_matches(exempt, ref["title"])
                    or (ref["id"] and ref["id"].lower() == str(exempt).strip().lower())
                    for exempt in state.milestone_exemptions
                )
            ]
        result = validate_authoring_readiness(
            ws,
            plan_id=state.plan_id or "",
            expected_milestones=expected,
            planning_wo_id=state.planning_wo_id,
            completed_wo_ids=state.completed_wo_ids,
        )
        self._persist_readiness_report(state, ws, result)
        return result

    def _persist_readiness_report(self, state: RunState, ws: Path, result: Any) -> None:
        """Persist the readiness verdict for audit under the governed report path."""
        try:
            report_dir = ws / ".sync" / "reports" / "authoring" / state.run_id
            report_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": state.run_id,
                "plan_id": state.plan_id,
                "authoring_revision": state.authoring_revision,
                "phase": state.phase.value,
                "ready": bool(result.ready),
                "artifacts": list(result.artifact_paths),
                "issues": [i.to_dict() for i in result.issues],
                "warnings": list(getattr(result, "warnings", ()) or ()),
                "evaluated_at": _now(),
            }
            (report_dir / f"rev{state.authoring_revision}-readiness.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _archive_rejected_artifacts(
        self, state: RunState, ws: Path, issues: Any = None,
    ) -> list[str]:
        """Move failing governed artifacts to the audit path, preserving diagnostics.

        Only artifacts whose own content failed (parse/schema/budget/scope) are
        archived; relational findings (missing contract, dependency problems)
        stay in place so the repair turn can correct them surgically.
        Returns the list of archived artifact paths.
        """
        raw_issues: list[Any] = list(issues) if issues is not None else state.readiness_issues

        def _get(issue: Any, key: str) -> Any:
            if isinstance(issue, dict):
                return issue.get(key)
            return getattr(issue, key, None)

        content_failure_codes = {
            "BUDGET_MISSING", "BUDGET_INVALID", "BUDGET_UNDER_ESTIMATE",
            "DELIVERABLE_OUTSIDE_SCOPE", "OVERWRITE_CONFLICT",
        }
        archived: list[str] = []
        seen_paths: set[str] = set()
        for issue in raw_issues:
            category = str(_get(issue, "category") or "")
            code = str(_get(issue, "code") or "")
            rel = _get(issue, "artifact_path")
            if not rel or not isinstance(rel, str) or rel in seen_paths:
                continue
            if category not in ("yaml", "schema") and code not in content_failure_codes:
                continue
            if ".sync/work-orders/ACTIVE/" not in rel and ".sync/contracts/" not in rel:
                continue
            seen_paths.add(rel)
            source = ws / rel
            if not source.is_file():
                continue
            try:
                audit_dir = (
                    ws / ".sync" / "reports" / "authoring" / state.run_id
                    / f"rev{state.authoring_revision}"
                )
                audit_dir.mkdir(parents=True, exist_ok=True)
                target = audit_dir / Path(rel).name
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                source.unlink()
                archived.append(rel)
            except Exception:
                continue
        if archived:
            try:
                audit_dir = (
                    ws / ".sync" / "reports" / "authoring" / state.run_id
                    / f"rev{state.authoring_revision}"
                )
                (audit_dir / "rejected-artifacts.yaml").write_text(
                    yaml.safe_dump({
                        "archived_at": _now(),
                        "authoring_revision": state.authoring_revision,
                        "artifacts": archived,
                        "issues": [
                            i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in raw_issues
                        ],
                    }, sort_keys=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return archived
