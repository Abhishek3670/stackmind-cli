"""Authoring Readiness Gate — atomic publication validation for governed artifacts.

Pure deterministic validator invoked by the lifecycle supervisor between
AUTHORING and DISPATCHING.  It verifies that the set of Architect-authored
Work Orders and Contracts is complete, mutually consistent, and safe to
publish before any worker dispatch can begin (CONTRACT-01, HARNESS-01).

The gate is fail-closed: any issue prevents publication and routes the run
to ARCHITECT_REPAIR with structured evidence instead of dispatching a
partially authored artifact set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Sequence
import yaml

from validators.harness.authoring_gate import AuthoringGate

# Roles that may be assigned implementation work.  The architect role is
# excluded: implementation milestones must be dispatched to workers.
PERMITTED_WORKER_ROLES = ("codex", "gemini", "gemma", "local-llm")

_PLANNING_WO_IDS = {"WO-000"}
_WO_ID_PATTERN = re.compile(r"^WO-[0-9]{3,}$")


@dataclass(frozen=True)
class ReadinessIssue:
    """One structured readiness failure with a stable, machine-readable code."""

    code: str
    category: str
    message: str
    work_order_id: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "work_order_id": self.work_order_id,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class AuthoringReadinessResult:
    """Structured result returned by the readiness gate (never an exception)."""

    ready: bool
    plan_id: str
    artifact_paths: tuple[str, ...] = ()
    issues: tuple[ReadinessIssue, ...] = field(default_factory=tuple)

    def issue_codes(self) -> tuple[str, ...]:
        return tuple(sorted({issue.code for issue in self.issues}))


@dataclass
class _WorkOrderArtifact:
    wo_id: str
    path: str
    data: dict[str, Any]


@dataclass
class _ContractArtifact:
    path: str
    data: dict[str, Any]
    work_order: str | None
    agent_id: str | None


def _issue(issues: list[ReadinessIssue], code: str, category: str, message: str, **kw: Any) -> None:
    issues.append(ReadinessIssue(code, category, message, **kw))


def _title_tokens(value: Any) -> set[str]:
    stop = {"and", "the", "for", "of", "a", "an", "to", "in", "with", "on"}
    return {
        t for t in re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower()).split() if t not in stop
    }


def milestone_matches(milestone: str, wo_title: str) -> bool:
    """Deterministic lenient matcher: containment or >=60% token overlap."""
    m_tokens = _title_tokens(milestone)
    w_tokens = _title_tokens(wo_title)
    if not m_tokens or not w_tokens:
        return False
    m_text = " ".join(sorted(m_tokens))
    w_text = " ".join(sorted(w_tokens))
    if m_text == w_text or m_text in w_text or w_text in m_text:
        return True
    overlap = len(m_tokens & w_tokens) / max(len(m_tokens), len(w_tokens))
    return overlap >= 0.6


def load_expected_milestones(workspace: Path | str) -> list[str] | None:
    """Parse PLAN.md milestone titles; returns None when the plan is unparseable.

    The supervisor passes these to the readiness gate so that every expected
    implementation milestone must be covered by exactly one authored work order.
    """
    plan_file = Path(workspace) / "PLAN.md"
    if not plan_file.is_file():
        return None
    try:
        content = plan_file.read_text(encoding="utf-8")
    except Exception:
        return None
    titles: list[str] = []
    try:
        from validators.harness.plan import parse_plan

        parsed = parse_plan(content)
        titles = [m.title for m in parsed.milestones if m.title]
    except Exception:
        return None
    return titles or None


def _path_matches_rule(norm_path: str, pattern: str) -> bool:
    p_norm = pattern.replace("\\", "/")
    if p_norm.startswith("./"):
        p_norm = p_norm[2:]
    p_norm = p_norm.lstrip("/")
    base = p_norm.rstrip("/")
    return (
        norm_path == p_norm
        or fnmatch(norm_path, p_norm)
        or fnmatch(norm_path, base + "/*")
        or fnmatch(norm_path, base + "/**")
        or norm_path.startswith(base + "/")
    )


_CODE_EXTENSIONS = (".py", ".ts", ".js", ".go", ".rs", ".dart")
_DOC_EXTENSIONS = (".md", ".txt", ".json", ".yaml", ".yml")


def _is_test_artifact(path: str) -> bool:
    norm = str(path).replace("\\", "/").strip().lstrip("./")
    if not norm:
        return False
    parts = norm.lower().split("/")
    if parts[0] in ("tests", "test"):
        return True
    stem = Path(norm).stem.lower()
    return stem.startswith("test_") or stem.endswith("_test")


def _is_code_deliverable(deliv_type: str, deliv_path: str) -> bool:
    """Mirror the D024 gate's code classification for companion-test planning."""
    if deliv_type == "code" and not deliv_path.endswith(_DOC_EXTENSIONS):
        return True
    return deliv_path.endswith(_CODE_EXTENSIONS)


