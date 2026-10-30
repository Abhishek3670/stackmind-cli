"""SDP-FIX-008B — dependency integrity and deadlock prevention.

Deterministic tests over isolated temporary workspaces.  Nothing here mocks the
dependency evaluator or the supervisor transition logic: the evaluator is driven
against real work-order records on disk, and supervisor tests go through
``advance()`` / ``_advance_executing()`` with a deterministic fake operation
manager.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_lifecycle_supervisor import MockSessionManager
from validators.kernel.daemon.supervisor import (
    AdvanceResult,
    LifecycleSupervisor,
    Phase,
    RunState,
)
from validators.kernel.daemon.supervisor.dependencies import (
    DependencyStatus,
    PrerequisiteState,
    evaluate_dependencies,
    read_dependency_declaration,
    validate_dependency_graph,
)
from validators.kernel.work_order_ops import mark_dependents_stale

_OMIT = object()


# ── Fixtures ────────────────────────────────────────────────────────────


def _wo_dir(root: Path, bucket: str) -> Path:
    path = root / ".sync" / "work-orders" / bucket
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_raw(root: Path, wo_id: str, text: str, bucket: str = "ACTIVE") -> Path:
    path = _wo_dir(root, bucket) / f"{wo_id}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_wo(
    root: Path,
    wo_id: str,
    *,
    bucket: str = "ACTIVE",
    deps: Any = (),
    status: str | None = None,
    deliverable: str | None = None,
    extra: dict[str, Any] | None = None,
    contract_revision: int = 1,
) -> Path:
    data: dict[str, Any] = {
        "id": wo_id,
        "title": f"Task {wo_id}",
        "revision": 1,
        "status": status or ("COMPLETED" if bucket == "COMPLETED" else "ACTIVE"),
        "assigned_agents": ["codex"],
    }
    if deps is not _OMIT:
        # Preserve the declared value verbatim so malformed metadata stays
        # malformed (a string must not become a list of characters).
        data["dependencies"] = list(deps) if isinstance(deps, (list, tuple)) else deps
    if deliverable:
        data["deliverable"] = {"type": "code", "path": deliverable}
    if bucket == "COMPLETED":
        data.setdefault("completed_at", "2026-01-01T00:00:00Z")
        # Add authoritative completion record for HF1-A compliance
        from validators.kernel.work_order_ops import sha256_file
        deliverable_hashes: dict[str, str] = {}
        if deliverable:
            # Compute hash of the deliverable file if it exists
            deliverable_path = root / deliverable
            if deliverable_path.is_file():
                deliverable_hashes[deliverable] = sha256_file(deliverable_path) or ""
        data["completion"] = {
            "work_order_id": wo_id,
            "verified_at_revision": 1,
            "verified_at_contract_revision": contract_revision,
            "deliverable_hashes": deliverable_hashes,
        }
    if extra:
        data.update(extra)
    return _write_raw(
        root, wo_id, yaml.safe_dump(data, sort_keys=False), bucket=bucket
    )


def _complete(root: Path, wo_id: str, *, deps: Any = (), contract_revision: int = 1) -> None:
    """Authoritatively complete a work order: one COMPLETED record, no stale copies."""
    for bucket in ("ACTIVE", "BLOCKED"):
        path = _wo_dir(root, bucket) / f"{wo_id}.yaml"
        if path.is_file():
            path.unlink()
    # Read the ACTIVE work order to get deliverable info before deleting
    active_path = _wo_dir(root, "ACTIVE") / f"{wo_id}.yaml"
    deliverable = None
    if active_path.is_file():
        try:
            active_data = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            if isinstance(active_data, dict):
                deliverable_data = active_data.get("deliverable")
                if isinstance(deliverable_data, dict):
                    deliverable = deliverable_data.get("path")
        except Exception:
            pass
    _write_wo(root, wo_id, bucket="COMPLETED", status="COMPLETED", deps=deps, deliverable=deliverable, contract_revision=contract_revision)


def _write_contract(root: Path, wo_id: str, revision: int) -> None:
    contracts = root / ".sync" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / f"{wo_id}.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "work_order": wo_id,
                "agent_id": "codex",
                "contract_revision": revision,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class RecordingManager(MockSessionManager):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.dispatched: list[Any] = []

    def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        record = super().start_turn(session_id, prompt, **kwargs)
        self.dispatched.append(kwargs.get("work_order_id"))
        return record


def _state(
    sup: LifecycleSupervisor,
    ws: Path,
    *,
    wos: tuple[str, ...] = ("WO-001",),
    phase: Phase = Phase.EXECUTING,
    **overrides: Any,
) -> RunState:
    state = sup.start_run("run-008b", "Goal", ws, "sess-008b")
    state.phase = phase
    state.worker_wo_ids = list(wos)
    state.published_wo_ids = list(wos)
    state.global_turn_budget = 25
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ── 1. Metadata validation (fail-closed) ────────────────────────────────


def test_missing_work_order_returns_invalid(tmp_path: Path) -> None:
    result = evaluate_dependencies(tmp_path, "WO-404")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_WORK_ORDER_MISSING"


def test_malformed_yaml_returns_invalid(tmp_path: Path) -> None:
    _write_raw(tmp_path, "WO-001", "id: WO-001\ndependencies: [unclosed\n")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_METADATA_UNREADABLE"


def test_yaml_scalar_returns_invalid(tmp_path: Path) -> None:
    _write_raw(tmp_path, "WO-001", "just a scalar\n")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_METADATA_INVALID"


def test_dependencies_string_returns_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps="WO-002")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_METADATA_INVALID"


def test_dependencies_null_returns_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=None)
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_METADATA_MISSING"


def test_missing_dependencies_field_returns_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=_OMIT)
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_METADATA_MISSING"


def test_duplicate_dependency_ids_are_deterministic(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002", "WO-002", "WO-002"])
    _write_wo(tmp_path, "WO-002", bucket="COMPLETED")
    declaration = read_dependency_declaration(tmp_path, "WO-001")
    assert declaration.dependencies == ("WO-002",)
    assert declaration.duplicates == ("WO-002",)
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.SATISFIED


def test_empty_dependency_list_is_explicitly_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.SATISFIED
    assert result.code == "DEPENDENCIES_SATISFIED"


def test_unreadable_bytes_never_return_satisfied(tmp_path: Path) -> None:
    path = _wo_dir(tmp_path, "ACTIVE") / "WO-001.yaml"
    path.write_bytes(b"\xff\xfe\x00\x00not utf8")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID


# ── 2. Graph integrity ──────────────────────────────────────────────────


def test_self_dependency_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-001"])
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "SELF_DEPENDENCY"


def test_direct_cycle_is_detected_with_path(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_CYCLE"
    cycle_issue = next(i for i in result.graph_issues if i.code == "DEPENDENCY_CYCLE")
    assert set(cycle_issue.path[:2]) == {"WO-001", "WO-002"}
    assert len(cycle_issue.path) == 3
    assert " -> " in result.reason_text


def test_transitive_cycle_is_detected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=["WO-003"])
    _write_wo(tmp_path, "WO-003", deps=["WO-001"])
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    cycle = next(i for i in result.graph_issues if i.code == "DEPENDENCY_CYCLE")
    assert {"WO-001", "WO-002", "WO-003"}.issubset(set(cycle.path))


def test_missing_dependency_is_reported_not_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"], deliverable="src/a.py")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.INVALID
    assert result.code == "DEPENDENCY_MISSING"
    assert "WO-999" in result.reason_text
    assert result.revision == 1


def test_cross_plan_dependency_is_rejected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"], extra={"plan_id": "PLAN-1"})
    _write_wo(tmp_path, "WO-002", bucket="COMPLETED", extra={"plan_id": "PLAN-2"})
    result = evaluate_dependencies(tmp_path, "WO-001", plan_id="PLAN-1")
    assert result.status is DependencyStatus.INVALID
    assert any(i.code == "CROSS_PLAN_DEPENDENCY" for i in result.graph_issues)


def test_amendment_introducing_a_cycle_is_rejected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=[])
    assert not [i for i in validate_dependency_graph(tmp_path) if i.code == "DEPENDENCY_CYCLE"]
    # Architect amendment adds the back edge.
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    issues = validate_dependency_graph(tmp_path)
    assert [i for i in issues if i.code == "DEPENDENCY_CYCLE"]


def test_superseded_dependency_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=[], extra={"superseded_by": "WO-003"})
    result = evaluate_dependencies(tmp_path, "WO-001", superseded_ids={"WO-002"})
    assert result.status is DependencyStatus.INVALID
    assert any(i.code == "SUPERSEDED_DEPENDENCY" for i in result.graph_issues)
    assert "WO-003" in result.reason_text


def test_reopened_prerequisite_does_not_satisfy(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(
        tmp_path,
        "WO-002",
        bucket="ACTIVE",
        status="ACTIVE",
        extra={
            "reopened_from": {"revision": 1, "reason": "defect"},
            "completion_history": [{"revision": 1, "status": "COMPLETED"}],
        },
    )
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.REOPENED
    # Historical completion is preserved, not erased.
    data = yaml.safe_load(
        (_wo_dir(tmp_path, "ACTIVE") / "WO-002.yaml").read_text(encoding="utf-8")
    )
    assert data["completion_history"][0]["status"] == "COMPLETED"


# ── 3. Authoritative completion and evidence ────────────────────────────


def test_completed_with_valid_evidence_is_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"], deliverable="src/a.py")
    _wo_dir(tmp_path, "ACTIVE").joinpath().mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _write_wo(tmp_path, "WO-002", bucket="COMPLETED", deliverable="src/a.py")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_VALID


def test_stale_evidence_is_not_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(
        tmp_path,
        "WO-002",
        bucket="COMPLETED",
        extra={
            "evidence_stale": {
                "revision": 2,
                "reason": "upstream changed",
                "fields": ["acceptance"],
                "requires_rework": True,
            }
        },
    )
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_changed_contract_revision_requires_revalidation(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(
        tmp_path,
        "WO-002",
        bucket="COMPLETED",
        extra={
            "completion": {
                "work_order_id": "WO-002",
                "verified_at_revision": 1,
                "verified_at_contract_revision": 1,
                "deliverable_hashes": {},
            }
        },
    )
    _write_contract(tmp_path, "WO-002", revision=2)
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "contract" in result.prerequisites[0].detail


def test_artifact_hash_mismatch_invalidates_completion(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(
        tmp_path,
        "WO-002",
        bucket="COMPLETED",
        deliverable="src/a.py",
        extra={
            "completion": {
                "work_order_id": "WO-002",
                "verified_at_revision": 1,
                "verified_at_contract_revision": 1,
                "deliverable_hashes": {"src/a.py": "0" * 64},
            }
        },
    )
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert "hash changed" in result.prerequisites[0].detail


def test_blocked_prerequisite_does_not_satisfy(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", bucket="BLOCKED", status="BLOCKED")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.BLOCKED
    assert "Architect recovery" in result.prerequisites[0].expected_action


def test_failed_prerequisite_does_not_satisfy(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", status="FAILED")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.FAILED


def test_missing_completion_record_does_not_satisfy(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", bucket="ACTIVE", status="ACTIVE")
    result = evaluate_dependencies(tmp_path, "WO-001")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.ACTIVE


# ── 4. Transitive freshness ─────────────────────────────────────────────


def _chain(root: Path) -> None:
    """A ← B ← C ← D plus an unrelated branch E (all complete and valid)."""
    _write_wo(root, "WO-001", deps=[], bucket="COMPLETED")
    for wo_id, dep in (("WO-002", "WO-001"), ("WO-003", "WO-002"), ("WO-004", "WO-003")):
        _write_wo(root, wo_id, deps=[dep], bucket="COMPLETED")
    _write_wo(root, "WO-005", deps=[], bucket="COMPLETED")


def test_upstream_change_invalidates_transitive_descendants(tmp_path: Path) -> None:
    _chain(tmp_path)
    assert evaluate_dependencies(tmp_path, "WO-004").status is DependencyStatus.SATISFIED

    mark_dependents_stale(
        tmp_path,
        upstream_id="WO-001",
        upstream_revision=2,
        reason="reopened by Architect",
        fields=["deliverable"],
        actor="architect",
    )

    # WO-002's own completion evidence is now stale, so it can never be a valid
    # prerequisite: every descendant loses eligibility.
    descendant = evaluate_dependencies(tmp_path, "WO-003")
    assert descendant.status is DependencyStatus.WAITING
    assert descendant.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "evidence stale" in descendant.prerequisites[0].detail or "upstream" in descendant.prerequisites[0].detail
    assert evaluate_dependencies(tmp_path, "WO-004").status is DependencyStatus.WAITING
    # Unrelated branch is untouched.
    assert evaluate_dependencies(tmp_path, "WO-005").status is DependencyStatus.SATISFIED


def test_invalidation_preserves_historical_completion(tmp_path: Path) -> None:
    _chain(tmp_path)
    before = yaml.safe_load(
        (_wo_dir(tmp_path, "COMPLETED") / "WO-002.yaml").read_text(encoding="utf-8")
    )
    mark_dependents_stale(
        tmp_path,
        upstream_id="WO-001",
        upstream_revision=2,
        reason="reopened",
        fields=["deliverable"],
        actor="architect",
    )
    after = yaml.safe_load(
        (_wo_dir(tmp_path, "COMPLETED") / "WO-002.yaml").read_text(encoding="utf-8")
    )
    assert after["status"] == "COMPLETED"
    assert after.get("completed_at") == before.get("completed_at")
    assert after["evidence_stale"]["sources"][0]["upstream_work_order"] == "WO-001"


def test_repeated_invalidation_does_not_duplicate_sources(tmp_path: Path) -> None:
    _chain(tmp_path)
    for _ in range(3):
        mark_dependents_stale(
            tmp_path,
            upstream_id="WO-001",
            upstream_revision=2,
            reason="reopened",
            fields=["deliverable"],
            actor="architect",
        )
    data = yaml.safe_load(
        (_wo_dir(tmp_path, "COMPLETED") / "WO-002.yaml").read_text(encoding="utf-8")
    )
    sources = data["evidence_stale"]["sources"]
    assert len([s for s in sources if s["upstream_work_order"] == "WO-001"]) == 1


# ── 5. Waiting / recovery through the supervisor ────────────────────────


def test_active_prerequisite_creates_waiting_state_and_no_retry_burn(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))

    for _ in range(3):
        sup.advance(state)

    assert "WO-002" not in mgr.dispatched
    wait = state.dependency_waits["WO-002"]
    assert wait["status"] == "WAITING"
    assert wait["blocking_dependency_ids"] == ["WO-001"]
    assert wait["resolution_owner"] == "worker"
    # Polling the graph consumes no worker/dispatch retry budget.
    assert state.retry_counts == {}
    assert state.dispatch_failures == {}
    assert state.attempt_counts == {}


def test_resolved_dependency_makes_dependent_dispatchable(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)
    assert "WO-002" not in mgr.dispatched

    # Complete the prerequisite authoritatively (operation terminal + record).
    op1 = next(op for op in mgr.operations.values() if op.get("work_order_id") == "WO-001")
    mgr.complete_operation(op1["operation_id"], "COMPLETED")
    _complete(tmp_path, "WO-001")
    state.completed_wo_ids.append("WO-001")

    sup.advance(state)
    assert "WO-002" in mgr.dispatched
    assert "WO-002" not in state.dependency_waits


def test_invalid_dependency_blocks_and_routes_to_repair(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)

    res = sup.advance(state)

    assert res == AdvanceResult.TRANSITIONED
    assert state.phase is Phase.ARCHITECT_RECOVERY_DECISION
    assert "WO-001" in state.blocked_wo_ids
    blocker = state.worker_blockers["WO-001"]
    assert blocker["failure_code"] == "INVALID_DEPENDENCY"
    assert blocker["missing_dependencies"] == ["WO-999"]
    assert blocker["resolution_owner"] == "architect"
    assert state.dependency_waits["WO-001"]["status"] == "INVALID"
    assert "WO-001" not in mgr.dispatched


def test_clearing_blocked_ids_cannot_bypass_invalid_dependency(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)
    sup.advance(state)

    state.blocked_wo_ids.clear()
    state.dispatch_failures.clear()
    state.recovery_wo_id = None
    state.recovery_decision_operation_id = None
    state.dependency_waits.clear()
    state.phase = Phase.EXECUTING

    assert sup._dependencies_met(state, "WO-001", tmp_path) is False
    sup.advance(state)
    assert "WO-001" not in mgr.dispatched  # no worker turn was created
    assert state.worker_blockers["WO-001"]["failure_code"] == "INVALID_DEPENDENCY"


def test_dependency_waiting_does_not_consume_dispatch_attempts(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    for _ in range(2):
        sup.advance(state)
    assert state.dispatch_failures == {}
    assert state.recovery_attempts == {}


def test_dependency_resolution_does_not_reset_recovery_history(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(
        sup, tmp_path, wos=("WO-001", "WO-002"), recovery_attempts={"WO-002": 2}
    )
    sup.advance(state)
    op1 = next(op for op in mgr.operations.values() if op.get("work_order_id") == "WO-001")
    mgr.complete_operation(op1["operation_id"], "COMPLETED")
    _complete(tmp_path, "WO-001")
    state.completed_wo_ids.append("WO-001")
    sup.advance(state)
    assert "WO-002" in mgr.dispatched
    assert state.recovery_attempts["WO-002"] == 2


def test_no_duplicate_operations_for_a_satisfied_dependency(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)
    sup.advance(state)
    sup.advance(state)
    ops = [op for op in mgr.operations.values() if op.get("work_order_id") == "WO-001"]
    assert len(ops) == 1


def test_permanently_blocked_prerequisite_does_not_wait_forever(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)
    state.max_stall_ticks = 2

    [sup.advance(state) for _ in range(4)]

    # The invalid dependency is surfaced as an actionable blockage and routed to
    # repair — it never becomes an endless EXECUTING poll.
    assert state.phase is not Phase.EXECUTING
    assert state.phase in (
        Phase.ARCHITECT_RECOVERY_DECISION,
        Phase.WAITING_FOR_HUMAN,
        Phase.BLOCKED,
    )
    assert any(
        blocker.get("failure_code") == "INVALID_DEPENDENCY"
        for blocker in state.worker_blockers.values()
    )


def test_dependency_deadlock_escalates_with_actionable_graph(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))

    sup.advance(state)

    assert state.phase is Phase.ARCHITECT_RECOVERY_DECISION
    blocker = state.worker_blockers.get("WO-001") or state.worker_blockers.get("WO-002")
    assert blocker["failure_code"] == "INVALID_DEPENDENCY"
    graph_issues = blocker["graph_issues"]
    assert any(i["code"] == "DEPENDENCY_CYCLE" for i in graph_issues)
    path = next(i["path"] for i in graph_issues if i["code"] == "DEPENDENCY_CYCLE")
    assert len(path) >= 3


def test_unchanged_invalid_graph_does_not_redispatch_recovery(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)

    sup.advance(state)  # invalid dependency → recovery decision phase
    sup.advance(state)  # decision turn dispatched
    settled = [op for op in mgr.operations.values() if op.get("role") == "architecture"]
    assert len(settled) == 1

    sup.advance(state)
    sup.advance(state)
    after = [op for op in mgr.operations.values() if op.get("role") == "architecture"]
    # An unchanged graph never re-dispatches the recovery turn.
    assert len(after) == len(settled)
    assert state.recovery_attempts.get("WO-001", 0) <= 1


# ── 6. Restart and persistence ──────────────────────────────────────────


def test_restart_preserves_dependency_wait_metadata(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)

    snapshot = state.dependency_waits["WO-002"]
    fingerprint = state.dependency_graph_fingerprint
    restored = RunState.from_dict(state.to_dict())

    assert restored.dependency_waits["WO-002"]["blocking_dependency_ids"] == ["WO-001"]
    assert restored.dependency_waits["WO-002"]["status"] == "WAITING"
    assert restored.dependency_graph_fingerprint == fingerprint
    assert snapshot["work_order_revision"] == 1


def test_restart_reevaluates_and_does_not_trust_cached_satisfaction(
    tmp_path: Path,
) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    state.completed_wo_ids.append("WO-001")  # stale in-memory claim, disk says ACTIVE
    restored = RunState.from_dict(state.to_dict())

    assert sup._dependency_result(restored, "WO-002", tmp_path).status is DependencyStatus.WAITING


def test_restart_preserves_invalid_dependency_blockers(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-999"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path)
    sup.advance(state)

    restored = RunState.from_dict(state.to_dict())

    blocker = restored.worker_blockers["WO-001"]
    assert blocker["failure_code"] == "INVALID_DEPENDENCY"
    assert blocker["missing_dependencies"] == ["WO-999"]
    assert restored.dependency_waits["WO-001"]["status"] == "INVALID"
    assert "WO-001" in restored.blocked_wo_ids


def test_restart_then_upstream_completion_enables_dispatch(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)
    restored = RunState.from_dict(state.to_dict())

    _complete(tmp_path, "WO-001")
    restored.completed_wo_ids.append("WO-001")

    mgr2 = RecordingManager(tmp_path)
    sup2 = LifecycleSupervisor(mgr2)
    assert sup2.advance(restored) == AdvanceResult.WAITING_FOR_HUMAN  # fail-closed restart
    sup2.re_attest_run(restored, human_signature="operator", confirmed_budget=25)
    sup2.advance(restored)

    assert "WO-002" in mgr2.dispatched


def test_restart_then_upstream_reopen_invalidates_downstream(tmp_path: Path) -> None:
    _chain(tmp_path)
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001",))
    restored = RunState.from_dict(state.to_dict())

    completed_copy = _wo_dir(tmp_path, "COMPLETED") / "WO-001.yaml"
    if completed_copy.is_file():
        completed_copy.unlink()
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="ACTIVE",
        status="ACTIVE",
        extra={"reopened_from": {"revision": 1, "reason": "defect"}},
    )
    # Freshness is recomputed transitively after restart: the reopened root
    # invalidates A → B → C → D without any cached satisfaction being trusted.
    result = sup._dependency_result(restored, "WO-004", tmp_path)
    assert result.status is DependencyStatus.WAITING
    assert "WO-001" in result.prerequisites[0].detail


# ── 7. Integration with SDP-FIX-008A ────────────────────────────────────


def test_008a_dispatch_promotion_only_for_satisfied_dependencies(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))

    sup.advance(state)
    assert sup._dispatch_block_reason(state, "WO-002") is None
    assert "WO-002" not in mgr.dispatched  # dependency, not dispatch, holds it

    op1 = next(op for op in mgr.operations.values() if op.get("work_order_id") == "WO-001")
    mgr.complete_operation(op1["operation_id"], "COMPLETED")
    _complete(tmp_path, "WO-001")
    state.completed_wo_ids.append("WO-001")
    sup.advance(state)
    assert "WO-002" in mgr.dispatched


def test_dependency_regression_does_not_move_into_dispatch_failures(
    tmp_path: Path,
) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)
    assert state.dispatch_failures == {}
    assert state.max_dispatch_attempts == 3


def test_dependency_state_survives_state_roundtrip_with_blocker_fields(
    tmp_path: Path,
) -> None:
    _write_wo(tmp_path, "WO-001", deps=[])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)

    wait = state.dependency_waits["WO-002"]
    for required in (
        "work_order_id",
        "blocking_dependency_ids",
        "dependency_states",
        "dependency_revisions",
        "expected_resolution_actions",
        "resolution_owner",
        "last_state_change_at",
        "graph_fingerprint",
    ):
        assert required in wait
