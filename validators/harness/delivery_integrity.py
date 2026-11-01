"""Authoritative end-to-end delivery integrity evaluation (SDP-FIX-009).

One deterministic evidence layer over the existing production gates. It does not
replace the supervisor, the contract gate, the QA verdict store, or the
integration gate: it evaluates their authoritative artifacts and answers a
single question — can this workspace be truthfully delivered?

Stages, in lifecycle order:

* :func:`evaluate_planning_completeness` — executable plan / work-breakdown
  completeness, ownership, criteria mapping, dependencies, QA/integration/
  release responsibility, duplicate and placeholder detection.
* :func:`evaluate_artifact_integrity` — declared deliverables exist, are
  non-empty and non-placeholder, and match the completion-record hashes.
* :func:`evaluate_qa_evidence` — revision-bound QA verdicts, with explicit
  NOT_TESTED / RUNNING / PASSED / FAILED / INCONCLUSIVE / BLOCKED status.
* :func:`evaluate_integration` — the existing :class:`IntegrationGate` plus
  combined-product checks (missing artifacts/manifests across work orders).
* :func:`evaluate_release_readiness` — fail-closed aggregation of all of the
  above, plus stale-evidence, blocker, documentation, and governance checks.

Every evaluator returns immutable decisions with a deterministic evidence
payload, so results can be persisted and audited.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from validators.harness import review_gate
from validators.harness.integration_gate import IntegrationGate

PLANNING_GATE = "planning"
ARTIFACT_GATE = "artifacts"
QA_GATE = "qa"
INTEGRATION_GATE = "integration"
RELEASE_GATE = "release"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
NOT_APPLICABLE = "not_applicable"

# Canonical QA evidence statuses (upper-case, operator-facing).
QA_PASSED = "PASSED"
QA_FAILED = "FAILED"
NOT_TESTED = "NOT_TESTED"
QA_RUNNING = "RUNNING"
QA_INCONCLUSIVE = "INCONCLUSIVE"
QA_BLOCKED = "BLOCKED"

EXECUTABLE_TYPES = {"FEATURE", "CODE", "MODULE", "API", "COMPONENT", "INTEGRATION", "GITOPS", "RELEASE"}
DOC_EXTENSIONS = {".md", ".txt", ".rst"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".go", ".rs", ".java", ".sql"}

PLACEHOLDER_PATTERNS = (
    re.compile(r"\bTODO\b"),
    re.compile(r"raise\s+NotImplementedError"),
    re.compile(r"\bnot\s+implemented\b", re.IGNORECASE),
    re.compile(r"<placeholder>", re.IGNORECASE),
    re.compile(r"^\s*pass\s*#\s*placeholder\s*$", re.IGNORECASE | re.MULTILINE),
)


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle transition model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransitionRule:
    """One auditable lifecycle transition.

    ``preconditions`` and ``evidence`` are the authoritative inputs; display
    labels and cached status are never sufficient.
    """

    source: str
    destination: str
    preconditions: tuple[str, ...]
    authorization: str
    evidence: tuple[str, ...]
    failure_behavior: str
    recovery_behavior: str
    audit_event: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "preconditions": list(self.preconditions),
            "authorization": self.authorization,
            "evidence": list(self.evidence),
            "failure_behavior": self.failure_behavior,
            "recovery_behavior": self.recovery_behavior,
            "audit_event": self.audit_event,
        }


LIFECYCLE_TRANSITIONS: tuple[TransitionRule, ...] = (
    TransitionRule(
        "INITIALIZING", "PLANNING",
        ("workspace initialized", "product goal recorded"),
        "operator_session",
        ("session.operation_id",),
        "stay INITIALIZING and surface the initialization error",
        "re-run initialization; no artifact is overwritten",
        "lifecycle.initialized",
    ),
    TransitionRule(
        "PLANNING", "AWAITING_APPROVAL",
        ("PLAN.md valid", "planning work order authored"),
        "architecture_author",
        ("plan_hash", "planning_operation_id", "plan_id"),
        "stay PLANNING; authoring repair is bounded",
        "authorized Architecture repair re-authors and re-validates",
        "plan.proposed",
    ),
    TransitionRule(
        "AWAITING_APPROVAL", "PLAN_APPROVED",
        ("human approval recorded for the current plan revision",),
        "human_operator",
        ("approval_event", "plan_id", "plan_hash"),
        "remain awaiting approval; execution cannot dispatch",
        "re-approve after an authorized plan revision",
        "plan.approved",
    ),
    TransitionRule(
        "PLAN_APPROVED", "DISPATCHING",
        ("planning completeness passed", "work orders and contracts published"),
        "supervisor",
        ("plan_hash", "work_order_ids", "contract_revisions", "dependency_graph"),
        "transition to BLOCKED with a planning-integrity blocker",
        "authorized Architecture repair; completed work is preserved",
        "work_orders.published",
    ),
    TransitionRule(
        "DISPATCHING", "EXECUTING",
        ("at least one eligible work order", "dependency evidence current"),
        "supervisor",
        ("dispatch_eligibility", "dependency_evidence", "contract_identity"),
        "wait on dependencies or open a blocker; unrelated eligible work continues",
        "re-evaluate eligibility; no duplicate dispatch",
        "operation.dispatched",
    ),
    TransitionRule(
        "EXECUTING", "QA_REVIEW",
        ("work order completed with artifact hashes",),
        "supervisor",
        ("artifact_hashes", "work_order_revision", "contract_revision"),
        "open a worker blocker with the failed artifact evidence",
        "bounded worker recovery; completed siblings preserved",
        "qa.required",
    ),
    TransitionRule(
        "QA_REVIEW", "INTEGRATION_REVIEW",
        ("all mandatory work orders have fresh QA PASS verdicts",),
        "qa_role",
        ("qa_verdict", "subject_hash", "test_evidence"),
        "route a NEEDS_CHANGES verdict back to the owning worker",
        "re-run QA only after the affected revision changes",
        "qa.passed",
    ),
    TransitionRule(
        "INTEGRATION_REVIEW", "PRODUCT_READY",
        ("integration gate passed", "combined artifact set verified", "evidence current"),
        "architecture_role",
        ("integration_evidence", "artifact_revisions", "test_execution_evidence"),
        "route integration blockers to the owning workers; release remains blocked",
        "re-run integration after the changed artifacts",
        "integration.passed",
    ),
    TransitionRule(
        "PRODUCT_READY", "GITOPS",
        ("release readiness passed", "release documentation present", "QA and integration evidence fresh"),
        "supervisor",
        ("release_readiness", "documentation_hashes", "qa_and_integration_hashes"),
        "remain PRODUCT_READY with explicit release blockers",
        "remediate the missing evidence; never relax the gate",
        "release.requested",
    ),
    TransitionRule(
        "GITOPS", "COMPLETE",
        ("GitOps release turn completed", "final acceptance invariant passed", "release readiness still fresh"),
        "gitops_role + architecture_final_review",
        ("release_commit", "final_verdict", "release_readiness", "artifact_hashes"),
        "WAITING_FOR_HUMAN with release evidence attached",
        "authorized human resume only; terminal state cannot be revived implicitly",
        "run.complete",
    ),
)


def lifecycle_transition_matrix() -> list[dict[str, Any]]:
    """Serializable transition/evidence matrix for reports and audit."""
    return [rule.to_dict() for rule in LIFECYCLE_TRANSITIONS]


# ─────────────────────────────────────────────────────────────────────────────
# Decisions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeliveryCheck:
    name: str
    passed: bool
    status: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DeliveryDecision:
    gate: str
    passed: bool
    checks: tuple[DeliveryCheck, ...] = ()
    issues: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def failed_checks(self) -> tuple[DeliveryCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "issues": list(self.issues),
            "evidence": self.evidence,
            "timestamp": self.timestamp,
        }


class DeliveryIntegrityError(RuntimeError):
    """Raised by :func:`assert_release_ready` when delivery evidence is incomplete."""

    def __init__(self, decision: DeliveryDecision):
        self.decision = decision
        super().__init__(
            "RELEASE_NOT_READY: " + "; ".join(decision.issues)
            if decision.issues
            else "RELEASE_NOT_READY"
        )


def _canonical_evidence(value: Any) -> Any:
    """Strip volatile timestamps so decision fingerprints are stable."""
    if isinstance(value, Mapping):
        return {
            key: _canonical_evidence(item)
            for key, item in sorted(value.items())
            if key not in ("timestamp", "evaluated_at", "evidence_sha256", "evaluated")
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_evidence(item) for item in value]
    return value


def _finish(gate: str, checks: list[DeliveryCheck], evidence: dict[str, Any] | None = None) -> DeliveryDecision:
    failed = [c for c in checks if not c.passed]
    return DeliveryDecision(
        gate=gate,
        passed=not failed,
        checks=tuple(checks),
        issues=tuple(c.message for c in failed),
        evidence=evidence or {},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workspace loading
# ─────────────────────────────────────────────────────────────────────────────

def _norm(path: str | None) -> str:
    value = str(path or "").replace("\\", "/").strip().lstrip("/")
    while value.startswith("./"):
        value = value[2:]
    return value


def work_order_paths(workspace: Path | str) -> list[Path]:
    ws = Path(workspace)
    paths: list[Path] = []
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED"):
        directory = ws / ".sync" / "work-orders" / bucket
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.yaml")))
    return paths


def load_work_orders(workspace: Path | str) -> dict[str, dict[str, Any]]:
    """Load work orders by authoritative ID; later buckets never mask ACTIVE."""
    ws = Path(workspace)
    orders: dict[str, dict[str, Any]] = {}
    for bucket in ("COMPLETED", "BLOCKED", "ACTIVE"):
        directory = ws / ".sync" / "work-orders" / bucket
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            wo_id = str(data.get("id") or path.stem).strip()
            if not wo_id:
                continue
            data["_path"] = str(path)
            data["_bucket"] = bucket
            orders[wo_id] = data
    return orders


def duplicate_work_order_ids(workspace: Path | str) -> dict[str, list[str]]:
    """Work-order IDs declared in more than one bucket/file (planning duplicates)."""
    ws = Path(workspace)
    seen: dict[str, list[str]] = {}
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED"):
        directory = ws / ".sync" / "work-orders" / bucket
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            wo_id = str((data or {}).get("id") or path.stem).strip()
            seen.setdefault(wo_id, []).append(f"{bucket}/{path.name}")
    return {wo_id: files for wo_id, files in seen.items() if len(files) > 1}


def _declared_paths(wo: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    entries: list[Any] = [wo.get("deliverable")]
    extra = wo.get("deliverables")
    if isinstance(extra, list):
        entries.extend(extra)
    for entry in entries:
        raw = entry.get("path") if isinstance(entry, dict) else entry
        rel = _norm(raw if isinstance(raw, str) else None)
        if rel and rel not in paths:
            paths.append(rel)
    return paths


def _criteria(wo: Mapping[str, Any]) -> list[str]:
    raw = wo.get("acceptance_criteria")
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _requirements_refs(wo: Mapping[str, Any]) -> list[str]:
    raw = wo.get("requirement_refs")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _assignees(wo: Mapping[str, Any]) -> list[str]:
    raw = wo.get("assigned_agents") or wo.get("agent_id") or wo.get("assigned_agent")
    if isinstance(raw, str):
        return [raw.lower().strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).lower().strip() for item in raw if str(item).strip()]
    return []


def _dependencies(wo: Mapping[str, Any]) -> list[str]:
    raw = wo.get("dependencies")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _is_doc_only(wo: Mapping[str, Any]) -> bool:
    paths = _declared_paths(wo)
    if not paths:
        return True
    for rel in paths:
        suffix = Path(rel).suffix.lower()
        if suffix not in DOC_EXTENSIONS:
            return False
    return True


def _is_qa_work_order(wo: Mapping[str, Any]) -> bool:
    role = str(wo.get("role") or "").lower().strip()
    title = str(wo.get("title") or "").lower()
    wo_type = str(wo.get("type") or "").upper()
    return (
        role in ("qa", "q/a", "quality")
        or wo_type in ("QA", "TEST", "VERIFICATION")
        or "gemma" in _assignees(wo)
        or "verification suite" in title
    )


def _is_integration_work_order(wo: Mapping[str, Any]) -> bool:
    blob = f"{wo.get('title', '')} {wo.get('type', '')} {wo.get('role', '')}".lower()
    return any(token in blob for token in ("integration", "integrate", "gitops", "release"))


def plan_criteria(workspace: Path | str) -> list[str]:
    """Acceptance criteria declared by the approved plan.

    Reads explicit bullets under an acceptance-criteria heading (PLAN.md), and
    falls back to the requirement ledger when present. Display headings alone are
    never treated as criteria.
    """
    ws = Path(workspace)
    criteria: list[str] = []

    plan = ws / "PLAN.md"
    if plan.is_file():
        try:
            lines = plan.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        in_section = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                in_section = "acceptance criteria" in stripped.lower() or "requirements" in stripped.lower()
                continue
            if in_section and stripped.startswith(("-", "*")):
                text = stripped.lstrip("-*").strip()
                if text and not text.startswith("[") and text not in criteria:
                    criteria.append(text)

    if not criteria:
        ledger = ws / ".sync" / "requirements.yaml"
        if ledger.is_file():
            try:
                data = yaml.safe_load(ledger.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
            for req in (data.get("requirements") or []) if isinstance(data, dict) else []:
                if not isinstance(req, dict):
                    continue
                for item in req.get("acceptance_criteria") or []:
                    text = str(item).strip()
                    if text and text not in criteria:
                        criteria.append(text)
    return criteria


# ─────────────────────────────────────────────────────────────────────────────
# Workstream B — planning completeness
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def evaluate_planning_completeness(
    workspace: Path | str,
    *,
    require_executable: bool = True,
    required_criteria: Iterable[str] | None = None,
) -> DeliveryDecision:
    """Fail closed unless the plan is an executable, owned, verifiable breakdown."""
    ws = Path(workspace)
    orders = load_work_orders(ws)
    criteria = list(required_criteria) if required_criteria is not None else plan_criteria(ws)
    checks: list[DeliveryCheck] = []

    checks.append(DeliveryCheck(
        "plan_present",
        (ws / "PLAN.md").is_file(),
        PASSED if (ws / "PLAN.md").is_file() else FAILED,
        "Approved PLAN.md is present" if (ws / "PLAN.md").is_file() else "No PLAN.md exists in the workspace",
        {"plan": str(ws / "PLAN.md")},
    ))

    checks.append(DeliveryCheck(
        "work_orders_present",
        bool(orders),
        PASSED if orders else FAILED,
        f"{len(orders)} work order(s) published" if orders else "No work orders have been published",
        {"work_order_ids": sorted(orders)},
    ))

    checks.append(DeliveryCheck(
        "acceptance_criteria_declared",
        bool(criteria),
        PASSED if criteria else FAILED,
        f"{len(criteria)} acceptance criterion/criteria declared" if criteria else "The plan declares no acceptance criteria",
        {"criteria": criteria},
    ))

    duplicates = duplicate_work_order_ids(ws)
    checks.append(DeliveryCheck(
        "no_duplicate_work_orders",
        not duplicates,
        PASSED if not duplicates else FAILED,
        "No duplicate work-order identities" if not duplicates else f"Duplicate work-order IDs: {sorted(duplicates)}",
        {"duplicates": duplicates},
    ))

    # Ownership and deliverables.


    missing_deliverables = [
        wo_id for wo_id, wo in orders.items()
        if not _declared_paths(wo) and str(wo.get("type", "")).upper() not in ("QA", "GITOPS", "RELEASE")
    ]
    checks.append(DeliveryCheck(
        "meaningful_deliverables",
        not missing_deliverables,
        PASSED if not missing_deliverables else FAILED,
        "Every implementation work order declares a deliverable path"
        if not missing_deliverables
        else f"Work orders without a deliverable: {missing_deliverables}",
        {"missing_deliverables": missing_deliverables},
    ))

    # Acceptance-criteria mapping: every plan criterion is owned by a work order.
    normalized_criteria = {_normalize_text(c): c for c in criteria}
    wo_text = {
        wo_id: " ".join(_criteria(wo) + [str(wo.get("description", ""))])
        for wo_id, wo in orders.items()
    }
    unmapped = [
        text for norm, text in normalized_criteria.items()
        if not any(norm in _normalize_text(blob) for blob in wo_text.values())
    ]
    checks.append(DeliveryCheck(
        "criteria_implementation_ownership",
        bool(criteria) and not unmapped,
        PASSED if criteria and not unmapped else FAILED,
        "Every acceptance criterion maps to an implementing work order"
        if criteria and not unmapped
        else f"Acceptance criteria with no implementation owner: {unmapped}",
        {"unmapped_criteria": unmapped},
    ))

    criteria_ownerless = [
        wo_id for wo_id, wo in orders.items()
        if str(wo.get("type", "")).upper() not in ("GITOPS", "RELEASE")
        and not _criteria(wo)
        and not _requirements_refs(wo)
    ]
    checks.append(DeliveryCheck(
        "work_orders_have_criteria",
        not criteria_ownerless,
        PASSED if not criteria_ownerless else FAILED,
        "Every work order maps to acceptance criteria"
        if not criteria_ownerless
        else f"Work orders with no acceptance criteria: {criteria_ownerless}",
        {"ownerless_criteria_work_orders": criteria_ownerless},
    ))

    # Dependencies: every referenced ID exists and the graph is acyclic.
    missing_deps: dict[str, list[str]] = {}
    for wo_id, wo in orders.items():
        absent = [dep for dep in _dependencies(wo) if dep not in orders and dep not in (wo_id,)]
        if absent:
            missing_deps[wo_id] = absent
    checks.append(DeliveryCheck(
        "dependencies_resolvable",
        not missing_deps,
        PASSED if not missing_deps else FAILED,
        "All declared dependencies resolve to published work orders"
        if not missing_deps
        else f"Unresolved dependencies: {missing_deps}",
        {"missing_dependencies": missing_deps},
    ))

    cycle = _dependency_cycle(orders)
    checks.append(DeliveryCheck(
        "dependency_graph_acyclic",
        cycle is None,
        PASSED if cycle is None else FAILED,
        "Dependency graph is acyclic" if cycle is None else f"Dependency cycle detected: {cycle}",
        {"cycle": cycle},
    ))

    # Executable delivery responsibilities.
    executable_orders = [wo for wo in orders.values() if not _is_doc_only(wo)]
    if require_executable:
        doc_only = bool(orders) and not executable_orders
        checks.append(DeliveryCheck(
            "executable_plan",
            not doc_only,
            PASSED if not doc_only else FAILED,
            "Plan contains executable deliverables"
            if not doc_only
            else "Plan contains only documentation deliverables; executable software is required",
            {"code_work_orders": [wo_id for wo_id, wo in orders.items() if not _is_doc_only(wo)]},
        ))
    else:
        checks.append(DeliveryCheck(
            "executable_plan", True, SKIPPED,
            "Executable delivery not required for this project type", {},
        ))

    qa_orders = [wo_id for wo_id, wo in orders.items() if _is_qa_work_order(wo)]
    test_plans = [wo_id for wo_id, wo in orders.items() if wo.get("test_plan")]
    checks.append(DeliveryCheck(
        "qa_responsibility",
        (not require_executable) or bool(qa_orders or test_plans),
        PASSED if (not require_executable or qa_orders or test_plans) else FAILED,
        "QA/test responsibility is assigned" if (not require_executable or qa_orders or test_plans)
        else "No QA work order or test plan is declared",
        {"qa_work_orders": qa_orders, "test_plan_work_orders": test_plans},
    ))

    multi_component = len(executable_orders) > 1
    integration_orders = [wo_id for wo_id, wo in orders.items() if _is_integration_work_order(wo)]
    integration_required = require_executable and multi_component
    checks.append(DeliveryCheck(
        "integration_responsibility",
        (not integration_required) or bool(integration_orders),
        PASSED if (not integration_required or integration_orders) else FAILED,
        "Integration/release ownership is assigned" if (not integration_required or integration_orders)
        else "Multi-component delivery has no integration or release work order",
        {"integration_work_orders": integration_orders, "required": integration_required},
    ))

    release_required = require_executable
    release_present = bool(integration_orders) or any(
        (ws / name).is_file() for name in ("VERSION.md", "CHANGELOG.md")
    )
    checks.append(DeliveryCheck(
        "release_responsibility",
        (not release_required) or release_present,
        PASSED if (not release_required or release_present) else FAILED,
        "Release responsibility is assigned" if (not release_required or release_present)
        else "No release/GitOps responsibility exists for an executable product",
        {"release_required": release_required, "release_work_orders": integration_orders},
    ))

    # Unjustified overlap: two work orders owning the same deliverable path.
    owners: dict[str, list[str]] = {}
    for wo_id, wo in orders.items():
        for rel in _declared_paths(wo):
            owners.setdefault(rel, []).append(wo_id)
    overlaps = {rel: ids for rel, ids in owners.items() if len(ids) > 1}
    checks.append(DeliveryCheck(
        "no_unjustified_overlap",
        not overlaps,
        PASSED if not overlaps else FAILED,
        "No two work orders declare the same deliverable" if not overlaps
        else f"Overlapping deliverable ownership: {overlaps}",
        {"overlaps": overlaps},
    ))

    placeholder_deliverables = [
        wo_id for wo_id, wo in orders.items()
        if any(rel.lower() in ("todo", "tbd", "placeholder") or rel.endswith("/") for rel in _declared_paths(wo))
    ]
    checks.append(DeliveryCheck(
        "no_placeholder_deliverables",
        not placeholder_deliverables,
        PASSED if not placeholder_deliverables else FAILED,
        "No placeholder deliverable paths" if not placeholder_deliverables
        else f"Placeholder deliverable paths: {placeholder_deliverables}",
        {"placeholder_work_orders": placeholder_deliverables},
    ))

    evidence = {
        "work_order_ids": sorted(orders),
        "criteria": criteria,
        "executable_work_orders": [wo_id for wo_id, wo in orders.items() if not _is_doc_only(wo)],
        "qa_work_orders": qa_orders,
        "integration_work_orders": integration_orders,
    }
    return _finish(PLANNING_GATE, checks, evidence)


def _dependency_cycle(orders: Mapping[str, Mapping[str, Any]]) -> list[str] | None:
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for dep in _dependencies(orders.get(node, {})):
            if dep not in orders:
                continue
            if color.get(dep) == 1:
                idx = stack.index(dep) if dep in stack else 0
                return stack[idx:] + [dep]
            if color.get(dep, 0) == 0:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for wo_id in sorted(orders):
        if color.get(wo_id, 0) == 0:
            found = visit(wo_id)
            if found:
                return found
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Workstream C — artifact integrity
# ─────────────────────────────────────────────────────────────────────────────

def sha256_file(path: Path | str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _looks_like_placeholder(text: str, rel_path: str) -> str | None:
    if not text.strip():
        return "file is empty"
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(text):
            return f"placeholder content matched {pattern.pattern!r}"
    if Path(rel_path).suffix.lower() in CODE_EXTENSIONS:
        meaningful = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        if not meaningful:
            return "code file contains no executable statements"
    return None


def _contained(ws: Path, rel_path: str) -> Path | None:
    try:
        candidate = (ws / rel_path).resolve()
        candidate.relative_to(ws.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def evaluate_artifact_integrity(
    workspace: Path | str,
    work_order_ids: Iterable[str] | None = None,
    *,
    allow_placeholder: bool = False,
) -> DeliveryDecision:
    """Verify each declared deliverable exists, is meaningful, and matches evidence."""
    ws = Path(workspace)
    orders = load_work_orders(ws)
    target_ids = list(work_order_ids) if work_order_ids is not None else sorted(orders)
    checks: list[DeliveryCheck] = []
    artifact_evidence: dict[str, Any] = {}

    for wo_id in target_ids:
        wo = orders.get(wo_id)
        if wo is None:
            checks.append(DeliveryCheck(
                f"artifact:{wo_id}", False, FAILED,
                f"Work order {wo_id} is not published", {},
            ))
            continue
        declared = _declared_paths(wo)
        if not declared:
            status = SKIPPED if str(wo.get("type", "")).upper() in ("GITOPS", "RELEASE") else FAILED
            checks.append(DeliveryCheck(
                f"artifact:{wo_id}", status != FAILED, status,
                f"Work order {wo_id} declares no artifact" if status == FAILED
                else f"Work order {wo_id} is release bookkeeping with no artifact",
                {"declared": []},
            ))
            continue

        completion = wo.get("completion") if isinstance(wo.get("completion"), dict) else {}
        recorded_hashes = completion.get("deliverable_hashes") or {}
        for rel in declared:
            resolved = _contained(ws, rel)
            if resolved is None or not resolved.is_file():
                checks.append(DeliveryCheck(
                    f"artifact:{wo_id}:{rel}", False, FAILED,
                    f"Declared artifact '{rel}' does not exist inside the workspace",
                    {"path": rel},
                ))
                continue
            digest = sha256_file(resolved)
            try:
                text = resolved.read_text(encoding="utf-8")
            except Exception:
                text = ""
            placeholder = None if allow_placeholder else _looks_like_placeholder(text, rel)
            expected = str(recorded_hashes.get(rel) or "")
            stale = bool(expected) and expected != digest
            artifact_evidence[rel] = {"sha256": digest, "bytes": resolved.stat().st_size}
            if placeholder:
                checks.append(DeliveryCheck(
                    f"artifact:{wo_id}:{rel}", False, FAILED,
                    f"Artifact '{rel}' is a no-op/placeholder: {placeholder}",
                    {"path": rel, "sha256": digest},
                ))
            elif stale:
                checks.append(DeliveryCheck(
                    f"artifact:{wo_id}:{rel}", False, FAILED,
                    f"Artifact '{rel}' no longer matches its completion-record hash (stale evidence)",
                    {"path": rel, "expected": expected, "observed": digest},
                ))
            else:
                checks.append(DeliveryCheck(
                    f"artifact:{wo_id}:{rel}", True, PASSED,
                    f"Artifact '{rel}' exists with meaningful, hash-bound content",
                    {"path": rel, "sha256": digest},
                ))

    return _finish(ARTIFACT_GATE, checks, {"artifacts": artifact_evidence})


# ─────────────────────────────────────────────────────────────────────────────
# Workstream D — QA evidence
# ─────────────────────────────────────────────────────────────────────────────

def qa_status(workspace: Path | str, wo_id: str) -> tuple[str, str]:
    """Return (status, reason) for one work order's QA evidence.

    Statuses: PASSED, FAILED, NOT_TESTED, RUNNING, INCONCLUSIVE, BLOCKED.
    """
    ws = Path(workspace)
    verdict = review_gate.load_qa_verdict(ws, wo_id)
    if not isinstance(verdict, dict):
        running = (ws / ".sync" / "runtime" / "qa_inflight.json")
        return NOT_TESTED, "no QA verdict has been persisted"
    raw = str(verdict.get("verdict", "")).upper()
    if raw == review_gate.QA_PASS:
        ok, reason = review_gate.validate_qa_verdict(verdict, ws, wo_id)
        return (QA_PASSED if ok else QA_FAILED), reason if not ok else "QA verdict accepted"
    if raw == review_gate.QA_FAIL:
        return QA_FAILED, "QA verdict is FAIL"
    if raw in ("RUNNING", "IN_PROGRESS", "PENDING"):
        return QA_RUNNING, "QA verdict is still in progress"
    if raw in ("BLOCKED", "WAITING_FOR_HUMAN"):
        return QA_BLOCKED, "QA is blocked"
    return QA_INCONCLUSIVE, f"unrecognized QA verdict {raw!r}"


def evaluate_qa_evidence(
    workspace: Path | str,
    work_order_ids: Iterable[str] | None = None,
) -> DeliveryDecision:
    """Evaluate revision-bound QA evidence for every work order that requires it."""
    ws = Path(workspace)
    orders = load_work_orders(ws)
    target_ids = list(work_order_ids) if work_order_ids is not None else sorted(orders)
    checks: list[DeliveryCheck] = []
    evidence: dict[str, Any] = {}

    for wo_id in target_ids:
        wo = orders.get(wo_id)
        if wo is None:
            checks.append(DeliveryCheck(f"qa:{wo_id}", False, FAILED, f"Work order {wo_id} is not published", {}))
            continue
        if not review_gate.requires_qa(ws, wo_id, wo):
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", True, SKIPPED,
                f"QA is not mandatory for {wo_id}", {"work_order_id": wo_id},
            ))
            continue
        status, reason = qa_status(ws, wo_id)
        verdict = review_gate.load_qa_verdict(ws, wo_id) or {}
        evidence[wo_id] = {
            "status": status,
            "reason": reason,
            "subject_hash": verdict.get("subject_hash"),
            "current_subject_hash": review_gate.subject_hash(ws, wo_id, wo),
        }
        if status == QA_PASSED:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", True, PASSED, f"QA PASS for {wo_id} is revision-bound and accepted",
                evidence[wo_id],
            ))
        elif status == QA_RUNNING:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", False, "running", f"QA is still running for {wo_id}", evidence[wo_id],
            ))
        elif status == QA_BLOCKED:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", False, "blocked", f"QA is blocked for {wo_id}: {reason}", evidence[wo_id],
            ))
        elif status == QA_INCONCLUSIVE:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", False, "inconclusive", f"QA is inconclusive for {wo_id}: {reason}", evidence[wo_id],
            ))
        elif status == QA_FAILED:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", False, FAILED, f"QA failed for {wo_id}: {reason}", evidence[wo_id],
            ))
        else:
            checks.append(DeliveryCheck(
                f"qa:{wo_id}", False, "not_tested", f"QA was not executed for {wo_id}: {reason}", evidence[wo_id],
            ))

    return _finish(QA_GATE, checks, {"qa": evidence})


# ─────────────────────────────────────────────────────────────────────────────
# Workstream E — integration
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_integration(
    workspace: Path | str,
    state: Any = None,
    integration_op: dict[str, Any] | None = None,
    extra_telemetry: list[dict[str, Any]] | None = None,
) -> DeliveryDecision:
    """Evaluate the combined product using the existing integration gate."""
    ws = Path(workspace)
    checks: list[DeliveryCheck] = []
    gate_decision = IntegrationGate(ws).evaluate(state, integration_op, extra_telemetry)

    for check in gate_decision.checks:
        checks.append(DeliveryCheck(
            f"integration:{check.name}",
            check.passed,
            check.status,
            check.message,
            dict(check.evidence),
        ))

    orders = load_work_orders(ws)
    missing: dict[str, list[str]] = {}
    for wo_id, wo in orders.items():
        absent: list[str] = []
        for rel in _declared_paths(wo):
            resolved = _contained(ws, rel)
            if resolved is None or not resolved.is_file():
                absent.append(rel)
        if absent:
            missing[wo_id] = absent
    checks.append(DeliveryCheck(
        "integration:combined_artifacts_present",
        not missing,
        PASSED if not missing else FAILED,
        "All declared work-order artifacts exist in the combined product"
        if not missing
        else f"Combined product is missing artifacts: {missing}",
        {"missing": missing},
    ))

    # Declared dependency manifests must exist when a package-style project uses them.
    manifest_issues: list[str] = []
    for wo in orders.values():
        for rel in _declared_paths(wo):
            if rel.endswith("package.json") or rel.endswith("requirements.txt") or rel.endswith("pyproject.toml"):
                if not (ws / rel).is_file():
                    manifest_issues.append(rel)
    checks.append(DeliveryCheck(
        "integration:dependency_manifests",
        not manifest_issues,
        PASSED if not manifest_issues else FAILED,
        "Declared dependency manifests are present"
        if not manifest_issues
        else f"Missing dependency manifests: {sorted(set(manifest_issues))}",
        {"missing_manifests": sorted(set(manifest_issues))},
    ))

    interface_mismatches = _interface_mismatches(ws)
    checks.append(DeliveryCheck(
        "integration:interface_contracts",
        not interface_mismatches,
        PASSED if not interface_mismatches else FAILED,
        "Frontend API calls resolve to declared backend routes"
        if not interface_mismatches
        else f"Frontend calls have no matching backend route: {interface_mismatches}",
        {"unmatched_frontend_calls": interface_mismatches},
    ))

    return _finish(INTEGRATION_GATE, checks, dict(gate_decision.evidence))


_FETCH_RE = re.compile(
    r"""(?:fetch|axios\.(?:get|post|put|delete|patch))\s*\(\s*['\"`]([^'\"`]+)['\"`]"""
)
_ROUTE_RE = re.compile(
    r"""(?:@\w+\.(?:get|post|put|delete|patch)|(?:\.|^)(?:get|post|put|delete|patch))\s*\(\s*['\"]([^'\"]+)['\"]""",
    re.MULTILINE,
)
_SKIP_DIRS = {".git", ".sync", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _interface_mismatches(workspace: Path) -> list[str]:
    """Static frontend-call/backend-route consistency for the combined product."""
    frontend: set[str] = set()
    backend: set[str] = set()
    for path in workspace.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".html", ".py"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontend.update(_FETCH_RE.findall(text))
        backend.update(_ROUTE_RE.findall(text))
    if not frontend or not backend:
        return []

    def _norm(value: str) -> str:
        return "/" + value.split("?")[0].strip().strip("/")

    routes = {_norm(route) for route in backend}
    misses: list[str] = []
    for call in frontend:
        if not call.startswith("/"):
            continue
        target = _norm(call)
        if not _route_matches(target, routes):
            misses.append(target)
    return sorted(set(misses))


def _route_matches(target: str, routes: set[str]) -> bool:
    target_parts = target.split("/")
    for route in routes:
        if route == target:
            return True
        route_parts = route.split("/")
        if len(route_parts) == len(target_parts) and all(
            r == t or "<" in r or "{" in r for r, t in zip(route_parts, target_parts)
        ):
            return True
        if "<" not in route and "{" not in route and target.startswith(route.rstrip("/") + "/"):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Workstream F — release readiness
# ─────────────────────────────────────────────────────────────────────────────

def _open_blockers(state: Any) -> list[str]:
    if state is None:
        return []
    blockers: list[str] = []
    for blocker_id, record in (getattr(state, "blocker_registry", {}) or {}).items():
        if isinstance(record, Mapping) and str(record.get("status", "")).upper() not in (
            "RESOLVED", "CLOSED", "CANCELLED",
        ):
            blockers.append(str(blocker_id))
    for wo_id in getattr(state, "blocked_wo_ids", []) or []:
        if str(wo_id) not in blockers:
            blockers.append(str(wo_id))
    return sorted(set(blockers))


def _stale_evidence(ws: Path, orders: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    stale: dict[str, str] = {}
    for wo_id, wo in orders.items():
        marker = wo.get("evidence_stale")
        if isinstance(marker, dict) and marker:
            stale[wo_id] = str(marker.get("reason") or marker.get("fields") or "evidence marked stale")
    for wo_id in orders:
        verdict = review_gate.load_qa_verdict(ws, wo_id)
        if not isinstance(verdict, dict):
            continue
        expected = str(verdict.get("subject_hash") or "")
        current = review_gate.subject_hash(ws, wo_id, orders[wo_id])
        if expected and expected != current:
            stale[wo_id] = "QA evidence is bound to a superseded artifact revision"
    return stale


def evaluate_release_readiness(
    workspace: Path | str,
    state: Any = None,
    *,
    require_executable: bool = True,
    require_documentation: bool = True,
    integration_op: dict[str, Any] | None = None,
) -> DeliveryDecision:
    """Fail-closed release evaluation across planning, artifacts, QA and integration."""
    ws = Path(workspace)
    orders = load_work_orders(ws)
    completed = sorted(
        wo_id for wo_id, wo in orders.items()
        if str(wo.get("status", "")).upper() == "COMPLETED"
    ) or sorted(orders)
    checks: list[DeliveryCheck] = []

    planning = evaluate_planning_completeness(ws, require_executable=require_executable)
    artifacts = evaluate_artifact_integrity(ws, completed)
    qa = evaluate_qa_evidence(ws, completed)
    integration = evaluate_integration(ws, state=state, integration_op=integration_op)
    for decision in (planning, artifacts, qa, integration):
        checks.append(DeliveryCheck(
            f"{decision.gate}_gate", decision.passed,
            PASSED if decision.passed else FAILED,
            f"{decision.gate} gate passed" if decision.passed
            else f"{decision.gate} gate failed: " + "; ".join(decision.issues[:3]),
            decision.to_dict(),
        ))

    stale = _stale_evidence(ws, orders)
    checks.append(DeliveryCheck(
        "release:stale_evidence",
        not stale,
        PASSED if not stale else FAILED,
        "No stale completion/QA evidence" if not stale
        else f"Stale evidence blocks release: {stale}",
        {"stale": stale},
    ))

    blockers = _open_blockers(state)
    checks.append(DeliveryCheck(
        "release:blockers",
        not blockers,
        PASSED if not blockers else FAILED,
        "No unresolved blockers" if not blockers else f"Open blockers: {blockers}",
        {"blockers": blockers},
    ))

    # Governance: every completed implementation order has an authoritative contract.
    missing_contracts = [
        wo_id for wo_id, wo in orders.items()
        if str(wo.get("type", "")).upper() not in ("QA", "GITOPS", "RELEASE")
        and not (ws / ".sync" / "contracts" / f"{wo_id}.yaml").is_file()
    ]
    checks.append(DeliveryCheck(
        "release:contract_identity",
        not missing_contracts,
        PASSED if not missing_contracts else FAILED,
        "Every implementation work order has a contract" if not missing_contracts
        else f"Work orders without contracts: {missing_contracts}",
        {"missing_contracts": missing_contracts},
    ))

    if require_documentation:
        docs = [name for name in ("README.md", "VERSION.md", "CHANGELOG.md") if (ws / name).is_file()]
        startup = any(
            (ws / name).is_file() for name in ("README.md", "STARTUP.md", "docs/README.md")
        )
        checks.append(DeliveryCheck(
            "release:documentation",
            bool(docs) and startup,
            PASSED if docs and startup else FAILED,
            f"Release documentation present: {docs}" if docs and startup
            else "Release documentation/startup instructions are missing",
            {"documents": docs, "startup_instructions": startup},
        ))
    else:
        checks.append(DeliveryCheck(
            "release:documentation", True, SKIPPED,
            "Release documentation not required for this project type", {},
        ))

    decision = _finish(RELEASE_GATE, checks, {
        "planning": planning.evidence,
        "artifacts": artifacts.evidence,
        "qa": qa.evidence,
        "integration": integration.evidence,
        "work_order_ids": sorted(orders),
    })
    # Deterministic fingerprint so release evidence can be revision-bound.
    # Wall-clock timestamps never participate in the decision identity.
    canonical = _canonical_evidence(decision.to_dict())
    blob = json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    payload = dict(decision.evidence)
    payload["evidence_sha256"] = hashlib.sha256(blob).hexdigest()
    return DeliveryDecision(
        gate=decision.gate,
        passed=decision.passed,
        checks=decision.checks,
        issues=decision.issues,
        evidence=payload,
        timestamp=decision.timestamp,
    )


def assert_release_ready(
    workspace: Path | str,
    state: Any = None,
    **kwargs: Any,
) -> DeliveryDecision:
    """Raise :class:`DeliveryIntegrityError` unless release readiness passes."""
    decision = evaluate_release_readiness(workspace, state, **kwargs)
    if not decision.passed:
        raise DeliveryIntegrityError(decision)
    return decision


# ─────────────────────────────────────────────────────────────────────────────
# Workstream F (cont.) — structured blockers, evidence snapshots, view
# ─────────────────────────────────────────────────────────────────────────────

RELEASE_DECISION_SCHEMA = "sdp009.release_decision.v1"
RELEASE_DECISION_RELPATH = Path(".sync") / "reports" / "delivery" / "release_gate.json"

_PHASE_FOR_GATE = {
    PLANNING_GATE: "PLANNING",
    ARTIFACT_GATE: "EXECUTING",
    QA_GATE: "QA_REVIEW",
    INTEGRATION_GATE: "INTEGRATION_REVIEW",
    RELEASE_GATE: "RELEASE_READINESS",
}

_REMEDIATION = {
    PLANNING_GATE: (
        "Correct the approved plan and work breakdown: map every acceptance "
        "criterion to an owned work order, publish the missing work orders, and "
        "repair dependency/ownership defects. Then re-evaluate."
    ),
    ARTIFACT_GATE: (
        "Have the owning worker regenerate the declared artifact inside its "
        "contract scope, or correct the work-order deliverable path. Then "
        "re-run QA and re-evaluate."
    ),
    QA_GATE: (
        "Dispatch the revision-bound QA review for the current artifact set and "
        "require a fresh PASS verdict. A worker's completion claim is not QA "
        "evidence. Then re-evaluate."
    ),
    INTEGRATION_GATE: (
        "Resolve the combined-product integration failure (missing artifacts, "
        "dependency manifests, interface mismatches), re-run integration against "
        "the current revisions, then re-evaluate."
    ),
    RELEASE_GATE: (
        "Resolve the reported release prerequisite (stale evidence, open "
        "blocker, missing contract, missing documentation) and re-evaluate. "
        "Do not override the gate."
    ),
}


def _blocker_code(check: DeliveryCheck) -> str:
    """Stable, human-readable blocker code derived from the failed requirement."""
    token = re.sub(r"[^A-Za-z0-9]+", "_", check.name).strip("_").upper()
    return f"DELIVERY_{token}"


def _evidence_state(check: DeliveryCheck) -> str:
    message = check.message.lower()
    if "stale" in message or "superseded" in message:
        return "STALE"
    if "contradict" in message or "mismatch" in message:
        return "CONTRADICTORY"
    if "missing" in message or "does not exist" in message or "no " in message:
        return "MISSING"
    if "unrecognized" in message or "unknown" in message or "inconclusive" in message:
        return "UNKNOWN"
    return "INVALID"


def _affected_entity(check: DeliveryCheck) -> str | None:
    evidence = check.evidence or {}
    for key in ("work_order_id", "work_order", "path", "contract"):
        value = evidence.get(key)
        if value:
            return str(value)
    name = check.name
    if name.startswith("artifact:"):
        parts = name.split(":")
        return parts[-1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else None)
    if name.startswith("qa:"):
        return name.split(":", 1)[1]
    for key in ("blockers", "missing", "stale", "duplicates", "overlaps", "unmapped_criteria"):
        value = evidence.get(key)
        if isinstance(value, (list, dict)) and value:
            return ", ".join(sorted(str(item) for item in (value.keys() if isinstance(value, dict) else value)))[:200]
    return None


def structured_blockers(decision: DeliveryDecision) -> list[dict[str, Any]]:
    """Convert a failed decision into deterministic, actionable blockers.

    Ordering is by lifecycle stage then dependency/check name, so equivalent
    evidence always produces the same blocker list and the same codes.
    """
    blockers: list[dict[str, Any]] = []
    for check in decision.failed_checks:
        gate = check.name.split(":", 1)[0] if ":" in check.name else RELEASE_GATE
        phase = _PHASE_FOR_GATE.get(gate, _PHASE_FOR_GATE[RELEASE_GATE])
        blockers.append({
            "blocker_id": _blocker_code(check),
            "code": _blocker_code(check),
            "phase": phase,
            "gate": gate,
            "requirement": check.name,
            "affected": _affected_entity(check),
            "severity": "BLOCKING",
            "evidence_state": _evidence_state(check),
            "message": check.message,
            "remediation": _REMEDIATION.get(gate, _REMEDIATION[RELEASE_GATE]),
            "reevaluation_required": True,
        })
    # Deterministic, stable ordering for equal inputs — phase then gate then
    # requirement name.  No position in the list may shift when the evidence set
    # changes.
    blockers.sort(key=lambda item: (item["phase"], item["gate"], item["requirement"]))
    return blockers


def decision_fingerprint(decision: DeliveryDecision) -> str | None:
    """The deterministic evidence identity bound to a release decision."""
    value = (decision.evidence or {}).get("evidence_sha256")
    return str(value) if value else None


def release_snapshot(
    decision: DeliveryDecision,
    *,
    run_id: str | None,
    phase: str | None,
    source: str,
) -> dict[str, Any]:
    """Build the persisted, auditable release-decision snapshot."""
    return {
        "schema": RELEASE_DECISION_SCHEMA,
        "run_id": run_id,
        "phase": phase,
        "source": source,
        "evaluated_at": decision.timestamp,
        "passed": decision.passed,
        "evidence_sha256": decision_fingerprint(decision),
        "issues": list(decision.issues),
        "checks": [check.to_dict() for check in decision.checks],
        "blockers": structured_blockers(decision),
        "evidence": decision.evidence,
    }


def persist_release_decision(
    workspace: Path | str,
    decision: DeliveryDecision,
    *,
    run_id: str | None = None,
    phase: str | None = None,
    source: str = "release_boundary",
) -> dict[str, Any]:
    """Persist the exact decision snapshot that authorized (or blocked) release."""
    from validators.kernel.io import durable_write_text

    snapshot = release_snapshot(decision, run_id=run_id, phase=phase, source=source)
    path = Path(workspace) / RELEASE_DECISION_RELPATH
    durable_write_text(path, json.dumps(snapshot, indent=2, sort_keys=True))
    return snapshot


def load_release_decision(workspace: Path | str | None) -> dict[str, Any] | None:
    """Load the last authoritative release-decision snapshot (read-only)."""
    if workspace is None:
        return None
    path = Path(workspace) / RELEASE_DECISION_RELPATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != RELEASE_DECISION_SCHEMA:
        return None
    return data


def decision_is_current(
    workspace: Path | str,
    state: Any,
    decision: DeliveryDecision | dict[str, Any] | None,
    **kwargs: Any,
) -> tuple[bool, str]:
    """Re-evaluate and prove the decision still matches the current workspace.

    Guards the release boundary against a time-of-check/time-of-use gap: any
    artifact, contract, verdict or plan mutation between evaluation and release
    changes the deterministic evidence fingerprint.
    """
    expected = decision_fingerprint(decision) if isinstance(decision, DeliveryDecision) else (
        str((decision or {}).get("evidence_sha256") or "") or None
    )
    if not expected:
        return False, "decision carries no evidence fingerprint"
    fresh = evaluate_release_readiness(workspace, state, **kwargs)
    observed = decision_fingerprint(fresh)
    if observed != expected:
        return False, (
            "release evidence changed between evaluation and release "
            f"(expected {expected[:16]}…, observed {(observed or 'none')[:16]}…)"
        )
    return True, "release evidence is unchanged and still current"


def format_delivery_integrity(
    snapshot: Mapping[str, Any] | None,
    *,
    max_blockers: int = 12,
) -> str:
    """Read-only operator view of the persisted authoritative decision.

    Never recalculates readiness: it renders the supervisor-persisted snapshot so
    the TUI cannot become a second source of truth.
    """
    if not isinstance(snapshot, Mapping):
        return (
            "DELIVERY INTEGRITY — NOT EVALUATED\n"
            "No authoritative release-decision snapshot exists for this workspace.\n"
            "The supervisor persists one when the release boundary is evaluated "
            "(GITOPS completion). Until then, delivery readiness is unknown — not passed."
        )

    passed = bool(snapshot.get("passed"))
    lines = [
        f"DELIVERY INTEGRITY — {'READY' if passed else 'BLOCKED'}",
        f"Phase: {snapshot.get('phase') or 'UNKNOWN'}"
        f"  |  Run: {snapshot.get('run_id') or 'UNKNOWN'}"
        f"  |  Source: {snapshot.get('source') or 'UNKNOWN'}",
        f"Evaluated: {snapshot.get('evaluated_at') or 'UNKNOWN'}",
        f"Evidence: sha256:{snapshot.get('evidence_sha256') or 'UNAVAILABLE'}",
        "",
        "GATES",
    ]

    gate_checks = [
        check for check in (snapshot.get("checks") or [])
        if isinstance(check, Mapping) and not str(check.get("name", "")).startswith(
            ("artifact:", "qa:", "integration:")
        )
    ]
    for check in gate_checks:
        status = "PASS" if check.get("passed") else "FAIL"
        if str(check.get("status")) == "skipped":
            status = "SKIP"
        lines.append(f"  [{status}] {check.get('name')}: {check.get('message')}")

    blockers = list(snapshot.get("blockers") or [])
    lines.append("")
    lines.append(f"OPEN BLOCKERS ({len(blockers)})")
    if not blockers:
        lines.append("  none")
    for index, blocker in enumerate(blockers[:max_blockers], start=1):
        if not isinstance(blocker, Mapping):
            continue
        lines.append(
            f"  {index}. [{blocker.get('code')}] ({blocker.get('phase')}, "
            f"{blocker.get('evidence_state')}) {blocker.get('message')}"
        )
        if blocker.get("affected"):
            lines.append(f"     affected: {blocker.get('affected')}")
        lines.append(f"     remediation: {blocker.get('remediation')}")
    if len(blockers) > max_blockers:
        lines.append(f"  … {len(blockers) - max_blockers} more blocker(s); see .sync/reports/delivery/release_gate.json")

    lines.append("")
    lines.append(
        "Release decision: "
        + ("AUTHORIZED by this evidence snapshot" if passed else "DENIED — delivery must remain blocked")
        + ". This view is read-only and does not re-evaluate readiness."
    )
    return "\n".join(lines)