_TEST_PATH_PATTERN = re.compile(r"(?:\b|[\s`\"'(\[])(?:tests?/)?[A-Za-z0-9_\-/]*test_[A-Za-z0-9_\-]+\.py")


def _planned_test_paths(data: dict[str, Any]) -> list[str]:
    """Collect the test artifact paths a work order plans to produce."""
    planned: list[str] = []
    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict):
        p = str(deliverable.get("path") or "").replace("\\", "/").strip().lstrip("/")
        if p and _is_test_artifact(p):
            planned.append(p)
    estimate = data.get("implementation_estimate")
    if isinstance(estimate, dict):
        for f in estimate.get("expected_files") or []:
            norm = str(f).replace("\\", "/").strip().lstrip("/")
            if norm and _is_test_artifact(norm) and norm not in planned:
                planned.append(norm)
    text = f"{data.get('title', '')}\n{data.get('description', '')}"
    for match in _TEST_PATH_PATTERN.findall(text):
        norm = match.strip().strip("`\"'(),[]").replace("\\", "/").lstrip("./")
        if norm and _is_test_artifact(norm) and norm not in planned:
            planned.append(norm)
    return planned


def _covers_stem(test_path: str, stem: str) -> bool:
    name = Path(str(test_path).replace("\\", "/")).stem.lower()
    return name.startswith(f"test_{stem}") or name.endswith(f"{stem}_test") or f"_{stem}_" in f"_{name}_"


def _scope_allows(contract_data: dict[str, Any], rel_path: str) -> bool:
    scope = contract_data.get("scope")
    if not isinstance(scope, dict):
        return False
    norm = rel_path.replace("\\", "/").lstrip("/")
    for rule in scope.get("allow", []) or []:
        if isinstance(rule, dict):
            pattern = rule.get("module") or rule.get("path") or rule.get("target")
        elif isinstance(rule, str):
            pattern = rule
        else:
            pattern = None
        if pattern and _path_matches_rule(norm, str(pattern)):
            return True
    return False


