"""Mandatory QA review and Architect final-acceptance gates.

This module is the single source of truth for the two review gates required by
SDP-QA-ARCHITECT-REVIEW:

1. **QA gate** — a backend/frontend work order may not be accepted merely
   because its worker declared completion.  A persisted, revision-bound QA
   verdict must exist and pass.
2. **Final acceptance gate** — a run may not reach a successful terminal state
   merely because every work order reports ``COMPLETED``.  A persisted
   Architect final-review verdict must pass against the authoritative approved
   plan and the current deliverable revisions.

The implementation deliberately reuses the mechanisms that already exist in the
repository instead of introducing a parallel review framework:

* verdicts are stored under ``.sync/reviews/**`` alongside the existing
  ``.sync/reports/**`` governance artifacts and written with
  :func:`validators.kernel.io.durable_write_text` (atomic rename + fsync);
* deliverable content hashing reuses the same sha256 convention as
  :mod:`validators.harness.snapshot` and :mod:`validators.harness.integration_gate`;
* role/applicability decisions reuse the canonical agent→role mapping already
  used by the supervisor (``gemma`` → ``qa``).

Review freshness is explicit and revision based: every verdict records a
``subject_hash`` derived from the reviewed deliverable bytes and the work-order
definition.  If either changes after review the verdict becomes stale and is
rejected, so a timestamp alone can never authorise acceptance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from validators.kernel.io import durable_write_text

QA_VERDICT_SCHEMA = "stackmind.qa-verdict.v1"
FINAL_REVIEW_SCHEMA = "stackmind.final-review.v1"

QA_PASS = "PASS"
QA_FAIL = "FAIL"

# Canonical agent → role mapping (same table the supervisor uses for dispatch).
AGENT_ROLES = {
    "claude": "architecture",
    "codex": "backend",
    "gemini": "frontend",
    "gemma": "qa",
    "local-llm": "gitops",
}

# Roles whose deliverables produce application code or user-facing behaviour and
# therefore require the mandatory QA gate.  Architecture/QA/GitOps work orders
# keep their existing (unchanged) verification treatment.
QA_REQUIRED_ROLES = {"backend", "frontend"}

# Work orders that are orchestration bookkeeping rather than deliverable work.
_BOOKKEEPING_IDS = {"WO-000"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(path: str | Path) -> str:
    return str(path).replace("\\", "/").strip().lstrip("/")


def role_for_agent(agent: str | None) -> str:
    return AGENT_ROLES.get(str(agent or "").lower().strip(), "backend")


# ─── Work-order discovery ─────────────────────────────────────────────

def find_wo_file(workspace: Path | str, wo_id: str) -> Path | None:
    """Locate a work-order YAML across ACTIVE/BLOCKED/COMPLETED archives."""
    ws = Path(workspace)
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED"):
        candidate = ws / ".sync" / "work-orders" / bucket / f"{wo_id}.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_wo(workspace: Path | str, wo_id: str) -> dict[str, Any]:
    path = find_wo_file(workspace, wo_id)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def wo_deliverable_paths(data: dict[str, Any]) -> list[str]:
    """All declared deliverable paths for a work order (primary + extras)."""
    paths: list[str] = []
    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict) and deliverable.get("path"):
        paths.append(_norm(deliverable["path"]))
    elif isinstance(deliverable, str) and deliverable.strip():
        paths.append(_norm(deliverable))
    for extra in data.get("deliverables") or []:
        if isinstance(extra, dict) and extra.get("path"):
            paths.append(_norm(extra["path"]))
        elif isinstance(extra, str) and extra.strip():
            paths.append(_norm(extra))
    out: list[str] = []
    for p in paths:
        if p and p not in out:
            out.append(p)
    return out


def wo_deliverable_types(data: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict) and deliverable.get("type"):
        types.add(str(deliverable["type"]).lower())
    for extra in data.get("deliverables") or []:
        if isinstance(extra, dict) and extra.get("type"):
            types.add(str(extra["type"]).lower())
    return types


def wo_agents(data: dict[str, Any]) -> list[str]:
    assigned = data.get("assigned_agents") or data.get("agent") or []
    if isinstance(assigned, str):
        assigned = [assigned]
    return [str(a).lower().strip() for a in assigned if str(a).strip()]


def is_qa_work_order(data: dict[str, Any]) -> bool:
    """True when the work order belongs to the QA role (verdict producer)."""
    if str(data.get("role", "")).lower().strip() in ("qa", "q/a"):
        return True
    return any(role_for_agent(a) == "qa" for a in wo_agents(data))


def is_gitops_work_order(data: dict[str, Any]) -> bool:
    if str(data.get("role", "")).lower().strip() == "gitops":
        return True
    return any(role_for_agent(a) == "gitops" for a in wo_agents(data))


def requires_qa(workspace: Path | str, wo_id: str, data: dict[str, Any] | None = None) -> bool:
    """Decide whether a work order must pass the mandatory QA gate.

    Applicability follows the existing contracts and conventions rather than a
    new classification system:

    * QA, GitOps and planning/bookkeeping work orders are exempt (they either
      produce verdicts, are release bookkeeping, or are verified deterministically).
    * Documentation-only work orders are exempt unless they declare application
      code/module deliverables or a backend/frontend role.
    """
    data = data if data is not None else load_wo(workspace, wo_id)
    if not data:
        return False
    if wo_id in _BOOKKEEPING_IDS:
        return False
    if is_qa_work_order(data) or is_gitops_work_order(data):
        return False
    role = str(data.get("role", "")).lower().strip() or role_for_agent(
        (wo_agents(data) or ["codex"])[0]
    )
    if role in ("architecture", "gitops", "qa"):
        return False
    types = wo_deliverable_types(data)
    if role in QA_REQUIRED_ROLES:
        return True
    return bool(types & {"code", "module", "api", "component"})


# ─── Subject hashing (revision identity) ──────────────────────────────

def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def subject_hash(workspace: Path | str, wo_id: str, data: dict[str, Any] | None = None) -> str:
    """Deterministic revision hash of the reviewed subject.

    Covers the work-order definition (minus volatile bookkeeping fields) and the
    exact bytes of every declared deliverable.  Any change to either invalidates
    a previously issued verdict.
    """
    ws = Path(workspace)
    data = data if data is not None else load_wo(ws, wo_id)
    payload: dict[str, Any] = {
        "work_order_id": wo_id,
        "deliverables": wo_deliverable_paths(data),
        "acceptance_criteria": data.get("acceptance_criteria") or [],
        "tasks": data.get("tasks") or [],
        "dependencies": data.get("dependencies") or [],
    }
    files: dict[str, str | None] = {}
    for rel in payload["deliverables"]:
        files[rel] = file_sha256(ws / rel)
    payload["files"] = files
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def plan_hash(workspace: Path | str) -> str | None:
    return file_sha256(Path(workspace) / "PLAN.md")


# ─── Verdict persistence ──────────────────────────────────────────────

def qa_verdict_path(workspace: Path | str, wo_id: str) -> Path:
    return Path(workspace) / ".sync" / "reviews" / "qa" / f"{wo_id}.json"


def final_verdict_path(workspace: Path | str, run_id: str) -> Path:
    return Path(workspace) / ".sync" / "reviews" / "final" / f"{run_id}.json"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    durable_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def persist_qa_verdict(workspace: Path | str, verdict: dict[str, Any]) -> Path:
    path = qa_verdict_path(workspace, str(verdict["work_order_id"]))
    _write_json(path, verdict)
    return path


def load_qa_verdict(workspace: Path | str, wo_id: str) -> dict[str, Any] | None:
    return _read_json(qa_verdict_path(workspace, wo_id))


def archive_qa_verdict(workspace: Path | str, wo_id: str, suffix: str = "stale") -> Path | None:
    """Move a verdict aside so it can never authorise acceptance again."""
    path = qa_verdict_path(workspace, wo_id)
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    target = path.with_name(f"{path.stem}-{suffix}-{stamp}.json")
    try:
        path.replace(target)
    except Exception:
        return None
    return target


def persist_final_verdict(workspace: Path | str, verdict: dict[str, Any]) -> Path:
    path = final_verdict_path(workspace, str(verdict["run_id"]))
    _write_json(path, verdict)
    return path


def load_final_verdict(workspace: Path | str, run_id: str) -> dict[str, Any] | None:
    return _read_json(final_verdict_path(workspace, run_id))


def archive_final_verdict(
    workspace: Path | str, run_id: str, suffix: str = "superseded"
) -> Path | None:
    """Retire a final-review verdict so stale evidence cannot be reused."""
    path = final_verdict_path(workspace, run_id)
    if not path.is_file():
        return None
    target = path.with_name(
        f"{path.stem}-{suffix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.json"
    )
    try:
        path.replace(target)
    except Exception:
        return None
    return target


# ─── Verdict validation ───────────────────────────────────────────────

def validate_qa_verdict(
    verdict: dict[str, Any] | None, workspace: Path | str, wo_id: str
) -> tuple[bool, str]:
    """Validate structure *and* freshness of a QA verdict. Fail-closed."""
    if not isinstance(verdict, dict):
        return False, "no QA verdict persisted"
    if str(verdict.get("schema", "")) != QA_VERDICT_SCHEMA:
        return False, f"unsupported QA verdict schema {verdict.get('schema')!r}"
    if str(verdict.get("work_order_id", "")) != wo_id:
        return False, "QA verdict targets a different work order"

    status = str(verdict.get("verdict", "")).upper()
    if status not in (QA_PASS, QA_FAIL):
        return False, f"QA verdict must be {QA_PASS} or {QA_FAIL}, got {status!r}"

    criteria = verdict.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return False, "QA verdict declares no evaluated criteria (unverified claim)"

    current = subject_hash(workspace, wo_id)
    recorded = str(verdict.get("subject_hash", ""))
    if not recorded or recorded != current:
        return False, "QA verdict is stale: the reviewed deliverable revision has changed"

    data = load_wo(workspace, wo_id)
    if status == QA_PASS:
        failed = [
            c for c in criteria
            if isinstance(c, dict)
            and str(c.get("outcome", "")).upper() not in (QA_PASS, "APPROVED", "N/A")
        ]
        if failed:
            return False, "QA verdict claims PASS while criteria are not all satisfied"
        findings = verdict.get("findings") or []
        critical = [
            f for f in findings
            if isinstance(f, dict)
            and str(f.get("severity", "")).lower() in ("critical", "blocker", "major")
        ]
        if critical:
            return False, "QA verdict claims PASS while critical findings are recorded"
        if verdict.get("required_actions"):
            return False, "QA verdict claims PASS while corrective actions are still required"
        evidence = verdict.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return False, "QA verdict declares no executed evidence (unverified claim)"
        for item in evidence:
            if isinstance(item, dict) and item.get("passed") is False:
                return False, "QA verdict PASS contradicts recorded failing evidence"
        types = wo_deliverable_types(data)
        needs_test_evidence = bool(types & {"code", "module", "api", "component"}) or bool(
            wo_deliverable_paths(data)
        )
        if needs_test_evidence:
            def _is_test_or_build(item: Any) -> bool:
                if not isinstance(item, dict):
                    return False
                if str(item.get("kind", "")).lower() in ("test", "build"):
                    return True
                cmd = str(item.get("command", "")).lower()
                tokens = ("pytest", "unittest", "test", "build", "npm", "vite", "make")
                return any(tok in cmd for tok in tokens)
            if not any(_is_test_or_build(item) for item in evidence):
                return False, "QA verdict PASS without test/build execution evidence"
        return True, "QA verdict accepted"
    return False, "QA verdict is FAIL: the implementation was rejected by QA"


def validate_final_verdict(
    verdict: dict[str, Any] | None,
    workspace: Path | str,
    *,
    run_id: str,
    plan_id: str | None,
    required_wo_ids: list[str],
) -> tuple[bool, str]:
    """Validate structure *and* freshness of the Architect final-review verdict."""
    if not isinstance(verdict, dict):
        return False, "no Architect final-review verdict persisted"
    if str(verdict.get("schema", "")) != FINAL_REVIEW_SCHEMA:
        return False, f"unsupported final-review schema {verdict.get('schema')!r}"
    if str(verdict.get("run_id", "")) != run_id:
        return False, "final-review verdict belongs to a different run"

    status = str(verdict.get("status", "")).upper()
    if status not in (QA_PASS, QA_FAIL):
        return False, f"final-review verdict must be {QA_PASS} or {QA_FAIL}, got {status!r}"

    if plan_id and str(verdict.get("plan_id", "")) != plan_id:
        return False, "final-review verdict was issued for a different plan revision"

    current_plan = plan_hash(workspace)
    if not current_plan or str(verdict.get("plan_hash", "")) != current_plan:
        return False, "final-review verdict is stale: the approved plan has changed since review"

    reviewed = verdict.get("reviewed_work_orders") or []
    reviewed_map = {
        str(item.get("id")): item for item in reviewed if isinstance(item, dict) and item.get("id")
    }
    for wo_id in required_wo_ids:
        item = reviewed_map.get(wo_id)
        if item is None:
            return False, f"final-review verdict did not review required work order {wo_id}"
        if str(item.get("subject_hash", "")) != subject_hash(workspace, wo_id):
            return False, f"final-review verdict is stale for {wo_id}: deliverable revision changed"

    if status == QA_PASS:
        criteria = verdict.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria:
            return False, "final-review verdict declares no acceptance criteria"
        failed = [
            c for c in criteria
            if isinstance(c, dict)
            and str(c.get("outcome", "")).upper() not in (QA_PASS, "MET", "N/A")
        ]
        if failed:
            return False, "final-review verdict claims PASS while acceptance criteria are unmet"
        if verdict.get("required_actions"):
            return False, "final-review verdict claims PASS while corrective actions remain"
        findings = verdict.get("findings") or []
        if any(
            isinstance(f, dict) and str(f.get("severity", "")).lower() in ("critical", "blocker")
            for f in findings
        ):
            return False, "final-review verdict claims PASS with unresolved critical findings"
        if not isinstance(verdict.get("evidence"), list) or not verdict.get("evidence"):
            return False, "final-review verdict declares no evidence"
        return True, "final-review verdict accepted"
    return False, "final-review verdict is FAIL: the Architect rejected the implementation"


# ─── Gate evaluation ──────────────────────────────────────────────────

@dataclass(frozen=True)
class GateDecision:
    """Outcome of one review gate, safe to persist in the audit trail."""

    passed: bool
    subject_id: str
    reason: str
    detail: str | None = None
    pending: bool = False
    verdict: dict[str, Any] | None = None
    findings: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "subject_id": self.subject_id,
            "reason": self.reason,
            "detail": self.detail,
            "pending": self.pending,
            "verdict": self.verdict,
            "findings": list(self.findings),
            "required_actions": list(self.required_actions),
        }


def _findings_of(verdict: dict[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for item in verdict.get("findings") or []:
        if isinstance(item, dict):
            detail = str(item.get("detail") or item.get("finding") or "").strip()
            if detail:
                sev = str(item.get("severity", "")).strip()
                out.append(f"[{sev}] {detail}" if sev else detail)
        elif str(item).strip():
            out.append(str(item))
    return tuple(out)


def _actions_of(verdict: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(a) for a in (verdict.get("required_actions") or []) if str(a).strip()
    )


def evaluate_qa_gate(workspace: Path | str, wo_id: str) -> GateDecision:
    """Evaluate the mandatory QA gate for one work order (fail-closed)."""
    data = load_wo(workspace, wo_id)
    if not requires_qa(workspace, wo_id, data):
        return GateDecision(
            passed=True,
            subject_id=wo_id,
            reason="work order is not in the mandatory QA scope",
            detail="deterministic deliverable verification still applies",
        )
    verdict = load_qa_verdict(workspace, wo_id)
    ok, why = validate_qa_verdict(verdict, workspace, wo_id)
    if ok:
        return GateDecision(
            passed=True, subject_id=wo_id, reason=why, verdict=verdict,
        )
    pending = verdict is None
    return GateDecision(
        passed=False,
        subject_id=wo_id,
        reason="QA gate not satisfied",
        detail=why,
        pending=pending,
        verdict=verdict,
        findings=_findings_of(verdict or {}),
        required_actions=_actions_of(verdict or {}),
    )


@dataclass(frozen=True)
class FinalAcceptanceReport:
    """Deterministic preconditions for Architect final acceptance."""

    passed: bool
    reasons: tuple[str, ...] = ()
    required_wo_ids: tuple[str, ...] = ()
    missing_wo_ids: tuple[str, ...] = ()
    qa_failures: tuple[str, ...] = ()
    blocked_wo_ids: tuple[str, ...] = ()
    integration_passed: bool = False
    plan_id: str | None = None
    plan_hash_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "required_work_orders": list(self.required_wo_ids),
            "missing_work_orders": list(self.missing_wo_ids),
            "qa_failures": list(self.qa_failures),
            "blocked_work_orders": list(self.blocked_wo_ids),
            "integration_passed": self.integration_passed,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash_value,
        }


def required_wo_ids_for_run(state: Any, workspace: Path | str) -> list[str]:
    """Work orders that must be completed, verified and QA-passed for acceptance."""
    superseded = set(getattr(state, "superseded_wo_ids", []) or [])
    # Planning, release and the Architect's own integration-review work order
    # are orchestration bookkeeping, never acceptance targets.
    excluded = {
        str(getattr(state, "planning_wo_id", None) or ""),
        "WO-000",
        str(getattr(state, "gitops_wo_id", None) or ""),
        str(getattr(state, "integration_wo_id", None) or ""),
    }
    out: list[str] = []
    ids = list(getattr(state, "worker_wo_ids", []) or []) + list(
        getattr(state, "published_wo_ids", []) or []
    )
    for wo_id in ids:
        wo_id = str(wo_id)
        if not wo_id or wo_id in superseded or wo_id in excluded:
            continue
        data = load_wo(workspace, wo_id)
        if not data and wo_id not in (getattr(state, "completed_wo_ids", []) or []):
            continue
        if is_gitops_work_order(data):
            continue
        if wo_id not in out:
            out.append(wo_id)
    return out


def evaluate_final_acceptance(state: Any, workspace: Path | str) -> FinalAcceptanceReport:
    """Deterministic checks required before an Architect final review may pass."""
    ws = Path(workspace)
    reasons: list[str] = []

    plan_id = getattr(state, "plan_id", None)
    current_plan = plan_hash(ws)
    if not plan_id:
        reasons.append("no user-approved plan is recorded for this run")
    if not current_plan:
        reasons.append("PLAN.md is missing from the workspace")

    required = required_wo_ids_for_run(state, ws)
    completed = {str(w) for w in (getattr(state, "completed_wo_ids", []) or [])}
    missing = [w for w in required if w not in completed]
    if missing:
        reasons.append(f"required work orders are not completed: {', '.join(missing)}")

    integrated = getattr(state, "integration_wo_id", None)
    if integrated:
        completed.add(str(integrated))

    qa_failures: list[str] = []
    for wo_id in required:
        if wo_id in missing:
            continue
        decision = evaluate_qa_gate(ws, wo_id)
        if not decision.passed:
            qa_failures.append(f"{wo_id}: {decision.detail or decision.reason}")
    if qa_failures:
        reasons.append("mandatory QA gate is not satisfied for: " + "; ".join(qa_failures))

    blocked = [str(w) for w in (getattr(state, "blocked_wo_ids", []) or [])]
    if blocked:
        reasons.append(f"work orders remain blocked: {', '.join(blocked)}")

    pending_qa = [
        str(k) for k, v in (getattr(state, "qa_rework_targets", {}) or {}).items() if v
    ]
    if pending_qa:
        reasons.append(f"QA rework is still pending for: {', '.join(pending_qa)}")

    evidence = getattr(state, "integration_evidence", {}) or {}
    integration_passed = bool(evidence.get("passed")) and not list(
        getattr(state, "integration_blockers", []) or []
    )
    if not integration_passed:
        reasons.append("the Architect integration review has not passed with clean evidence")

    return FinalAcceptanceReport(
        passed=not reasons,
        reasons=tuple(reasons),
        required_wo_ids=tuple(required),
        missing_wo_ids=tuple(missing),
        qa_failures=tuple(qa_failures),
        blocked_wo_ids=tuple(blocked),
        integration_passed=integration_passed,
        plan_id=plan_id,
        plan_hash_value=current_plan,
    )


def review_status(state: Any, workspace: Path | str) -> dict[str, Any]:
    """Authoritative, restart-safe review snapshot for operators and the TUI."""
    ws = Path(workspace)
    required = required_wo_ids_for_run(state, ws)
    completed = {str(w) for w in (getattr(state, "completed_wo_ids", []) or [])}
    qa_entries: list[dict[str, Any]] = []
    awaiting_qa: list[str] = []
    failed_qa: list[str] = []
    stale_qa: list[str] = []
    for wo_id in required:
        if not requires_qa(ws, wo_id):
            continue
        verdict = load_qa_verdict(ws, wo_id)
        decision = evaluate_qa_gate(ws, wo_id)
        entry = {
            "work_order_id": wo_id,
            "completed": wo_id in completed,
            "passed": decision.passed,
            "reason": decision.detail or decision.reason,
            "verdict": (verdict or {}).get("verdict"),
            "reviewer": (verdict or {}).get("reviewer"),
            "reviewed_at": (verdict or {}).get("timestamp"),
            "findings": list(decision.findings),
            "required_actions": list(decision.required_actions),
            "rework_attempt": (verdict or {}).get("rework_attempt"),
            "rework_budget": (verdict or {}).get("rework_budget"),
        }
        qa_entries.append(entry)
        if decision.passed:
            continue
        if verdict is None:
            awaiting_qa.append(wo_id)
        elif "stale" in str(decision.detail):
            stale_qa.append(wo_id)
        else:
            failed_qa.append(wo_id)

    final_report = evaluate_final_acceptance(state, ws)
    final_verdict = load_final_verdict(ws, str(getattr(state, "run_id", "")))
    final_ok, final_why = validate_final_verdict(
        final_verdict,
        ws,
        run_id=str(getattr(state, "run_id", "")),
        plan_id=getattr(state, "plan_id", None),
        required_wo_ids=required,
    )
    final_verdict_view: dict[str, Any] | None = None
    if final_verdict is not None:
        final_verdict_view = {
            "status": final_verdict.get("status"),
            "reviewer": final_verdict.get("reviewer"),
            "timestamp": final_verdict.get("timestamp"),
            "fresh": final_ok,
            "freshness_detail": final_why,
            "findings": _findings_of(final_verdict),
            "required_actions": _actions_of(final_verdict),
            "attempt": final_verdict.get("attempt"),
        }

    return {
        "run_id": getattr(state, "run_id", None),
        "phase": str(getattr(state, "phase", "")),
        "plan_id": getattr(state, "plan_id", None),
        "qa_required_work_orders": [e["work_order_id"] for e in qa_entries],
        "qa_reviews": qa_entries,
        "awaiting_qa": awaiting_qa,
        "failed_qa": failed_qa,
        "stale_qa": stale_qa,
        "qa_review_attempts": dict(getattr(state, "qa_review_attempts", {}) or {}),
        "qa_reworks": dict(getattr(state, "qa_reworks", {}) or {}),
        "max_qa_reworks": getattr(state, "max_qa_reworks", 2),
        "final_review": {
            "pending": not final_ok,
            "operation_id": getattr(state, "final_review_operation_id", None),
            "rounds": getattr(state, "final_review_rounds", 0),
            "max_rounds": getattr(state, "max_final_review_rounds", 2),
            "can_accept": final_report.passed,
            "blocking_reasons": list(final_report.reasons),
            "verdict": final_verdict_view,
        },
        "acceptable": final_report.passed and final_ok,
    }
