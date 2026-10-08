"""Deterministic contract compiler / normalizer for authored governance artifacts.

Sits between the architect's authoring turn and the Authoring Readiness Gate:
the LLM's raw work-order/contract YAML is treated as authoring INTENT; this
module deterministically injects system-owned invariants into the canonical
artifact set and classifies anything it refuses to normalize as
ARCHITECT-DECISION-REQUIRED for a targeted repair turn.

Invariants injected here (from validators/harness/authoring_policy.py):
- QA verdict channel authorization on gemma (QA) contracts
- milestone_id linkage on work orders matched to the approved plan

Semantic decisions (budgets, deliverables, dependencies, scope, test
architecture) are NEVER auto-filled — the compiler either preserves them or
routes the artifact to repair.  No LLM participates in this stage; the output
is a pure function of the on-disk artifact set plus the parsed plan milestones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from validators.harness.authoring_policy import (
    POLICY_VERSION,
    QA_VERDICT_CHANNEL,
    QA_VERDICT_PROBE_PATH,
    TEST_PLAN_FIELD,
    qa_verdict_channel_required,
)
from validators.harness.authoring_readiness import (
    ReadinessIssue,
    _declared_test_plan_entries,
    _is_code_deliverable,
    _is_test_artifact,
    _planned_test_paths,
    _covers_stem,
    _scope_allows,
    _title_tokens,
    milestone_matches,
    _wo_matches_milestone,
)

_NORMALIZED_BY = f"authoring-compiler/{POLICY_VERSION}"


@dataclass
class AuthoringCompileResult:
    """Outcome of one deterministic compilation pass over the authored set."""

    normalized_paths: tuple[str, ...] = ()
    injections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    decision_required: tuple[ReadinessIssue, ...] = ()


def _issue(
    issues: list[ReadinessIssue],
    code: str,
    message: str,
    required_action: str,
    affected: tuple[str, ...] = (),
) -> None:
    issues.append(
        ReadinessIssue(
            code=code,
            category="coverage",
            message=message,
            affected_artifacts=affected,
            required_action=required_action,
            canonical_rule="milestone.identity" if code.startswith("MILESTONE") else "test.companion_coverage",
        )
    )


def _normalize_rel(path_str: Any) -> str:
    return str(path_str or "").replace("\\", "/").strip().lstrip("./")


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def compile_authoring_artifacts(
    workspace: Path | str,
    plan_milestones: list[dict[str, Any]] | None = None,
) -> AuthoringCompileResult:
    """Compile the authored artifact set into its canonical representation.

    Args:
        workspace: governed project root.
        plan_milestones: parsed plan milestones as
            [{"id": ..., "title": ..., "agent": ...}, ...]; None when PLAN.md
            is absent or unparseable (milestone injection is skipped then).

    Returns:
        AuthoringCompileResult with the paths normalized, the injections
        applied, and any ARCHITECT-DECISION-REQUIRED findings.  Artifacts are
        normalized in place; only safe system-owned invariants are written.
    """
    ws = Path(workspace)
    normalized: list[str] = []
    injections: dict[str, list[str]] = {}
    decision_required: list[ReadinessIssue] = []

    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"

    plan_ids: set[str] = set()
    if plan_milestones:
        plan_ids = {str(m.get("id") or "").strip() for m in plan_milestones if m.get("id")}

    def _mark(rel: str, what: str, data: dict[str, Any], path: Path) -> None:
        data["normalized_by"] = _NORMALIZED_BY
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        if rel not in normalized:
            normalized.append(rel)
        injections.setdefault(rel, []).append(what)

    # ── Anti-collision check: detect active work orders colliding with COMPLETED/BLOCKED
    closed_ids: set[str] = set()
    for sub in ("COMPLETED", "BLOCKED"):
        d = ws / ".sync" / "work-orders" / sub
        if d.is_dir():
            closed_ids.update(f.stem for f in d.glob("*.yaml"))

    if closed_ids and active_dir.is_dir():
        active_wo_files = sorted(active_dir.glob("*.yaml"))
        colliding = [f for f in active_wo_files if f.stem in closed_ids]
        if colliding:
            from validators.kernel.daemon.authoring import get_next_work_order_int
            start_idx = get_next_work_order_int(ws)
            id_map: dict[str, str] = {}
            for offset, f in enumerate(colliding):
                new_id = f"WO-{start_idx + offset:03d}"
                id_map[f.stem] = new_id

            for old_id, new_id in id_map.items():
                old_f = active_dir / f"{old_id}.yaml"
                new_f = active_dir / f"{new_id}.yaml"
                if old_f.is_file():
                    data = _load_yaml_mapping(old_f) or {}
                    data["id"] = new_id
                    deps = data.get("dependencies", [])
                    if isinstance(deps, list):
                        data["dependencies"] = [id_map.get(d, d) for d in deps]
                    _mark(new_f.relative_to(ws).as_posix(), f"+renumber: {old_id} -> {new_id} (anti-collision)", data, new_f)
                    old_f.unlink(missing_ok=True)
                if contracts_dir.is_dir():
                    old_c = contracts_dir / f"{old_id}.yaml"
                    new_c = contracts_dir / f"{new_id}.yaml"
                    if old_c.is_file():
                        c_data = _load_yaml_mapping(old_c) or {}
                        c_data["work_order"] = new_id
                        _mark(new_c.relative_to(ws).as_posix(), f"+contract renumber: {old_id} -> {new_id}", c_data, new_c)
                        old_c.unlink(missing_ok=True)

    # ── Work orders: milestone identity + declared test plan ──────────
    wo_records: list[tuple[str, Path, dict[str, Any]]] = []
    for wo_path in sorted(active_dir.glob("*.yaml")) if active_dir.is_dir() else []:
        rel = wo_path.relative_to(ws).as_posix()
        data = _load_yaml_mapping(wo_path)
        if data is None:
            continue  # unreadable/invalid intent — reported by the readiness gate
        wo_records.append((rel, wo_path, data))

    mapped_ids: set[str] = set()
    unmapped: list[tuple[str, Path, dict[str, Any]]] = []
    ambiguous: list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]] = []
    had_ambiguity = False

    for rel, wo_path, data in wo_records:
        # 1. Declared test plan validation (structural only — coverage is
        #    validated by the readiness gate against the canonical set).
        if data.get(TEST_PLAN_FIELD) is not None:
            _, test_plan_error = _declared_test_plan_entries(data)
            if test_plan_error:
                _issue(
                    decision_required,
                    "TEST_PLAN_INVALID",
                    f"Work order '{data.get('id', rel)}' declares an invalid test_plan: {test_plan_error}",
                    "Fix the test_plan mapping: each entry needs 'tests' (list of "
                    "test artifact paths) and 'source' or 'sources' (the code "
                    "deliverable paths they cover).",
                    affected=(rel,),
                )

        # 2. Milestone identity: explicit id (validated) → unique title match
        #    → ordinal 1:1 fallback below.
        if not plan_milestones:
            continue
        wo_mid = str(data.get("milestone_id") or "").strip()
        if wo_mid:
            if plan_ids and wo_mid not in plan_ids:
                _issue(
                    decision_required,
                    "MILESTONE_ID_UNKNOWN",
                    (
                        f"Work order '{data.get('id', rel)}' declares milestone_id "
                        f"'{wo_mid}' which is not an id of the approved plan"
                    ),
                    "Correct `milestone_id` to one of: " + ", ".join(sorted(plan_ids)) + ".",
                    affected=(rel,),
                )
            else:
                mapped_ids.add(wo_mid)
            continue

        matches = [
            m for m in plan_milestones
            if _wo_matches_milestone(data, m)
        ]
        if len(matches) > 1:
            wo_assigned = data.get("assigned_agents")
            wo_agents = [
                str(a).strip().lower()
                for a in (wo_assigned if isinstance(wo_assigned, list) else [wo_assigned])
                if a
            ]
            if wo_agents:
                agent_matches = [
                    m for m in matches
                    if str(m.get("agent") or "").strip().lower() in wo_agents
                ]
                if len(agent_matches) == 1:
                    matches = agent_matches

        if len(matches) == 1:
            data["milestone_id"] = str(matches[0]["id"])
            _mark(rel, f"+milestone_id: {matches[0]['id']}", data, wo_path)
            mapped_ids.add(str(matches[0]["id"]))
        elif len(matches) > 1:
            _issue(
                decision_required,
                "MILESTONE_MAPPING_AMBIGUOUS",
                (
                    f"Work order '{data.get('id', rel)}' title matches multiple plan "
                    f"milestones ({', '.join(str(m.get('id')) for m in matches)}); "
                    "the milestone_id cannot be injected deterministically"
                ),
                "Set `milestone_id` explicitly on the work order to the plan "
                "milestone it implements, or make the title unambiguous.",
                affected=(rel,),
            )
            had_ambiguity = True
        else:
            # The architect legitimately renamed this work order relative to
            # the plan milestone phrasing; resolved by the ordinal fallback
            # below when the authored set is 1:1 with the plan.
            unmapped.append((rel, wo_path, data))

    # 3. Ordinal 1:1 fallback & dependency-inherited decomposition:
    #    When unmapped count matches uncovered count, assign in order.
    #    When all milestones are already covered, unmapped work orders that
    #    depend on a work order belonging to an established milestone inherit
    #    that milestone identity.
    if plan_milestones and not had_ambiguity and unmapped:
        uncovered = [
            m for m in plan_milestones
            if str(m.get("id") or "") not in mapped_ids
        ]
        if len(unmapped) == len(uncovered) and len(wo_records) == len(plan_milestones):
            for (rel, wo_path, data), milestone in zip(unmapped, uncovered):
                data["milestone_id"] = str(milestone["id"])
                _mark(rel, f"+milestone_id: {milestone['id']} (ordinal)", data, wo_path)
                mapped_ids.add(str(milestone["id"]))
            unmapped.clear()
        elif not uncovered:
            wo_id_to_mid = {
                str(d.get("id") or Path(r).stem): str(d.get("milestone_id"))
                for r, _p, d in wo_records
                if d.get("milestone_id")
            }
            for rel, wo_path, data in list(unmapped):
                deps = [str(dep).strip() for dep in (data.get("dependencies") or []) if str(dep).strip()]
                parent_mids = {wo_id_to_mid[dep] for dep in deps if dep in wo_id_to_mid}
                if len(parent_mids) == 1:
                    parent_mid = next(iter(parent_mids))
                    data["milestone_id"] = parent_mid
                    _mark(rel, f"+milestone_id: {parent_mid} (dependency decomposition)", data, wo_path)
                    wo_id_to_mid[str(data.get("id") or Path(rel).stem)] = parent_mid
                    unmapped.remove((rel, wo_path, data))

    # 4. Consolidated test_plan default: when code deliverables lack declared
    #    coverage and the authored set plans exactly ONE test artifact (the
    #    QA work order's suite), map the uncovered deliverables to it.  The
    #    test architecture IS represented in the authored set (the QA work
    #    order); this makes the source→test mapping explicit so the D024 gate
    #    cannot dead-end later.  Multiple planned suites or an explicit
    #    (possibly malformed) declaration defer to the gate / repair.
    declared_coverage: set[str] = set()
    malformed_test_plan_rels: set[str] = set()
    for rel, _path, data in wo_records:
        entries, error = _declared_test_plan_entries(data)
        if error:
            malformed_test_plan_rels.add(rel)
            continue
        for entry in entries:
            declared_coverage.update(entry["sources"])

    planned_test_artifacts: list[str] = []
    for _rel, _path, data in wo_records:
        for planned in _planned_test_paths(data):
            if planned not in planned_test_artifacts:
                planned_test_artifacts.append(planned)

    if len(planned_test_artifacts) == 1:
        suite = planned_test_artifacts[0]
        for rel, wo_path, data in wo_records:
            if rel in malformed_test_plan_rels:
                continue
            deliverable = data.get("deliverable")
            if not isinstance(deliverable, dict):
                continue
            deliv_path_str = _normalize_rel(deliverable.get("path"))
            deliv_type_str = str(deliverable.get("type") or "").lower()
            if not deliv_path_str or not _is_code_deliverable(deliv_type_str, deliv_path_str):
                continue
            if _is_test_artifact(deliv_path_str) or deliv_path_str in declared_coverage:
                continue
            stem = Path(deliv_path_str).stem.lower()
            if stem in ("app", "main", "index", "__init__"):
                continue  # exempt stems are covered by any planned test
            if _covers_stem(suite, stem):
                continue  # the suite already follows the per-stem convention
            if "test_plan" in data:
                continue  # author declared something; never overwrite intent
            data["test_plan"] = [{"sources": [deliv_path_str], "tests": [suite]}]
            _mark(rel, f"+test_plan (consolidated default -> {suite})", data, wo_path)

    # ── Contracts: identity normalization + QA verdict channel ────────
    wo_assignees: dict[str, str] = {}
    for _rel, _path, wo_data in wo_records:
        wo_id_value = str(wo_data.get("id") or Path(_rel).stem)
        assigned = wo_data.get("assigned_agents")
        primary = (
            str(assigned[0]).strip().lower()
            if isinstance(assigned, list) and assigned else ""
        )
        if wo_id_value and primary:
            wo_assignees[wo_id_value] = primary

    for contract_path in sorted(contracts_dir.glob("*.yaml")) if contracts_dir.is_dir() else []:
        rel = contract_path.relative_to(ws).as_posix()
        data = _load_yaml_mapping(contract_path)
        if data is None:
            continue

        # Identity normalization: the contract's agent_id must match its work
        # order's assignee — repair turns routinely copy the author's identity
        # onto the contracts they author, which the readiness gate rejects.
        wo_ref = str(data.get("work_order") or "").strip()
        assignee = wo_assignees.get(wo_ref)
        if assignee and str(data.get("agent_id") or "").strip().lower() != assignee:
            data["agent_id"] = assignee
            _mark(rel, f"agent_id -> {assignee} (matches work order assignee)", data, contract_path)

        scope = data.get("scope")
        if not isinstance(scope, dict):
            scope = {}
            data["scope"] = scope

        if qa_verdict_channel_required(data.get("agent_id")) and not _scope_allows(data, QA_VERDICT_PROBE_PATH):
            allow = scope.get("allow")
            if not isinstance(allow, list):
                allow = []
                scope["allow"] = allow
            allow.append({"module": QA_VERDICT_CHANNEL})
            _mark(rel, f"+scope.allow: {QA_VERDICT_CHANNEL}", data, contract_path)

        # Sanitize contradictory deny rules: remove broad deny patterns
        # (e.g. .sync/**) that would negate an explicitly allowed rule.
        deny_list = scope.get("deny")
        allow_list = scope.get("allow")
        if isinstance(deny_list, list) and isinstance(allow_list, list):
            allow_rules: list[str] = []
            for item in allow_list:
                p = (item.get("module") or item.get("target") or item.get("path")) if isinstance(item, dict) else item
                if p:
                    allow_rules.append(str(p))

            from validators.kernel.contract import ContractEvaluator
            filtered_deny = []
            removed_deny: list[str] = []
            for d_item in deny_list:
                d_pat = (d_item.get("module") or d_item.get("target") or d_item.get("path")) if isinstance(d_item, dict) else d_item
                d_str = str(d_pat or "")
                if any(ContractEvaluator._matches(a.replace("\\", "/").rstrip("/*"), d_str) for a in allow_rules):
                    removed_deny.append(d_str)
                else:
                    filtered_deny.append(d_item)

            if removed_deny:
                scope["deny"] = filtered_deny
                _mark(rel, f"-scope.deny: {', '.join(removed_deny)} (contradicts scope.allow)", data, contract_path)

    return AuthoringCompileResult(
        normalized_paths=tuple(normalized),
        injections={path: tuple(items) for path, items in injections.items()},
        decision_required=tuple(decision_required),
    )


__all__ = [
    "AuthoringCompileResult",
    "compile_authoring_artifacts",
]
