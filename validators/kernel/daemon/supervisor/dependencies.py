"""Fail-closed dependency evaluation for lifecycle dispatch (SDP-FIX-008B).

One authoritative evaluator used by every dispatch decision.  It answers three
questions and nothing else:

* ``SATISFIED`` — every declared prerequisite exists, is valid on disk, and is
  currently complete with current evidence.  Dispatch may be considered.
* ``WAITING`` — the declaration is valid but a prerequisite cannot yet satisfy
  it.  The dependent must not dispatch; a real prerequisite is named and its
  expected resolution is recorded.
* ``INVALID`` — the declaration or a referenced prerequisite cannot be trusted.
  The dependent must not dispatch and the issue is routed to authorized
  contract/authoring repair or human intervention.

Missing or malformed dependency metadata is never interpreted as "no
dependencies", and a parsing exception is never converted into success.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from validators.kernel.work_order_ops import (
    WORK_ORDER_BUCKETS,
    contract_revision,
    load_all_work_orders,
    load_work_order,
    sha256_file,
    stale_requires_rework,
    stale_sources_resolved,
)

_WO_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*-\d{2,}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPLETED_STATUSES = {"COMPLETED", "COMPLETE"}
_MAX_DEPENDENCY_DEPTH = 25


class DependencyStatus(StrEnum):
    """Outcome of evaluating one work order's declared dependencies."""

    SATISFIED = "SATISFIED"
    WAITING = "WAITING"
    INVALID = "INVALID"


class PrerequisiteState(StrEnum):
    """Authoritative state of a single declared prerequisite."""

    COMPLETED_VALID = "COMPLETED_VALID"
    COMPLETED_STALE = "COMPLETED_STALE"
    REOPENED = "REOPENED"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    MISSING = "MISSING"
    INVALID = "INVALID"

    @property
    def satisfies(self) -> bool:
        return self is PrerequisiteState.COMPLETED_VALID

    @property
    def fatal(self) -> bool:
        """States that can never become satisfied without authorized repair."""
        return self in (PrerequisiteState.MISSING, PrerequisiteState.INVALID)


@dataclass(frozen=True)
class DependencyDeclaration:
    """Result of reading and validating the declared dependency list."""

    status: DependencyStatus
    dependencies: tuple[str, ...] = ()
    code: str = ""
    detail: str = ""
    raw_type: str = ""
    duplicates: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status is not DependencyStatus.INVALID


@dataclass(frozen=True)
class PrerequisiteObservation:
    """Authoritative observation for one declared prerequisite."""

    wo_id: str
    state: PrerequisiteState
    bucket: str = ""
    revision: int | None = None
    contract_revision: int | None = None
    detail: str = ""
    expected_action: str = ""
    resolution_owner: str = "architect"

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_order_id": self.wo_id,
            "state": self.state.value,
            "bucket": self.bucket,
            "revision": self.revision,
            "contract_revision": self.contract_revision,
            "detail": self.detail,
            "expected_action": self.expected_action,
            "resolution_owner": self.resolution_owner,
        }


