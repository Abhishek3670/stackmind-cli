"""SDP-FIX-008B-HF1 — completion evidence integrity, transitive validation,
and dependency-wait liveness.

All tests are deterministic and drive the real evaluator / supervisor against
isolated temporary workspaces.  Nothing here mocks dependency evaluation.
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
)
from validators.kernel.daemon.supervisor.release import (
    archive_completed_work_orders,
    completion_record_for,
)
from validators.kernel.work_order_ops import mark_dependents_stale, sha256_file

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
    completion: Any = _OMIT,
    extra: dict[str, Any] | None = None,
    revision: Any = 1,
) -> Path:
    data: dict[str, Any] = {
        "id": wo_id,
        "title": f"Task {wo_id}",
        "status": status or ("COMPLETED" if bucket == "COMPLETED" else "ACTIVE"),
        "assigned_agents": ["codex"],
    }
    if revision is not _OMIT:
        data["revision"] = revision
    if deps is not _OMIT:
        data["dependencies"] = list(deps) if isinstance(deps, (list, tuple)) else deps
    if deliverable:
        data["deliverable"] = {"type": "code", "path": deliverable}
    if completion is not _OMIT:
        data["completion"] = completion
    if extra:
        data.update(extra)
    if bucket == "COMPLETED":
        # One authoritative record per work order: a completed archive must not
        # be shadowed by a leftover ACTIVE/BLOCKED copy.
        for other in ("ACTIVE", "BLOCKED"):
            stale = root / ".sync" / "work-orders" / other / f"{wo_id}.yaml"
            if stale.is_file():
                stale.unlink()
    return _write_raw(root, wo_id, yaml.safe_dump(data, sort_keys=False), bucket=bucket)


def _write_file(root: Path, rel: str, text: str = "content\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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


def _completion(
    root: Path,
    wo_id: str,
    *,
    revision: int = 1,
    contract_revision: int = 0,
    deliverable: str | None = None,
    hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    if hashes is None:
        hashes = {}
        if deliverable:
            digest = sha256_file(root / deliverable)
            if digest:
                hashes[deliverable] = digest
    return {
        "work_order_id": wo_id,
        "verified_at_revision": revision,
        "verified_at_contract_revision": contract_revision,
        "deliverable_hashes": hashes,
    }


def _complete(
    root: Path,
    wo_id: str,
    *,
    deliverable: str | None = None,
    deps: Any = (),
    revision: int = 1,
    contract_revision: int = 0,
) -> None:
    _write_wo(
        root,
        wo_id,
        bucket="COMPLETED",
        status="COMPLETED",
        deps=deps,
        deliverable=deliverable,
        revision=revision,
        completion=_completion(
            root,
            wo_id,
            revision=revision,
            contract_revision=contract_revision,
            deliverable=deliverable,
        ),
    )


def _state(
    sup: LifecycleSupervisor,
    ws: Path,
    *,
    wos: tuple[str, ...],
    phase: Phase = Phase.DISPATCHING,
    **overrides: Any,
) -> RunState:
    state = sup.start_run("run-hf1", "Goal", ws, "sess-hf1")
    state.phase = phase
    state.worker_wo_ids = list(wos)
    state.published_wo_ids = list(wos)
    state.global_turn_budget = 25
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class RecordingManager(MockSessionManager):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.dispatched: list[Any] = []

    def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        record = super().start_turn(session_id, prompt, **kwargs)
        self.dispatched.append(kwargs.get("work_order_id"))
        return record


def _dep_status(root: Path, wo_id: str, **kwargs: Any) -> DependencyStatus:
    return evaluate_dependencies(root, wo_id, **kwargs).status


# ════════════════════════════════════════════════════════════════════════
# A. Completion authority and evidence integrity
# ════════════════════════════════════════════════════════════════════════


def test_a01_completed_without_completion_record_is_not_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(tmp_path, "WO-001", bucket="COMPLETED", status="COMPLETED", deps=[])
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is not DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "completion" in result.prerequisites[0].detail


def test_a02_completed_with_empty_completion_record_is_not_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path, "WO-001", bucket="COMPLETED", status="COMPLETED", deps=[], completion={}
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_a03_completion_record_for_other_work_order_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(tmp_path, "WO-999", deliverable="src/a.py"),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_a04_missing_work_order_revision_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        revision=_OMIT,
        completion=_completion(tmp_path, "WO-001", deliverable="src/a.py"),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "revision" in result.prerequisites[0].detail


def test_a05_work_order_revision_mismatch_is_detected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        revision=2,
        completion=_completion(tmp_path, "WO-001", revision=1, deliverable="src/a.py"),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_a06_contract_revision_mismatch_is_detected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_contract(tmp_path, "WO-001", 2)
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(
            tmp_path, "WO-001", contract_revision=1, deliverable="src/a.py"
        ),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert "contract" in result.prerequisites[0].detail


def test_a07_missing_verified_at_revision_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    record = _completion(tmp_path, "WO-001", deliverable="src/a.py")
    record.pop("verified_at_revision")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=record,
    )
    assert _dep_status(tmp_path, "WO-002") is not DependencyStatus.SATISFIED


def test_a08_missing_deliverable_hash_map_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    record = _completion(tmp_path, "WO-001", deliverable="src/a.py")
    record.pop("deliverable_hashes")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=record,
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert "deliverable_hashes" in result.prerequisites[0].detail


def test_a09_empty_hash_map_rejected_when_deliverable_declared(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(tmp_path, "WO-001", deliverable="src/a.py", hashes={}),
    )
    assert _dep_status(tmp_path, "WO-002") is not DependencyStatus.SATISFIED


def test_a10_missing_required_deliverable_hash_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_file(tmp_path, "src/b.py")
    hashes = {"src/b.py": sha256_file(tmp_path / "src/b.py") or ""}
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(tmp_path, "WO-001", deliverable="src/a.py", hashes=hashes),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert "src/a.py" in result.prerequisites[0].detail


def test_a11_malformed_hash_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(
            tmp_path, "WO-001", deliverable="src/a.py", hashes={"src/a.py": "not-a-hash"}
        ),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert "malformed" in result.prerequisites[0].detail


def test_a12_artifact_hash_mismatch_is_detected(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py", "original\n")
    recorded = sha256_file(tmp_path / "src/a.py") or ""
    _write_file(tmp_path, "src/a.py", "tampered\n")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(
            tmp_path, "WO-001", deliverable="src/a.py", hashes={"src/a.py": recorded}
        ),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_a13_missing_deliverable_file_invalidates_completion(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    recorded = sha256_file(tmp_path / "src/a.py") or ""
    (tmp_path / "src/a.py").unlink()
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(
            tmp_path, "WO-001", deliverable="src/a.py", hashes={"src/a.py": recorded}
        ),
    )
    assert _dep_status(tmp_path, "WO-002") is not DependencyStatus.SATISFIED


def test_a14_hash_entry_outside_workspace_is_rejected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        completion=_completion(
            tmp_path,
            "WO-001",
            hashes={"../outside.txt": "0" * 64},
        ),
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_a15_superseded_completion_cannot_satisfy_dependency(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(tmp_path, "WO-001", deliverable="src/a.py"),
        extra={"superseded_by": "WO-003"},
    )
    result = evaluate_dependencies(tmp_path, "WO-002", superseded_ids={"WO-001"})
    assert result.status is DependencyStatus.INVALID
    assert result.prerequisites[0].state is PrerequisiteState.SUPERSEDED


def test_a16_reopened_prerequisite_cannot_satisfy_dependency(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="ACTIVE",
        status="ACTIVE",
        deps=[],
        deliverable="src/a.py",
        completion=_completion(tmp_path, "WO-001", deliverable="src/a.py"),
        extra={"reopened_from": {"revision": 1}},
    )
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.REOPENED


def test_a17_valid_completion_record_still_satisfies(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _complete(tmp_path, "WO-001", deliverable="src/a.py")
    result = evaluate_dependencies(tmp_path, "WO-002")
    assert result.status is DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_VALID


def test_a18_archive_boundary_writes_authoritative_completion_record(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py", "archived\n")
    _write_wo(tmp_path, "WO-001", deps=[], deliverable="src/a.py", revision=3)
    archived = archive_completed_work_orders(tmp_path, ["WO-001"])
    assert archived == ["WO-001"]
    data = yaml.safe_load(
        (tmp_path / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml").read_text(
            encoding="utf-8"
        )
    )
    completion = data["completion"]
    assert completion["work_order_id"] == "WO-001"
    assert completion["verified_at_revision"] == 3
    assert completion["deliverable_hashes"]["src/a.py"] == sha256_file(
        tmp_path / "src/a.py"
    )
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    assert _dep_status(tmp_path, "WO-002") is DependencyStatus.SATISFIED


def test_a19_completion_record_binds_current_contract_revision(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_contract(tmp_path, "WO-001", 4)
    record = completion_record_for(
        tmp_path,
        "WO-001",
        {"id": "WO-001", "revision": 1, "deliverable": {"path": "src/a.py"}},
        "2026-01-01T00:00:00Z",
    )
    assert record["verified_at_contract_revision"] == 4
    assert record["deliverable_hashes"]["src/a.py"] == sha256_file(tmp_path / "src/a.py")


def test_a20_archive_never_hash_records_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    _write_wo(
        tmp_path,
        "WO-001",
        deps=[],
        deliverable="../outside-secret.txt",
    )
    record = completion_record_for(
        tmp_path,
        "WO-001",
        {"id": "WO-001", "revision": 1, "deliverable": {"path": "../outside-secret.txt"}},
        "2026-01-01T00:00:00Z",
    )
    assert record["deliverable_hashes"] == {}


# ════════════════════════════════════════════════════════════════════════
# B. Strict transitive upstream validation
# ════════════════════════════════════════════════════════════════════════


def test_b01_transitive_missing_dependency_metadata_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=_OMIT,
        completion=_completion(tmp_path, "WO-001"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.status is DependencyStatus.WAITING
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "DEPENDENCY_METADATA_MISSING" in result.prerequisites[0].detail


def test_b02_transitive_null_dependency_metadata_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=None,
        completion=_completion(tmp_path, "WO-001"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "DEPENDENCY_METADATA_MISSING" in result.prerequisites[0].detail


def test_b03_transitive_scalar_dependency_declaration_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps="WO-009",
        completion=_completion(tmp_path, "WO-001"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "DEPENDENCY_METADATA_INVALID" in result.prerequisites[0].detail


def test_b04_transitive_malformed_yaml_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_raw(
        tmp_path,
        "WO-001",
        "id: WO-001\nrevision: 1\nstatus: COMPLETED\ndependencies: [unclosed\n",
        bucket="COMPLETED",
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "DEPENDENCY_METADATA_UNREADABLE" in result.prerequisites[0].detail


def test_b05_transitive_malformed_dependency_entry_is_invalid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[{"id": "WO-009"}],
        completion=_completion(tmp_path, "WO-001"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "WO-001" in result.prerequisites[0].detail


def test_b06_transitive_missing_dependency_is_detected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-404"])
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "WO-404" in result.prerequisites[0].detail


def test_b07_transitive_self_dependency_is_detected(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=["WO-001"],
        completion=_completion(tmp_path, "WO-001"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "SELF_DEPENDENCY" in result.prerequisites[0].detail


def test_b08_transitive_cycle_is_detected_with_path(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"])
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _complete(tmp_path, "WO-003", deps=["WO-001"])
    direct = evaluate_dependencies(tmp_path, "WO-003")
    assert direct.status is DependencyStatus.INVALID
    cycle = next(i for i in direct.graph_issues if i.code == "DEPENDENCY_CYCLE")
    assert {"WO-001", "WO-002"}.issubset(set(cycle.path))
    assert " -> " in direct.reason_text


def test_b09_transitive_superseded_dependency_is_not_satisfied(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        extra={"superseded_by": "WO-010"},
    )
    result = evaluate_dependencies(
        tmp_path, "WO-003", superseded_ids={"WO-001"}
    )
    assert result.status is not DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_b10_explicit_empty_dependency_lists_are_valid(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-001"])
    _complete(tmp_path, "WO-001", deps=[])
    assert _dep_status(tmp_path, "WO-002") is DependencyStatus.SATISFIED


def test_b11_valid_multi_level_chain_is_satisfied(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _write_file(tmp_path, "src/b.py")
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    _complete(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.status is DependencyStatus.SATISFIED


def test_b12_cross_plan_dependency_restriction_stays_enforced(tmp_path: Path) -> None:
    _complete(tmp_path, "WO-001", deps=[])
    _write_wo(
        tmp_path, "WO-001", bucket="COMPLETED", status="COMPLETED", deps=[],
        extra={"plan_id": "PLAN-2"}, completion=_completion(tmp_path, "WO-001"),
    )
    _write_wo(tmp_path, "WO-003", deps=["WO-001"], extra={"plan_id": "PLAN-1"})
    result = evaluate_dependencies(tmp_path, "WO-003", plan_id="PLAN-1")
    assert result.status is DependencyStatus.INVALID
    assert any(i.code == "CROSS_PLAN_DEPENDENCY" for i in result.graph_issues)


def test_b13_transitive_revision_change_invalidates_dependent(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py", revision=1)
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    assert _dep_status(tmp_path, "WO-003") is DependencyStatus.SATISFIED
    # WO-001 is amended: same completion record, higher revision.
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        revision=2,
        completion=_completion(tmp_path, "WO-001", revision=1, deliverable="src/a.py"),
    )
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.status is not DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_b14_transitive_artifact_change_invalidates_dependent(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py", "original\n")
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    assert _dep_status(tmp_path, "WO-003") is DependencyStatus.SATISFIED
    _write_file(tmp_path, "src/a.py", "rewritten\n")
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.status is not DependencyStatus.SATISFIED
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE


def test_b15_dependency_declaration_parser_is_canonical_for_upstream(
    tmp_path: Path,
) -> None:
    _write_wo(tmp_path, "WO-003", deps=["WO-002"])
    _complete(tmp_path, "WO-002", deps=["WO-001"])
    _write_wo(tmp_path, "WO-001", deps=["WO-001"])
    declaration = read_dependency_declaration(tmp_path, "WO-001")
    result = evaluate_dependencies(tmp_path, "WO-003")
    assert result.prerequisites[0].state is PrerequisiteState.COMPLETED_STALE
    assert "SELF_DEPENDENCY" in result.prerequisites[0].detail or declaration.valid


# ════════════════════════════════════════════════════════════════════════
# C. Dependency-wait lifecycle and dispatch integration
# ════════════════════════════════════════════════════════════════════════


def _dispatch_fixture(tmp_path: Path, *, prereq_status: str = "ACTIVE") -> tuple[Any, Any]:
    _write_file(tmp_path, "src/a.py")
    _write_file(tmp_path, "src/b.py")
    if prereq_status == "COMPLETED":
        _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    elif prereq_status == "FAILED":
        _write_wo(
            tmp_path, "WO-001", deps=[], deliverable="src/a.py", status="FAILED"
        )
    else:
        _write_wo(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    return sup, state, mgr


def test_c01_valid_dependency_wait_is_recognized_by_dispatch(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    result = sup.advance(state)
    assert result != AdvanceResult.BLOCKED
    assert state.phase is Phase.EXECUTING
    assert state.dependency_waits.get("WO-002")


def test_c02_dependency_wait_does_not_dispatch_dependent(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-002" not in mgr.dispatched


def test_c03_upstream_work_order_awaiting_dispatch_stays_schedulable(
    tmp_path: Path,
) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-001" in mgr.dispatched


def test_c04_valid_wait_is_not_reported_as_no_executable_work(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    result = sup.advance(state)
    assert not (state.error or "").startswith("DISPATCH_NO_EXECUTABLE_WORK")
    assert result == AdvanceResult.WAITING_FOR_OPERATION


def test_c05_dependency_wait_consumes_no_dispatch_retry_budget(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert state.dispatch_failures == {}
    assert state.retry_counts.get("WO-002", 0) == 0


def test_c06_dependency_wait_consumes_no_recovery_retry_budget(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert state.recovery_attempts.get("WO-002", 0) == 0


def test_c07_dependency_wait_consumes_no_qa_rework_budget(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert state.qa_reworks.get("WO-002", 0) == 0


def test_c08_wait_record_carries_resolution_metadata(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    record = state.dependency_waits["WO-002"]
    assert record["work_order_id"] == "WO-002"
    assert record["blocking_dependency_ids"] == ["WO-001"]
    assert record["resolution_owner"] == "worker"
    assert record["expected_resolution_actions"]
    assert record["graph_fingerprint"] == state.dependency_graph_fingerprint


def test_c09_dependent_dispatched_after_prerequisite_completion(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-002" not in mgr.dispatched
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    state.phase = Phase.DISPATCHING
    sup.advance(state)
    assert "WO-002" in mgr.dispatched


def test_c10_resolved_wait_is_removed_when_dependencies_satisfied(
    tmp_path: Path,
) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-002" in state.dependency_waits
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    sup.advance(state)
    assert "WO-002" not in state.dependency_waits


def test_c11_invalid_prerequisite_becomes_explicit_blocker(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-404"])
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-002",))
    sup.advance(state)
    blocker = state.worker_blockers.get("WO-002")
    assert blocker is not None
    assert blocker["failure_code"] == "INVALID_DEPENDENCY"
    assert "WO-404" in blocker["missing_dependencies"]


def test_c12_failed_prerequisite_wait_is_not_kept_indefinitely(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-002" in state.dependency_waits
    _write_wo(tmp_path, "WO-001", deps=[], deliverable="src/a.py", status="FAILED")
    sup.advance(state)
    assert "WO-002" not in state.dependency_waits
    assert "WO-002" in state.blocked_wo_ids or "WO-002" in state.worker_blockers


def test_c13_superseded_prerequisite_invalidates_wait(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    state.superseded_wo_ids = ["WO-001"]
    sup.advance(state)
    assert "WO-002" not in state.dependency_waits


def test_c14_graph_fingerprint_change_refreshes_wait_metadata(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    stale_record = dict(state.dependency_waits["WO-002"])
    stale_record["graph_fingerprint"] = "stale-fingerprint"
    state.dependency_waits["WO-002"] = stale_record
    sup.advance(state)
    refreshed = state.dependency_waits.get("WO-002")
    assert refreshed is not None
    assert refreshed["graph_fingerprint"] != "stale-fingerprint"


def test_c15_upstream_revision_change_triggers_reevaluation(tmp_path: Path) -> None:
    _write_file(tmp_path, "src/a.py")
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py", revision=1)
    _write_wo(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    state.dependency_waits["WO-002"] = {
        "work_order_id": "WO-002",
        "blocking_dependency_ids": ["WO-001"],
        "graph_fingerprint": "",
        "resolution_owner": "worker",
    }
    _write_wo(
        tmp_path,
        "WO-001",
        bucket="COMPLETED",
        status="COMPLETED",
        deps=[],
        deliverable="src/a.py",
        revision=2,
        completion=_completion(tmp_path, "WO-001", revision=1, deliverable="src/a.py"),
    )
    sup.advance(state)
    assert "WO-002" not in mgr.dispatched


def test_c16_restart_reconstructs_valid_dependency_wait(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    sup.save_run_state(state, tmp_path)
    restored = LifecycleSupervisor(RecordingManager(tmp_path)).load_run_state(
        "run-hf1", tmp_path
    )
    assert restored is not None
    assert "WO-002" in restored.dependency_waits
    assert restored.dependency_waits["WO-002"]["blocking_dependency_ids"] == ["WO-001"]


def test_c17_restart_reconstructs_resolved_wait(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    sup.save_run_state(state, tmp_path)
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    mgr2 = RecordingManager(tmp_path)
    sup2 = LifecycleSupervisor(mgr2)
    restored = sup2.load_run_state("run-hf1", tmp_path)
    assert restored is not None
    sup2.state_manager.re_attest_run(restored.run_id, "hf1 restart test")
    sup2.advance(restored)
    assert "WO-002" not in restored.dependency_waits


def test_c18_restart_detects_orphaned_dependency_wait(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-002", deps=["WO-404"], deliverable="src/b.py")
    _write_file(tmp_path, "src/b.py")
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-002",))
    state.dependency_waits["WO-002"] = {
        "work_order_id": "WO-002",
        "blocking_dependency_ids": ["WO-404"],
        "graph_fingerprint": "",
        "resolution_owner": "architect",
    }
    sup.save_run_state(state, tmp_path)
    sup2 = LifecycleSupervisor(RecordingManager(tmp_path))
    restored = sup2.load_run_state("run-hf1", tmp_path)
    assert restored is not None
    sup2.state_manager.re_attest_run(restored.run_id, "hf1 restart test")
    sup2.advance(restored)
    blocker = restored.worker_blockers.get("WO-002")
    assert blocker is not None
    assert blocker["failure_code"] == "INVALID_DEPENDENCY"
    assert "WO-404" in blocker["missing_dependencies"]


def test_c19_multi_level_chain_dispatches_upstream_first(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    _write_wo(tmp_path, "WO-003", deps=["WO-002"], deliverable="src/c.py")
    _write_file(tmp_path, "src/a.py")
    _write_file(tmp_path, "src/b.py")
    _write_file(tmp_path, "src/c.py")
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002", "WO-003"))
    sup.advance(state)
    assert mgr.dispatched == ["WO-001"]
    _complete(tmp_path, "WO-001", deps=[], deliverable="src/a.py")
    state.phase = Phase.DISPATCHING
    sup.advance(state)
    assert mgr.dispatched == ["WO-001", "WO-002"]
    _complete(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    state.phase = Phase.DISPATCHING
    sup.advance(state)
    assert mgr.dispatched == ["WO-001", "WO-002", "WO-003"]


def test_c20_cyclic_graph_is_rejected_before_scheduling(tmp_path: Path) -> None:
    _write_wo(tmp_path, "WO-001", deps=["WO-002"], deliverable="src/a.py")
    _write_wo(tmp_path, "WO-002", deps=["WO-001"], deliverable="src/b.py")
    mgr = RecordingManager(tmp_path)
    sup = LifecycleSupervisor(mgr)
    state = _state(sup, tmp_path, wos=("WO-001", "WO-002"))
    sup.advance(state)
    assert mgr.dispatched == []
    assert any(
        blocker.get("dependency_code") == "DEPENDENCY_CYCLE"
        or blocker.get("failure_code") == "INVALID_DEPENDENCY"
        for blocker in state.worker_blockers.values()
    )


def test_c21_invalidated_wait_produces_deterministic_blocker_code(
    tmp_path: Path,
) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    assert "WO-002" in state.dependency_waits
    # The prerequisite disappears entirely: no resolution path remains.
    (tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").unlink()
    state.worker_wo_ids = ["WO-002"]
    sup.advance(state)
    assert "WO-002" not in state.dependency_waits
    assert "WO-002" in state.worker_blockers or "WO-002" in state.blocked_wo_ids


def test_c22_dependency_waits_do_not_mutate_completed_history(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    sup.advance(state)
    snapshot = list(state.completed_wo_ids)
    sup.advance(state)
    assert state.completed_wo_ids == snapshot


def test_c23_waiting_dependent_is_not_redispatched_across_ticks(tmp_path: Path) -> None:
    sup, state, mgr = _dispatch_fixture(tmp_path)
    for _ in range(3):
        sup.advance(state)
        state.phase = Phase.DISPATCHING
    assert mgr.dispatched.count("WO-002") == 0