def validate_authoring_readiness(
    workspace: Path | str,
    plan_id: str = "",
    expected_milestones: Sequence[str] | None = None,
    planning_wo_id: str = "WO-000",
) -> AuthoringReadinessResult:
    """Validate the authored artifact set for atomic publication.

    Returns a structured :class:`AuthoringReadinessResult`; never raises for
    validation findings.  Only unexpected I/O problems degrade gracefully.
    """
    ws = Path(workspace)
    issues: list[ReadinessIssue] = []
    artifact_paths: list[str] = []
    skip_ids = _PLANNING_WO_IDS | ({planning_wo_id} if planning_wo_id else set())

    gate = AuthoringGate(project_root=ws)

    # ── 1. Discover and parse artifacts ─────────────────────────────
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"

    wo_files: dict[str, tuple[str, dict[str, Any]]] = {}
    duplicate_ids: set[str] = set()
    if active_dir.is_dir():
        for f in sorted(active_dir.glob("*.yaml")):
            rel = f.relative_to(ws).as_posix()
            artifact_paths.append(rel)
            data = _load_yaml(f, issues, rel)
            if data is None:
                continue
            wo_id = str(data.get("id") or f.stem)
            if wo_id in skip_ids:
                continue
            if wo_id in wo_files:
                duplicate_ids.add(wo_id)
                _issue(issues, "DUPLICATE_WORK_ORDER", "coverage", f"Work order '{wo_id}' is defined by more than one artifact " f"('{wo_files[wo_id][0]}' and '{rel}')", work_order_id=wo_id, artifact_path=rel)
                continue
            wo_files[wo_id] = (rel, data)
    else:
        _issue(issues, "NO_WORK_ORDERS", "coverage", f"Active work order directory '{active_dir.name}' does not exist; nothing authored", artifact_path=".sync/work-orders/ACTIVE")

    contracts: dict[str, _ContractArtifact] = {}
    if contracts_dir.is_dir():
        for f in sorted(contracts_dir.glob("*.yaml")):
            rel = f.relative_to(ws).as_posix()
            artifact_paths.append(rel)
            data = _load_yaml(f, issues, rel)
            if data is None:
                continue
            wo_ref = str(data.get("work_order") or "")
            if wo_ref in skip_ids:
                continue
            contracts[wo_ref or f.stem] = _ContractArtifact(
                path=rel, data=data, work_order=wo_ref or None, agent_id=data.get("agent_id")
            )
    else:
        _issue(issues, "NO_CONTRACTS", "coverage", f"Contracts directory does not exist; no contracts authored", artifact_path=".sync/contracts")

    if not wo_files and not any(i.category == "yaml" for i in issues):
        # Only surface NO_WORK_ORDERS once even if both directories are missing
        if not any(i.code == "NO_WORK_ORDERS" for i in issues):
            _issue(issues, "NO_WORK_ORDERS", "coverage", "No authored worker work orders found for the current authoring revision")

    for wo_id in duplicate_ids:
        wo_files.pop(wo_id, None)

    # ── 2. Per-artifact schema/semantic validation (reuse AuthoringGate) ──
    for wo_id, (rel, data) in wo_files.items():
        content = _read_text(ws / rel)
        if content is not None:
            decision = gate.validate_artifact_content(rel, content, agent=None, project_root=ws)
            for err in decision.errors:
                _issue(issues, _issue_code_from_gate_error(err, "WORK_ORDER_INVALID"), "schema", err, work_order_id=wo_id, artifact_path=rel)

    for wo_ref, contract in contracts.items():
        content = _read_text(ws / contract.path)
        if content is not None:
            decision = gate.validate_artifact_content(contract.path, content, agent=None, project_root=ws)
            for err in decision.errors:
                _issue(issues, _issue_code_from_gate_error(err, "CONTRACT_INVALID"), "schema", err, work_order_id=wo_ref or None, artifact_path=contract.path)

    # ── 3. Coverage: every WO has exactly one matching contract ─────
    for wo_id, (rel, data) in sorted(wo_files.items()):
        if wo_id not in contracts:
            _issue(issues, "MISSING_CONTRACT", "coverage", f"Work order '{wo_id}' has no matching contract at .sync/contracts/{wo_id}.yaml", work_order_id=wo_id, artifact_path=rel)

    for wo_ref, contract in sorted(contracts.items()):
        if wo_ref not in wo_files:
            _issue(issues, "ORPHAN_CONTRACT", "coverage", f"Contract '{contract.path}' references work order '{wo_ref}' " f"which is not an active authored work order", work_order_id=wo_ref, artifact_path=contract.path)

    # ── 4. Milestone coverage ───────────────────────────────────────
    if expected_milestones:
        matched_counts: dict[str, list[str]] = {}
        for milestone in expected_milestones:
            matched = [
                wo_id for wo_id, (_rel, data) in wo_files.items()
                if milestone_matches(milestone, str(data.get("title", "")))
            ]
            if len(matched) == 1:
                matched_counts[milestone] = matched
            elif len(matched) == 0:
                _issue(issues, "MILESTONE_UNCOVERED", "coverage", f"Expected implementation milestone '{milestone}' has no authored work order")
            else:
                _issue(issues, "MILESTONE_AMBIGUOUS", "coverage", f"Expected implementation milestone '{milestone}' is covered by multiple " f"work orders ({', '.join(sorted(matched))}); split milestones via a " f"recovery decision instead", artifact_path=", ".join(sorted(matched)))

    # ── 5. Contract binding, roles, deliverables, deps, budget, scope ──
    for wo_id, (rel, data) in sorted(wo_files.items()):
        contract = contracts.get(wo_id)

        assigned = data.get("assigned_agents")
        primary_agent: str | None = None
        if isinstance(assigned, list) and assigned:
            primary_agent = str(assigned[0]).lower().strip()
        elif isinstance(assigned, str) and assigned.strip():
            primary_agent = assigned.lower().strip()
        if not primary_agent:
            _issue(issues, "WORKER_ROLE_UNASSIGNED", "coverage", f"Work order '{wo_id}' has no assigned worker role", work_order_id=wo_id, artifact_path=rel)
        elif primary_agent not in PERMITTED_WORKER_ROLES:
            _issue(issues, "WORKER_ROLE_NOT_PERMITTED", "coverage", f"Work order '{wo_id}' assigns '{primary_agent}' which is not a permitted " f"worker role ({', '.join(PERMITTED_WORKER_ROLES)})", work_order_id=wo_id, artifact_path=rel)
            primary_agent = None

        if contract is not None:
            if str(contract.data.get("work_order") or "") != wo_id:
                _issue(issues, "CONTRACT_WO_MISMATCH", "schema", f"Contract '{contract.path}' declares work_order " f"'{contract.data.get('work_order')}' but is bound to '{wo_id}'", work_order_id=wo_id, artifact_path=contract.path)
            if primary_agent and str(contract.agent_id or "").lower().strip() != primary_agent:
                _issue(issues, "CONTRACT_AGENT_MISMATCH", "schema", f"Contract '{contract.path}' agent_id '{contract.agent_id}' does not match " f"work order assignee '{primary_agent}'", work_order_id=wo_id, artifact_path=contract.path)

            # Budget: explicitly present and positive
            budget = contract.data.get("budget")
            if not isinstance(budget, dict) or "max_files_touched" not in budget:
                _issue(issues, "BUDGET_MISSING", "budget", f"Contract '{contract.path}' must declare an explicit budget with " f"'max_files_touched'", work_order_id=wo_id, artifact_path=contract.path)
            else:
                max_files = budget.get("max_files_touched")
                if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
                    _issue(issues, "BUDGET_INVALID", "budget", f"Contract '{contract.path}' budget max_files_touched={max_files!r} " f"must be a positive integer", work_order_id=wo_id, artifact_path=contract.path)
                else:
                    estimate = data.get("implementation_estimate")
                    if isinstance(estimate, dict):
                        expected_files = estimate.get("expected_files")
                        if isinstance(expected_files, list) and len(expected_files) > max_files:
                            _issue(issues, "BUDGET_UNDER_ESTIMATE", "budget", f"Contract '{contract.path}' budget max_files_touched=" f"{max_files} is smaller than the work order's " f"implementation_estimate of {len(expected_files)} expected files; " f"split the work order instead of under-budgeting it", work_order_id=wo_id, artifact_path=contract.path)

        # Deliverable path/type
        deliverable = data.get("deliverable")
        deliv_path: str | None = None
        if not isinstance(deliverable, dict) or not deliverable.get("type"):
            _issue(issues, "DELIVERABLE_INCOMPLETE", "coverage", f"Work order '{wo_id}' must declare a deliverable type", work_order_id=wo_id, artifact_path=rel)
        else:
            deliv_type = str(deliverable.get("type", "")).lower()
            raw_path = deliverable.get("path")
            deliv_path = str(raw_path).replace("\\", "/").strip().lstrip("/") if raw_path else None
            if deliv_type in ("code", "config", "module") and not deliv_path:
                _issue(issues, "DELIVERABLE_PATH_MISSING", "coverage", f"Work order '{wo_id}' declares a '{deliv_type}' deliverable without a path", work_order_id=wo_id, artifact_path=rel)

        # QA work orders must deliver an executable test suite, not a sign-off
        # document: D024 demands a companion test file for every code deliverable.
        if primary_agent == "gemma":
            qa_ok = (
                isinstance(deliverable, dict)
                and str(deliverable.get("type", "")).lower() == "code"
                and bool(deliv_path)
                and _is_test_artifact(deliv_path)
            )
            if not qa_ok:
                _issue(issues, "QA_DELIVERABLE_NOT_EXECUTABLE", "coverage", f"QA work order '{wo_id}' must declare a code deliverable under tests/ " f"(the test suite its worker will author and execute), got " f"type '{deliverable.get('type') if isinstance(deliverable, dict) else '?'}' " f"path '{deliv_path or 'none'}'", work_order_id=wo_id, artifact_path=rel)
            # The QA verdict channel must be authorized: gemma delivers its
            # verdict to the Architect's inbox, and the dispatch prompt
            # instructs exactly that write.
            if contract is not None and not _scope_allows(
                contract.data, ".sync/inbox/claude/qa-verdict.md"
            ):
                _issue(issues, "QA_VERDICT_CHANNEL_MISSING", "scope", f"Contract '{contract.path}' does not authorize the QA verdict channel " f"'.sync/inbox/claude/**' — the QA worker is instructed to deliver its " f"verdict there and the write would be blocked", work_order_id=wo_id, artifact_path=contract.path)

        # Contract scope must authorize the deliverable path
        if contract is not None and deliv_path:
            if not _scope_allows(contract.data, deliv_path):
                _issue(issues, "DELIVERABLE_OUTSIDE_SCOPE", "scope", f"Contract '{contract.path}' scope does not authorize deliverable path " f"'{deliv_path}'", work_order_id=wo_id, artifact_path=contract.path)

        # Dependencies exist
        deps = data.get("dependencies")
        if isinstance(deps, list):
            for dep in deps:
                dep_str = str(dep).strip()
                if not dep_str:
                    continue
                if dep_str in wo_files or dep_str in skip_ids:
                    continue
                _issue(issues, "DEPENDENCY_MISSING", "dependency", f"Work order '{wo_id}' depends on '{dep_str}' which is not an authored " f"active work order", work_order_id=wo_id, artifact_path=rel)
        elif deps not in (None, []):
            _issue(issues, "DEPENDENCIES_MALFORMED", "dependency", f"Work order '{wo_id}' dependencies must be a list", work_order_id=wo_id, artifact_path=rel)

    # ── 5b. Companion-test coverage planning (D024 shift-left) ──────
    # Every code deliverable must have a test artifact planned somewhere in the
    # authored set — in its own footprint, a QA work order, or its task text —
    # otherwise the run deterministically dead-ends at the GitOps D024 gate.
    planned_tests: list[str] = []
    for _wo_id, (_rel, data) in wo_files.items():
        planned_tests.extend(_planned_test_paths(data))
    for _wo_id, (_rel, data) in wo_files.items():
        deliverable = data.get("deliverable")
        deliv_path_str = ""
        deliv_type_str = ""
        if isinstance(deliverable, dict):
            deliv_path_str = str(deliverable.get("path") or "").replace("\\", "/").strip().lstrip("/")
            deliv_type_str = str(deliverable.get("type") or "").lower()
        if not deliv_path_str or not _is_code_deliverable(deliv_type_str, deliv_path_str):
            continue
        if _is_test_artifact(deliv_path_str):
            continue  # the WO delivers a test itself
        stem = Path(deliv_path_str).stem.lower()
        if stem in ("app", "main", "index", "__init__"):
            covered = bool(planned_tests)
        else:
            covered = any(_covers_stem(t, stem) for t in planned_tests)
        if not covered:
            _issue(issues, "TEST_COVERAGE_UNPLANNED", "coverage", f"No test artifact is planned for code deliverable '{deliv_path_str}'; " f"plan a companion test (e.g. tests/test_{stem}.py) in this work order's " f"tasks or a dedicated QA work order — D024 blocks GitOps without one", work_order_id=_wo_id, artifact_path=_rel)

    # ── 6. Dependency graph acyclicity ──────────────────────────────
    cycle = _find_dependency_cycle(wo_files, skip_ids)
    if cycle:
        _issue(issues, "DEPENDENCY_CYCLE", "dependency", f"Work order dependency graph contains a cycle: {' -> '.join(cycle)}", artifact_path=", ".join(cycle))

    return AuthoringReadinessResult(
        ready=not issues,
        plan_id=plan_id,
        artifact_paths=tuple(sorted(set(artifact_paths))),
        issues=tuple(issues),
    )