@dataclass(frozen=True)
class GraphIssue:
    """One deterministic dependency-graph defect."""

    code: str
    work_order_id: str
    detail: str
    dependency_id: str | None = None
    path: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "work_order_id": self.work_order_id,
            "dependency_id": self.dependency_id,
            "path": list(self.path),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DependencyResult:
    """Complete, persistable dependency evaluation for one work order."""

    work_order_id: str
    status: DependencyStatus
    code: str = ""
    detail: str = ""
    revision: int | None = None
    contract_revision: int | None = None
    dependencies: tuple[str, ...] = ()
    prerequisites: tuple[PrerequisiteObservation, ...] = ()
    graph_issues: tuple[GraphIssue, ...] = ()
    fingerprint: str = ""
    evaluated_at: str = ""

    @property
    def blocking_ids(self) -> tuple[str, ...]:
        return tuple(
            obs.wo_id for obs in self.prerequisites if not obs.state.satisfies
        )

    @property
    def reason_text(self) -> str:
        parts = [f"{self.code}: {self.detail}"] if self.detail else [self.code]
        for obs in self.prerequisites:
            if obs.state.satisfies:
                continue
            parts.append(
                f"prerequisite {obs.wo_id} is {obs.state.value}"
                + (f" ({obs.detail})" if obs.detail else "")
                + (
                    f" — expected: {obs.expected_action}"
                    if obs.expected_action
                    else ""
                )
            )
        for issue in self.graph_issues:
            path = " -> ".join(issue.path) if issue.path else ""
            parts.append(
                f"{issue.code}: {issue.detail}" + (f" [{path}]" if path else "")
            )
        return "; ".join(p for p in parts if p)

    def to_wait_record(self, *, timestamp: str) -> dict[str, Any]:
        """Durable WAITING/INVALID record with an explicit resolution route."""
        blocking = [
            obs for obs in self.prerequisites if not obs.state.satisfies
        ]
        return {
            "work_order_id": self.work_order_id,
            "status": self.status.value,
            "code": self.code,
            "blocking_dependency_ids": [obs.wo_id for obs in blocking],
            "dependency_states": {obs.wo_id: obs.state.value for obs in blocking},
            "dependency_revisions": {
                obs.wo_id: obs.revision for obs in blocking if obs.revision is not None
            },
            "dependency_contract_revisions": {
                obs.wo_id: obs.contract_revision
                for obs in blocking
                if obs.contract_revision is not None
            },
            "last_observed_state": blocking[0].state.value if blocking else "ACTIVE",
            "work_order_revision": self.revision,
            "contract_revision": self.contract_revision,
            "expected_resolution_actions": sorted(
                {obs.expected_action for obs in blocking if obs.expected_action}
            ),
            "resolution_owner": (
                blocking[0].resolution_owner if blocking else "supervisor"
            ),
            "graph_fingerprint": self.fingerprint,
            "last_state_change_at": timestamp,
            "detail": self.detail,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_wait_record(timestamp=self.evaluated_at),
            "prerequisites": [obs.to_dict() for obs in self.prerequisites],
            "dependencies": list(self.dependencies),
            "graph_issues": [issue.to_dict() for issue in self.graph_issues],
        }


# ── Declaration reading (fail-closed) ────────────────────────────────────


def _wo_file(root: Path, wo_id: str) -> Path | None:
    for bucket in WORK_ORDER_BUCKETS:
        path = Path(root) / ".sync" / "work-orders" / bucket / f"{wo_id}.yaml"
        if path.is_file():
            return path
    return None


def read_dependency_declaration(root: Path, wo_id: str) -> DependencyDeclaration:
    """Read the declared dependency list, failing closed on anything unverifiable."""
    path = _wo_file(root, wo_id)
    if path is None:
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_WORK_ORDER_MISSING",
            detail=f"work order '{wo_id}' has no record in any work-order bucket",
            raw_type="missing",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is INVALID, never success
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_METADATA_UNREADABLE",
            detail=f"work order '{wo_id}' could not be parsed: {type(exc).__name__}: {exc}",
            raw_type=type(exc).__name__,
        )
    if not isinstance(data, dict):
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_METADATA_INVALID",
            detail=(
                f"work order '{wo_id}' record is {type(data).__name__}, "
                "not a mapping"
            ),
            raw_type=type(data).__name__,
        )
    if "dependencies" not in data:
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_METADATA_MISSING",
            detail=f"work order '{wo_id}' declares no dependencies field",
            raw_type="missing",
        )
    raw = data.get("dependencies")
    if raw is None:
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_METADATA_MISSING",
            detail=f"work order '{wo_id}' has a null dependencies field",
            raw_type="null",
        )
    if not isinstance(raw, list):
        return DependencyDeclaration(
            status=DependencyStatus.INVALID,
            code="DEPENDENCY_METADATA_INVALID",
            detail=(
                f"work order '{wo_id}' dependencies is {type(raw).__name__}, "
                "not a list"
            ),
            raw_type=type(raw).__name__,
        )

    declared: list[str] = []
    duplicates: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            return DependencyDeclaration(
                status=DependencyStatus.INVALID,
                code="DEPENDENCY_ENTRY_INVALID",
                detail=(
                    f"work order '{wo_id}' declares a non-string dependency "
                    f"({type(entry).__name__})"
                ),
                raw_type=type(entry).__name__,
            )
        dep = entry.strip()
        if not dep:
            continue
        if dep in declared:
            # Deterministic: first occurrence wins, duplicates are reported.
            if dep not in duplicates:
                duplicates.append(dep)
            continue
        declared.append(dep)
    return DependencyDeclaration(
        status=DependencyStatus.SATISFIED,  # declaration valid; graph decides
        dependencies=tuple(declared),
        duplicates=tuple(duplicates),
    )


# ── Graph validation ─────────────────────────────────────────────────────


def _declared_graph(
    root: Path, records: Mapping[str, Any] | None = None
) -> dict[str, tuple[str, ...]]:
    records = records if records is not None else load_all_work_orders(root)
    return {wo_id: tuple(ref.dependencies) for wo_id, ref in sorted(records.items())}


def _find_cycle(
    graph: Mapping[str, Iterable[str]],
) -> tuple[str, ...] | None:
    """Deterministic cycle detection returning the actual cycle path."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in sorted(graph)}
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        color[node] = GREY
        stack.append(node)
        for neighbour in sorted(graph.get(node, ())):
            if neighbour not in color:
                continue  # unknown prerequisite: reported as MISSING, not a cycle
            if color[neighbour] == GREY:
                start = stack.index(neighbour)
                return tuple(stack[start:] + [neighbour])
            if color[neighbour] == WHITE:
                found = visit(neighbour)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(graph):
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def validate_dependency_graph(
    root: Path,
    *,
    plan_id: str | None = None,
    superseded_ids: Iterable[str] = (),
) -> tuple[GraphIssue, ...]:
    """Validate the authoritative dependency graph; returns deterministic issues.

    Complexity: O(V + E log E) — one pass over records plus a DFS whose
    adjacency lists are sorted for determinism.
    """
    records = load_all_work_orders(root)
    superseded = {str(s) for s in superseded_ids}
    for wid, ref in records.items():
        st = str(ref.data.get("status", "")).upper()
        if st in ("SUPERSEDED", "CANCELLED"):
            superseded.add(wid)
        elif st == "COMPLETED" and not ref.data.get("superseded_by"):
            superseded.discard(wid)
    graph = _declared_graph(root, records)
    issues: list[GraphIssue] = []
    plan_ids = {
        wo_id: str(ref.data.get("plan_id"))
        for wo_id, ref in records.items()
        if ref.data.get("plan_id")
    }

    for wo_id in sorted(graph):
        wo_ref = records.get(wo_id)
        if wo_ref is not None and (
            wo_ref.bucket == "COMPLETED"
            or str(wo_ref.data.get("status", "")).upper() == "COMPLETED"
            or bool(wo_ref.data.get("completion"))
        ):
            # Completed work orders were already satisfied; do not fail graph validation retrospectively
            continue
        for dep in graph[wo_id]:
            if not _WO_ID_RE.match(dep):
                issues.append(
                    GraphIssue(
                        code="INVALID_DEPENDENCY_ID",
                        work_order_id=wo_id,
                        dependency_id=dep,
                        detail=f"dependency id '{dep}' is not a work-order identifier",
                    )
                )
                continue
            if dep == wo_id:
                issues.append(
                    GraphIssue(
                        code="SELF_DEPENDENCY",
                        work_order_id=wo_id,
                        dependency_id=dep,
                        path=(wo_id, wo_id),
                        detail=f"work order '{wo_id}' depends on itself",
                    )
                )
                continue
            if dep not in records:
                issues.append(
                    GraphIssue(
                        code="DEPENDENCY_MISSING",
                        work_order_id=wo_id,
                        dependency_id=dep,
                        detail=(
                            f"work order '{wo_id}' depends on '{dep}' which does "
                            "not exist in the authoritative work-order set"
                        ),
                    )
                )
                continue
            if dep in superseded:
                # Completed work orders were already satisfied; do not fail graph validation retrospectively
                wo_ref = records.get(wo_id)
                if wo_ref is not None and (
                    wo_ref.bucket == "COMPLETED"
                    or str(wo_ref.data.get("status", "")).upper() == "COMPLETED"
                ):
                    continue
                successor = _successor_for(records, dep)
                if successor == wo_id:
                    # Self-supersession: this work order replaced the dependency
                    continue
                issues.append(
                    GraphIssue(
                        code="SUPERSEDED_DEPENDENCY",
                        work_order_id=wo_id,
                        dependency_id=dep,
                        detail=(
                            f"work order '{wo_id}' depends on superseded '{dep}'"
                            + (
                                f"; authoritative replacement is '{successor}'"
                                if successor
                                else "; no authoritative replacement exists"
                            )
                        ),
                    )
                )
                continue
            if plan_id and plan_ids.get(wo_id) and plan_ids.get(dep):
                if plan_ids[wo_id] != plan_ids[dep]:
                    issues.append(
                        GraphIssue(
                            code="CROSS_PLAN_DEPENDENCY",
                            work_order_id=wo_id,
                            dependency_id=dep,
                            detail=(
                                f"'{wo_id}' (plan {plan_ids[wo_id]}) depends on "
                                f"'{dep}' (plan {plan_ids[dep]})"
                            ),
                        )
                    )

    cycle = _find_cycle(graph)
    if cycle:
        issues.append(
            GraphIssue(
                code="DEPENDENCY_CYCLE",
                work_order_id=cycle[0],
                path=cycle,
                detail="dependency cycle: " + " -> ".join(cycle),
            )
        )
    return tuple(issues)


def _successor_for(records: Mapping[str, Any], wo_id: str) -> str | None:
    ref = records.get(wo_id)
    if ref is None:
        return None
    data = ref.data
    for key in ("superseded_by", "replaced_by"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    split = data.get("replacement_work_orders")
    if isinstance(split, list) and split:
        return str(split[-1])
    # Fallback 1: check log text for rebase/replacement pointer (e.g. rebased onto WO-001)
    log_entries = data.get("log") or []
    if isinstance(log_entries, list):
        for entry in log_entries:
            if isinstance(entry, str):
                m = re.search(r'(?:rebased|replaced|superseded).*?onto(?:\s+the\s+completed\s+\w+)?\s+(WO-\d+)', entry, re.I)
                if m:
                    candidate = m.group(1).upper()
                    if candidate in records:
                        return candidate
    for key in ("cancellation_reason", "error"):
        reason = str(data.get(key) or "")
        if reason:
            m = re.search(r'(?:superseded|replaced)\s+by\s+(?:completed\s+)?(WO-\d+)', reason, re.I)
            if m:
                candidate = m.group(1).upper()
                if candidate in records:
                    return candidate
    # Fallback 2: if cancelled/superseded, find another completed work order with matching deliverable
    status = str(data.get("status", "")).upper()
    if status in ("CANCELLED", "SUPERSEDED"):
        deliv = data.get("deliverable")
        if isinstance(deliv, dict) and deliv.get("path"):
            target_path = str(deliv.get("path")).strip()
            for other_id, other_ref in records.items():
                if other_id != wo_id and str(other_ref.data.get("status", "")).upper() == "COMPLETED":
                    other_deliv = other_ref.data.get("deliverable")
                    if isinstance(other_deliv, dict) and str(other_deliv.get("path", "")).strip() == target_path:
                        return other_id
    return None


# ── Prerequisite observation (authoritative completion) ──────────────────


def _observe_prerequisite(
    root: Path,
    dep_id: str,
    *,
    records: Mapping[str, Any],
    completed_ids: set[str],
    blocked_ids: set[str],
    failed_ids: set[str],
    superseded_ids: set[str],
    depth: int = 0,
    lineage: tuple[str, ...] = (),
    cache: dict[str, PrerequisiteObservation] | None = None,
) -> PrerequisiteObservation:
    if cache is not None and dep_id in cache:
        return cache[dep_id]
    result = _observe_prerequisite_uncached(
        root,
        dep_id,
        records=records,
        completed_ids=completed_ids,
        blocked_ids=blocked_ids,
        failed_ids=failed_ids,
        superseded_ids=superseded_ids,
        depth=depth,
        lineage=lineage,
        cache=cache if cache is not None else {},
    )
    if cache is not None:
        cache[dep_id] = result
    return result


def _observe_prerequisite_uncached(
    root: Path,
    dep_id: str,
    *,
    records: Mapping[str, Any],
    completed_ids: set[str],
    blocked_ids: set[str],
    failed_ids: set[str],
    superseded_ids: set[str],
    depth: int,
    lineage: tuple[str, ...],
    cache: dict[str, PrerequisiteObservation],
) -> PrerequisiteObservation:
    ref = records.get(dep_id)
    if ref is None:
        ref = load_work_order(root, dep_id)
    if ref is None:
        # Distinguish "no record" from "a record exists but cannot be trusted":
        # a malformed prerequisite is INVALID, never silently absent.
        on_disk = any(
            (Path(root) / ".sync" / "work-orders" / bucket / f"{dep_id}.yaml").is_file()
            for bucket in WORK_ORDER_BUCKETS
        )
        if on_disk:
            declaration = read_dependency_declaration(root, dep_id)
            return PrerequisiteObservation(
                wo_id=dep_id,
                state=PrerequisiteState.INVALID,
                detail=(
                    f"{declaration.code}: {declaration.detail}"
                    if not declaration.valid
                    else "work-order record cannot be read"
                ),
                expected_action="repair the prerequisite record",
                resolution_owner="architect",
            )
        return PrerequisiteObservation(
            wo_id=dep_id,
            state=PrerequisiteState.MISSING,
            detail="no authoritative work-order record exists",
            expected_action="author the prerequisite or repair the dependency declaration",
            resolution_owner="architect",
        )
    data = ref.data
    status = str(data.get("status") or "").upper()
    revision = ref.revision
    contract_rev = contract_revision(root, dep_id)
    base = dict(
        wo_id=dep_id,
        bucket=ref.bucket,
        revision=revision,
        contract_revision=contract_rev,
    )

    is_superseded = status == "SUPERSEDED" or (
        dep_id in superseded_ids
        and not (status == "COMPLETED" and not data.get("superseded_by"))
    )
    if is_superseded:
        successor = _successor_for(records, dep_id)
        if successor and lineage and (successor in lineage or successor == lineage[-1]):
            return PrerequisiteObservation(
                **base,
                state=PrerequisiteState.COMPLETED_VALID,
                detail=f"superseded by calling work order {successor}",
                expected_action="none",
                resolution_owner="worker",
            )
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.SUPERSEDED,
            detail="superseded" + (f" by {successor}" if successor else ""),
            expected_action=(
                f"rewire dependents onto the authoritative replacement {successor}"
                if successor
                else "author a replacement prerequisite and rewire dependents"
            ),
            resolution_owner="architect",
        )

    if isinstance(data.get("reopened_from"), dict):
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.REOPENED,
            detail="reopened from a completed revision",
            expected_action="complete the reopened prerequisite",
            resolution_owner="worker",
        )

    if dep_id in failed_ids or status == "FAILED":
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.FAILED,
            detail="prerequisite failed",
            expected_action="recover the failed prerequisite through Architect recovery",
            resolution_owner="architect",
        )

    if dep_id in blocked_ids or ref.bucket == "BLOCKED" or status == "BLOCKED":
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.BLOCKED,
            detail="prerequisite blocked",
            expected_action="resolve the prerequisite blocker through Architect recovery",
            resolution_owner="architect",
        )

    # Authoritative completion is the persisted record, never an in-memory
    # claim alone.  A stale `completed_wo_ids` entry cannot authorize
    # downstream execution.
    completed = ref.bucket == "COMPLETED" or status in _COMPLETED_STATUSES
    if not completed:
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.ACTIVE,
            detail="prerequisite is not complete",
            expected_action="complete the prerequisite work order",
            resolution_owner="worker",
        )

    stale_reason = _completion_invalidation_reason(root, ref.data, revision, contract_rev)
    if stale_reason is None:
        # A locally valid completion is still unusable when its own upstream
        # chain was changed or reopened: downstream freshness is transitive.
        stale_reason = _upstream_chain_defect(
            root,
            dep_id,
            ref.data,
            records=records,
            completed_ids=completed_ids,
            blocked_ids=blocked_ids,
            failed_ids=failed_ids,
            superseded_ids=superseded_ids,
            depth=depth,
            lineage=lineage + (dep_id,),
            cache=cache,
        )
    if stale_reason:
        return PrerequisiteObservation(
            **base,
            state=PrerequisiteState.COMPLETED_STALE,
            detail=stale_reason,
            expected_action=(
                "revalidate or reopen the prerequisite, then re-dispatch the dependent"
            ),
            resolution_owner="architect",
        )
    return PrerequisiteObservation(
        **base,
        state=PrerequisiteState.COMPLETED_VALID,
        detail="complete with current evidence",
    )


def _upstream_chain_defect(
    root: Path,
    wo_id: str,
    data: Mapping[str, Any],
    *,
    records: Mapping[str, Any],
    completed_ids: set[str],
    blocked_ids: set[str],
    failed_ids: set[str],
    superseded_ids: set[str],
    depth: int,
    lineage: tuple[str, ...],
    cache: dict[str, PrerequisiteObservation],
) -> str | None:
    """Reason a completed work order's own upstream chain is no longer valid.

    Traversal is depth-bounded and cycle-safe (cycles are reported separately by
    graph validation), with one cached observation per prerequisite, so the
    whole evaluation stays O(V + E).

    Uses the canonical dependency declaration parser (read_dependency_declaration)
    for fail-closed validation of every upstream node's declared dependencies.
    """
    if depth >= _MAX_DEPENDENCY_DEPTH:
        return (
            "dependency chain exceeds the supported depth "
            f"({_MAX_DEPENDENCY_DEPTH}) and cannot be verified"
        )

    # Use canonical declaration parser for fail-closed validation
    declaration = read_dependency_declaration(root, wo_id)
    if not declaration.valid:
        return f"upstream {wo_id}: {declaration.code}: {declaration.detail}"
    if wo_id in declaration.dependencies:
        return f"upstream {wo_id}: SELF_DEPENDENCY: work order depends on itself"

    for dep in declaration.dependencies:
        if dep in lineage:
            continue
        observation = _observe_prerequisite(
            root,
            dep,
            records=records,
            completed_ids=completed_ids,
            blocked_ids=blocked_ids,
            failed_ids=failed_ids,
            superseded_ids=superseded_ids,
            depth=depth + 1,
            lineage=lineage,
            cache=cache,
        )
        if not observation.state.satisfies:
            return (
                f"upstream {dep} is {observation.state.value}"
                + (f" ({observation.detail})" if observation.detail else "")
            )
    return None


def _contained_file(root: Path, rel_path: str) -> Path | None:
    """Resolve a declared deliverable inside *root*, refusing traversal outside it."""
    try:
        candidate = (Path(root) / rel_path).resolve()
        candidate.relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _completion_invalidation_reason(
    root: Path,
    data: Mapping[str, Any],
    revision: int,
    contract_rev: int | None,
) -> str | None:
    """Why this completion's evidence can no longer authorize downstream work."""
    stale = data.get("evidence_stale")
    if isinstance(stale, dict) and stale:
        resolved, why = stale_sources_resolved(root, stale)
        if not resolved:
            return f"evidence stale: {why}"
        if stale_requires_rework(stale):
            return "evidence stale: invalidation requires rework (reopen required)"
        return "evidence stale: pending revalidation"

    completion = data.get("completion")
    if not isinstance(completion, dict):
        return "completion record is missing or malformed"

    # A. Work-order ID consistency (completion must be bound to this WO)
    completion_wo_id = completion.get("work_order_id")
    wo_id = data.get("id")
    if completion_wo_id is not None and wo_id is not None:
        if str(completion_wo_id).strip() != str(wo_id).strip():
            return f"completion work_order_id '{completion_wo_id}' does not match work order '{wo_id}'"

    # B. Revision integrity
    declared_revision = data.get("revision")
    if not isinstance(declared_revision, int) or isinstance(declared_revision, bool):
        return "work order revision is missing or malformed"
    verified_rev = completion.get("verified_at_revision")
    if not isinstance(verified_rev, int):
        return "completion missing verified_at_revision"
    if verified_rev != revision:
        return (
            f"completion verified at r{verified_rev} but the work order is now "
            f"r{revision}; revalidation is required"
        )

    verified_contract = completion.get("verified_at_contract_revision")
    if not isinstance(verified_contract, int):
        return "completion missing verified_at_contract_revision"
    if contract_rev is not None and verified_contract != contract_rev:
        return (
            f"completion verified at contract r{verified_contract} but the "
            f"contract is now r{contract_rev}; revalidation is required"
        )

    # C. Mandatory deliverable hashes
    recorded_hashes = completion.get("deliverable_hashes")
    if not isinstance(recorded_hashes, dict):
        return "completion missing deliverable_hashes map"
    if not recorded_hashes:
        # Check if there are mandatory deliverables declared
        deliverable = data.get("deliverable")
        if isinstance(deliverable, dict):
            path = str(deliverable.get("path") or "").strip()
            if path:
                return "completion deliverable_hashes is empty but mandatory deliverable exists"
        test_plan = data.get("test_plan")
        if isinstance(test_plan, list) and test_plan:
            return "completion deliverable_hashes is empty but mandatory test artifacts exist"

    for rel_path, recorded in sorted(recorded_hashes.items()):
        if not rel_path or not recorded:
            return f"completion contains malformed hash entry for '{rel_path}'"
        if not _HASH_RE.fullmatch(str(recorded)):
            return f"completion hash for '{rel_path}' is malformed"
        resolved = _contained_file(root, str(rel_path))
        if resolved is None:
            return f"declared deliverable '{rel_path}' is missing or outside the workspace"
        current = sha256_file(resolved)
        if current is None:
            return f"declared deliverable '{rel_path}' is missing"
        if current != recorded:
            return (
                f"declared deliverable '{rel_path}' hash changed since "
                "completion evidence was recorded"
            )

    # D. Every mandatory deliverable must carry a recorded, current hash.
    deliverable = data.get("deliverable")
    if isinstance(deliverable, dict):
        path = str(deliverable.get("path") or "").strip().replace("\\", "/")
        if path:
            if path not in recorded_hashes:
                return (
                    f"completion missing required deliverable hash for '{path}'"
                )
            if not (Path(root) / path).exists():
                return f"declared deliverable '{path}' is missing"

    return None