def _load_yaml(path: Path, issues: list[ReadinessIssue], rel: str) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _issue(issues, "YAML_PARSE_ERROR", "yaml", f"Artifact '{rel}' failed YAML parsing: {exc}", artifact_path=rel)
        return None
    except Exception as exc:  # unreadable file
        _issue(issues, "ARTIFACT_UNREADABLE", "yaml", f"Artifact '{rel}' could not be read: {exc}", artifact_path=rel)
        return None
    if not isinstance(data, dict):
        _issue(issues, "YAML_STRUCTURE_INVALID", "yaml", f"Artifact '{rel}' root must be a mapping, got {type(data).__name__}", artifact_path=rel)
        return None
    return data


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _issue_code_from_gate_error(error: str, fallback: str) -> str:
    lowered = error.lower()
    if "does not match pattern" in lowered and "id" in lowered:
        return "WORK_ORDER_ID_INVALID"
    if "schema error" in lowered:
        return "SCHEMA_VIOLATION"
    if "overwrite conflict" in lowered:
        return "OVERWRITE_CONFLICT"
    if "deliverable" in lowered:
        return "DELIVERABLE_INVALID"
    if "scope" in lowered and "allow" in lowered:
        return "SCOPE_RULES_INVALID"
    if "budget" in lowered:
        return "BUDGET_INVALID"
    return fallback


def _find_dependency_cycle(
    wo_files: dict[str, tuple[str, dict[str, Any]]], skip_ids: set[str]
) -> list[str] | None:
    """Return one cycle path when the dependency graph is cyclic."""
    graph = {
        wo_id: [d for d in map(str.strip, map(str, data.get("dependencies") or [])) if d in wo_files]
        if isinstance(data.get("dependencies"), list) else []
        for wo_id, (_rel, data) in wo_files.items()
    }
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        return list(exc.args[1])
    return None


__all__ = [
    "AuthoringReadinessResult",
    "PERMITTED_WORKER_ROLES",
    "ReadinessIssue",
    "load_expected_milestones",
    "milestone_matches",
    "validate_authoring_readiness",
]