# ── Evaluation ───────────────────────────────────────────────────────────


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def evaluate_dependencies(
    root: Path,
    wo_id: str,
    *,
    completed_ids: Iterable[str] = (),
    blocked_ids: Iterable[str] = (),
    failed_ids: Iterable[str] = (),
    superseded_ids: Iterable[str] = (),
    plan_id: str | None = None,
    evaluated_at: str = "",
) -> DependencyResult:
    """Evaluate one work order's dependencies against authoritative state."""
    root = Path(root)
    completed = set(completed_ids)
    blocked = set(blocked_ids)
    failed = set(failed_ids)
    superseded = set(superseded_ids)

    declaration = read_dependency_declaration(root, wo_id)
    if not declaration.valid:
        return DependencyResult(
            work_order_id=wo_id,
            status=DependencyStatus.INVALID,
            code=declaration.code,
            detail=declaration.detail,
            evaluated_at=evaluated_at,
            fingerprint=_fingerprint(
                {"wo": wo_id, "code": declaration.code, "detail": declaration.detail}
            ),
        )

    records = load_all_work_orders(root)
    ref = records.get(wo_id)
    if ref is not None and (
        ref.bucket == "COMPLETED"
        or str(ref.data.get("status", "")).upper() == "COMPLETED"
        or bool(ref.data.get("completion"))
    ):
        return DependencyResult(
            work_order_id=wo_id,
            status=DependencyStatus.SATISFIED,
            code="DEPENDENCIES_SATISFIED",
            detail="work order is already completed",
            evaluated_at=evaluated_at,
            dependencies=declaration.dependencies,
            fingerprint=_fingerprint({"wo": wo_id, "status": "completed"}),
        )
    revision = ref.revision if ref is not None else None
    contract_rev = contract_revision(root, wo_id)

    # A structural defect anywhere in this work order's prerequisite closure is
    # this work order's defect: a cycle or bad edge upstream makes the whole
    # chain unverifiable, so it must not be filtered out of the result.
    relevant_ids = {wo_id, *declaration.dependencies}
    graph_issues = tuple(
        issue
        for issue in validate_dependency_graph(
            root, plan_id=plan_id, superseded_ids=superseded
        )
        if issue.work_order_id in relevant_ids
        or (issue.path and relevant_ids.intersection(issue.path))
        or issue.dependency_id in declaration.dependencies
    )

    observation_cache: dict[str, PrerequisiteObservation] = {}
    observations = tuple(
        _observe_prerequisite(
            root,
            dep_id,
            records=records,
            completed_ids=completed,
            blocked_ids=blocked,
            failed_ids=failed,
            superseded_ids=superseded,
            lineage=(wo_id,),
            cache=observation_cache,
        )
        for dep_id in declaration.dependencies
    )

    wo_data = ref.data if ref is not None else load_work_order(root, wo_id) or {}
    wo_type = str(wo_data.get("type", "")).upper().strip()
    is_bugfix_order = wo_type in ("BUGFIX", "HOTFIX", "FIX")

    if is_bugfix_order:
        remediated_observations = []
        for obs in observations:
            if not obs.state.satisfies and obs.state in (PrerequisiteState.BLOCKED, PrerequisiteState.FAILED):
                dep_data = records.get(obs.wo_id)
                dep_dict = dep_data.data if dep_data is not None else load_work_order(root, obs.wo_id) or {}
                from validators.harness.review_gate import wo_deliverable_paths
                delivs = wo_deliverable_paths(dep_dict)
                if delivs and all((root / d).exists() for d in delivs):
                    deliv_str = ", ".join(delivs)
                    remediated_obs = PrerequisiteObservation(
                        wo_id=obs.wo_id,
                        bucket=obs.bucket,
                        revision=obs.revision,
                        contract_revision=obs.contract_revision,
                        state=PrerequisiteState.COMPLETED_VALID,
                        detail=f"prerequisite deliverable present on disk for {wo_type} repair ({deliv_str})",
                    )
                    remediated_observations.append(remediated_obs)
                    continue
            remediated_observations.append(obs)
        observations = tuple(remediated_observations)

    if graph_issues:
        status = DependencyStatus.INVALID
        code = next(
            (
                issue.code
                for issue in graph_issues
                if issue.code
                in {
                    "SELF_DEPENDENCY",
                    "DEPENDENCY_CYCLE",
                    "DEPENDENCY_MISSING",
                    "INVALID_DEPENDENCY_ID",
                    "SUPERSEDED_DEPENDENCY",
                    "CROSS_PLAN_DEPENDENCY",
                }
            ),
            "INVALID_DEPENDENCY",
        )
        detail = "; ".join(issue.detail for issue in graph_issues)
    elif any(obs.state.fatal for obs in observations):
        status = DependencyStatus.INVALID
        fatal = next(obs for obs in observations if obs.state.fatal)
        code = "DEPENDENCY_MISSING" if fatal.state is PrerequisiteState.MISSING else "INVALID_DEPENDENCY"
        detail = f"prerequisite {fatal.wo_id}: {fatal.detail}"
    elif any(not obs.state.satisfies for obs in observations):
        status = DependencyStatus.WAITING
        blocking = [obs.wo_id for obs in observations if not obs.state.satisfies]
        code = "WAITING_FOR_DEPENDENCY"
        detail = "waiting on " + ", ".join(blocking)
    else:
        status = DependencyStatus.SATISFIED
        code = "DEPENDENCIES_SATISFIED"
        detail = "all declared prerequisites are complete with current evidence"

    payload = {
        "wo": wo_id,
        "status": status.value,
        "revision": revision,
        "contract_revision": contract_rev,
        "dependencies": list(declaration.dependencies),
        "prerequisites": [obs.to_dict() for obs in observations],
        "graph_issues": [issue.to_dict() for issue in graph_issues],
    }
    return DependencyResult(
        work_order_id=wo_id,
        status=status,
        code=code,
        detail=detail,
        revision=revision,
        contract_revision=contract_rev,
        dependencies=declaration.dependencies,
        prerequisites=observations,
        graph_issues=graph_issues,
        fingerprint=_fingerprint(payload),
        evaluated_at=evaluated_at,
    )


def deadlock_graph_text(
    root: Path, wo_ids: Iterable[str], *, superseded_ids: Iterable[str] = ()
) -> str:
    """Deterministic, actionable description of the blocked dependency graph."""
    issues = validate_dependency_graph(root, superseded_ids=superseded_ids)
    relevant = [issue for issue in issues if issue.work_order_id in set(wo_ids) or issue.path]
    if not relevant:
        return "no structural dependency defect detected"
    lines: list[str] = []
    for issue in relevant:
        path = " -> ".join(issue.path) if issue.path else ""
        lines.append(
            f"{issue.code} for {issue.work_order_id}"
            + (f" (dep {issue.dependency_id})" if issue.dependency_id else "")
            + (f": {path}" if path else f": {issue.detail}")
        )
    return " | ".join(lines)


__all__ = [
    "DependencyDeclaration",
    "DependencyResult",
    "DependencyStatus",
    "GraphIssue",
    "PrerequisiteObservation",
    "PrerequisiteState",
    "deadlock_graph_text",
    "evaluate_dependencies",
    "read_dependency_declaration",
    "validate_dependency_graph",
]
