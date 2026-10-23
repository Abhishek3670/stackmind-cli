"""SDP-FIX-004 - recoverable amendment/reopen audit persistence.

The protocol under test is write-ahead: a stable ``op_id`` plus expected/target
revision and payload hashes are durable in ``.sync/runtime/work-order-ops``
before the authoritative work-order write, and the append-only JSONL audit is
written after it. A pending journal record means "either the mutation did not
land, or it landed and the audit did not"; both are reconciled without ever
applying the mutation twice or overwriting a newer revision.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from validators.harness.integration_gate import IntegrationGate
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import RevisionConflictError, ToolGateway
from validators.kernel.workspace import ScratchWorkspace
from validators.kernel.work_order_ops import (
    JOURNAL_REL,
    append_audit,
    load_operations,
    mark_dependents_stale,
    reconcile_operations,
    record_pending,
    sha256_bytes,
)

AUDIT_REL = Path(".sync") / "reports" / "work_order_amendments.jsonl"

GATEWAY_OPERATIONS = (
    "update_work_order",
    "reopen_work_order",
    "revalidate_work_order",
    "write_file",
    "read_file",
    "inspect_agent",
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED"):
        (root / ".sync" / "work-orders" / bucket).mkdir(parents=True)
    return root


def _wo(wo_id: str = "WO-001", path: str = "src/api.py", **extra) -> dict:
    record = {
        "id": wo_id,
        "revision": 1,
        "type": "FEATURE",
        "title": f"Deliver {path}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": path, "description": f"Author {path}"},
        "acceptance_criteria": [f"{path} exists"],
    }
    record.update(extra)
    return record


def _write_wo(root: Path, data: dict, bucket: str = "ACTIVE") -> Path:
    target = root / ".sync" / "work-orders" / bucket / f"{data['id']}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def _wo_path(root: Path, wo_id: str = "WO-001", bucket: str = "ACTIVE") -> Path:
    return root / ".sync" / "work-orders" / bucket / f"{wo_id}.yaml"


def _load(root: Path, wo_id: str = "WO-001", bucket: str = "ACTIVE") -> dict:
    return yaml.safe_load(_wo_path(root, wo_id, bucket).read_text(encoding="utf-8"))


def _gateway(root: Path, actor: str = "claude") -> ToolGateway:
    workspace = ScratchWorkspace.create(root, attempt_id=f"test-{actor}")
    contract = KernelContract(
        agent_id=actor,
        work_order="WO-000",
        allow=("workspace/.sync/work-orders/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    return ToolGateway(
        workspace=workspace,
        boundary=RuntimeBoundary(OperationJournal()),
        contract=contract,
        policy=AuthorizationPolicy.permit(f"test-{actor}", GATEWAY_OPERATIONS),
        session_id="s1",
        attempt_id="a1",
        actor_id=actor,
        provider_id="mock",
    )


def _audit(root: Path) -> list[dict]:
    path = root / AUDIT_REL
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _journal(root: Path) -> list[dict]:
    return load_operations(root)


def _journal_files(root: Path) -> list[Path]:
    return sorted((root / JOURNAL_REL).glob("woop-*.json"))


def _write_upstream_payload(root: Path, data: dict) -> str:
    """Persist a work order and return the exact payload hash reconciliation sees."""
    target = _write_wo(root, data)
    return sha256_bytes(target.read_bytes())


# ─── Before the mutation ────────────────────────────────────────────────


def test_journal_failure_aborts_before_any_mutation(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    before = _wo_path(root).read_bytes()

    import validators.kernel.tools as tools_module

    def _boom(*args, **kwargs):
        raise OSError("journal volume is read-only")

    monkeypatch.setattr(tools_module, "record_pending", _boom)

    with pytest.raises(PermissionError) as excinfo:
        _gateway(root).update_work_order(
            "WO-001", {"title": "never lands"}, reason="fail fast", expected_revision=1,
        )

    assert "no change applied" in str(excinfo.value)
    assert _wo_path(root).read_bytes() == before
    assert _audit(root) == []
    assert _journal_files(root) == []


def test_interrupted_before_mutation_is_abandoned_not_applied(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    previous_sha = sha256_bytes(_wo_path(root).read_bytes())

    pending = record_pending(
        root,
        kind="amend",
        work_order="WO-001",
        expected_revision=1,
        target_revision=2,
        reason="interrupted write",
        actor="claude",
        changed_fields=["title"],
        previous_sha256=previous_sha,
        expected_sha256="f" * 64,
    )
    assert pending["status"] == "pending"

    applied = _gateway(root).update_work_order(
        "WO-001", {"title": "landed once"}, reason="real amendment", expected_revision=1,
    )
    assert applied["revision"] == 2

    outcomes = [entry["outcome"] for entry in _audit(root)]
    assert outcomes.count("applied") == 1
    assert "abandoned" in outcomes

    records = {record["op_id"]: record for record in _journal(root)}
    assert records[pending["op_id"]]["status"] == "abandoned"


def test_failed_work_order_write_after_pending_is_recovered(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    before = _wo_path(root).read_bytes()

    import validators.kernel.io as kernel_io

    real_write = kernel_io.durable_write_yaml

    def _boom(*args, **kwargs):
        raise OSError("disk full after the intent was journaled")

    monkeypatch.setattr(kernel_io, "durable_write_yaml", _boom)

    with pytest.raises(OSError):
        _gateway(root).update_work_order(
            "WO-001", {"title": "never lands"}, reason="write fails", expected_revision=1,
        )

    assert _wo_path(root).read_bytes() == before
    pending = [record for record in _journal(root) if record["status"] == "pending"]
    assert len(pending) == 1

    monkeypatch.setattr(kernel_io, "durable_write_yaml", real_write)
    summary = reconcile_operations(root)
    assert [item["op_id"] for item in summary["abandoned"]] == [pending[0]["op_id"]]
    assert summary["unresolved"] == []
    assert _wo_path(root).read_bytes() == before
    assert any(entry["outcome"] == "abandoned" for entry in _audit(root))

    applied = _gateway(root).update_work_order(
        "WO-001", {"title": "lands now"}, reason="retry", expected_revision=1,
    )
    assert applied["revision"] == 2
    assert _load(root)["title"] == "lands now"


# ─── After the mutation ─────────────────────────────────────────────────


def test_audit_failure_recovers_after_restart(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    _write_wo(root, _wo())

    import validators.kernel.tools as tools_module

    real_append = tools_module.append_audit

    def _fail_applied(root_arg, entry, **kwargs):
        if entry.get("outcome") == "applied":
            return False
        return real_append(root_arg, entry, **kwargs)

    monkeypatch.setattr(tools_module, "append_audit", _fail_applied)

    result = _gateway(root).update_work_order(
        "WO-001", {"title": "landed but unlogged"}, reason="audit will fail",
        expected_revision=1,
    )

    assert result["amendment_audit"] == "pending_recovery"
    assert _load(root)["revision"] == 2
    assert [entry["outcome"] for entry in _audit(root)] == []

    # The mutation is durable but unlogged: exactly one pending record, which is
    # the only evidence that the audit is incomplete.
    pending = [record for record in _journal(root) if record["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["target_revision"] == 2

    # Process restart: an explicit reconciliation pass completes the audit
    # without mutating the work order.
    summary = reconcile_operations(root)
    assert [item["op_id"] for item in summary["recovered"]] == [pending[0]["op_id"]]
    assert summary["unresolved"] == []

    recovered = [entry for entry in _audit(root) if entry["outcome"] == "recovered"]
    assert len(recovered) == 1
    assert recovered[0]["revision"] == 2
    assert recovered[0]["op_id"] == pending[0]["op_id"]
    assert _load(root)["revision"] == 2
    assert all(record["status"] != "pending" for record in _journal(root))

    # The reconciled journal unblocks normal work.
    second = _gateway(root).update_work_order(
        "WO-001", {"title": "second"}, reason="second", expected_revision=2,
    )
    assert second["revision"] == 3

    # A further mutation is not blocked by the recovered record.
    third = _gateway(root).update_work_order(
        "WO-001", {"title": "third"}, reason="third", expected_revision=3,
    )
    assert third["revision"] == 4


def test_reconciliation_is_idempotent(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    record_pending(
        root,
        kind="amend",
        work_order="WO-001",
        expected_revision=1,
        target_revision=1,
        reason="already applied payload",
        actor="claude",
        changed_fields=["title"],
        previous_sha256="a" * 64,
        expected_sha256=sha256_bytes(_wo_path(root).read_bytes()),
    )

    first = reconcile_operations(root)
    assert len(first["recovered"]) == 1
    second = reconcile_operations(root)
    assert second["recovered"] == []
    assert second["abandoned"] == []
    assert second["superseded"] == []
    assert second["unresolved"] == []

    recovered = [entry for entry in _audit(root) if entry["outcome"] == "recovered"]
    assert len(recovered) == 1


def test_newer_revision_is_never_overwritten_by_recovery(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    record_pending(
        root,
        kind="amend",
        work_order="WO-001",
        expected_revision=1,
        target_revision=2,
        reason="stale intended mutation",
        actor="claude",
        changed_fields=["title"],
        previous_sha256=sha256_bytes(_wo_path(root).read_bytes()),
        expected_sha256="b" * 64,
    )

    newer = _wo(revision=5, title="a newer valid mutation")
    _write_wo(root, newer)

    summary = reconcile_operations(root)
    assert len(summary["superseded"]) == 1
    assert _load(root)["revision"] == 5
    assert _load(root)["title"] == "a newer valid mutation"
    assert any(entry["outcome"] == "superseded" for entry in _audit(root))
    assert all(record["status"] != "pending" for record in _journal(root))


def test_partial_dependent_invalidation_is_repaired_or_blocks(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _write_wo(root, _wo("WO-002", path="src/client.py", dependencies=["WO-001"]))

    upstream_payload = _wo(
        revision=2, acceptance_criteria=["src/api.py returns 404"],
    )
    expected_sha = _write_upstream_payload(root, upstream_payload)

    pending = record_pending(
        root,
        kind="amend",
        work_order="WO-001",
        expected_revision=1,
        target_revision=2,
        reason="dependent marking interrupted",
        actor="claude",
        changed_fields=["acceptance_criteria"],
        intended_dependents=["WO-002"],
        previous_sha256="0" * 64,
        expected_sha256=expected_sha,
    )

    # Without a repair callable the partial invalidation is unresolved, not
    # silently accepted as complete.
    first = reconcile_operations(root, apply_stale=None)
    assert first["unresolved"]
    assert "incomplete" in first["unresolved"][0]["reason"]
    assert _load(root, "WO-002").get("evidence_stale") is None

    def _repair(record: dict) -> bool:
        result = mark_dependents_stale(
            root,
            upstream_id=str(record["work_order"]),
            upstream_revision=int(record["target_revision"]),
            reason=str(record["reason"]),
            fields=[str(f) for f in record["changed_fields"]],
            actor="runtime-recovery",
        )
        return not result["failed"]

    second = reconcile_operations(root, apply_stale=_repair)
    assert second["unresolved"] == []
    assert [item["op_id"] for item in second["recovered"]] == [pending["op_id"]]
    stale = _load(root, "WO-002")["evidence_stale"]
    assert stale["sources"][0]["upstream_work_order"] == "WO-001"
    assert stale["sources"][0]["upstream_revision"] == 2


def test_ambiguous_state_blocks_mutation_and_release(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    record_pending(
        root,
        kind="amend",
        work_order="WO-001",
        expected_revision=1,
        target_revision=2,
        reason="torn write",
        actor="claude",
        changed_fields=["title"],
        previous_sha256="d" * 64,
        expected_sha256="e" * 64,
    )

    with pytest.raises(PermissionError) as excinfo:
        _gateway(root).update_work_order(
            "WO-001", {"title": "blocked"}, reason="blocked", expected_revision=1,
        )
    assert "unreconciled" in str(excinfo.value)
    assert _load(root)["revision"] == 1

    _, issues, _, _ = IntegrationGate(root)._check_artifacts_and_freshness(None, None, None)
    assert any("Unreconciled work-order operation" in issue for issue in issues)


# ─── Persistence boundaries ─────────────────────────────────────────────


def test_index_sync_failure_does_not_lose_the_mutation(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    (root / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "orders": [{"id": "WO-001", "revision": 1, "status": "ACTIVE"}],
        }, sort_keys=False),
        encoding="utf-8",
    )

    gateway = _gateway(root)

    def _boom(*args, **kwargs):
        raise OSError("index is locked")

    monkeypatch.setattr(gateway, "_sync_index_entry", _boom)

    result = gateway.update_work_order(
        "WO-001", {"title": "durable despite index"}, reason="index fails",
        expected_revision=1,
    )

    assert result["revision"] == 2
    assert _load(root)["title"] == "durable despite index"
    assert [entry["outcome"] for entry in _audit(root)] == ["applied"]
    assert all(record["status"] == "applied" for record in _journal(root))


def test_reopen_relocation_keeps_audit_and_index_consistent(tmp_path: Path):
    root = _project(tmp_path)
    completed = _wo(revision=3, status="COMPLETED")
    completed["completed_at"] = "2026-02-01T00:00:00Z"
    completed["completion"] = {"verified_at_revision": 3, "summary": "shipped"}
    _write_wo(root, completed, bucket="COMPLETED")
    (root / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "orders": [{
                "id": "WO-001", "revision": 3, "status": "COMPLETED",
                "file": "work-orders/COMPLETED/WO-001.yaml",
            }],
            "total_active": 0,
            "total_completed": 1,
        }, sort_keys=False),
        encoding="utf-8",
    )

    reopened = _gateway(root).reopen_work_order(
        "WO-001", reason="criteria changed", expected_revision=3,
    )

    assert reopened["revision"] == 4
    assert _wo_path(root).is_file()
    assert not _wo_path(root, bucket="COMPLETED").exists()
    entries = _audit(root)
    assert entries[-1]["outcome"] == "reopened"
    assert entries[-1]["previous_record"]["completion"]["summary"] == "shipped"

    index = yaml.safe_load(
        (root / ".sync" / "work-orders" / "INDEX.yaml").read_text(encoding="utf-8")
    )
    assert index["orders"][0]["status"] == "ACTIVE"
    assert index["orders"][0]["revision"] == 4
    assert index["orders"][0]["file"] == "work-orders/ACTIVE/WO-001.yaml"
    assert index["total_active"] == 1
    assert index["total_completed"] == 0

    assert reconcile_operations(root)["unresolved"] == []
    assert all(record["status"] == "applied" for record in _journal(root))


def test_more_than_twenty_amendments_keep_full_durable_history(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())
    gateway = _gateway(root)

    for index in range(30):
        gateway.update_work_order(
            "WO-001",
            {"title": f"title {index}"},
            reason=f"amendment {index}",
            expected_revision=index + 1,
        )

    stored = _load(root)
    assert stored["revision"] == 31
    assert len(stored["amendments"]) == 20

    applied = [entry for entry in _audit(root) if entry["outcome"] == "applied"]
    assert len(applied) == 30
    assert applied[0]["reason"] == "amendment 0"
    assert applied[-1]["reason"] == "amendment 29"
    assert len({entry["op_id"] for entry in applied}) == 30

    records = _journal(root)
    assert all(record["status"] == "applied" for record in records)
    assert all(record.get("recovered") is not True for record in records)
    assert reconcile_operations(root)["unresolved"] == []


# ─── Concurrency ────────────────────────────────────────────────────────


def test_two_amendments_from_one_revision_cannot_both_succeed(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo())

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}
    lock = threading.Lock()

    def _attempt(name: str, actor: str) -> None:
        gateway = _gateway(root, actor=actor)
        barrier.wait(timeout=10)
        try:
            result = gateway.update_work_order(
                "WO-001", {"title": name}, reason=name, expected_revision=1,
            )
            with lock:
                outcomes[name] = f"applied:r{result['revision']}"
        except (RevisionConflictError, PermissionError) as exc:
            with lock:
                outcomes[name] = type(exc).__name__

    threads = [
        threading.Thread(target=_attempt, args=("A", "claude")),
        threading.Thread(target=_attempt, args=("B", "ceo")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    applied = [value for value in outcomes.values() if value.startswith("applied")]
    assert applied == ["applied:r2"]
    assert _load(root)["revision"] == 2
    applied_audit = [entry for entry in _audit(root) if entry["outcome"] == "applied"]
    assert len(applied_audit) == 1
    assert applied_audit[0]["revision"] == 2
    assert all(record["status"] in ("applied", "abandoned") for record in _journal(root))


def test_release_evaluation_during_amendment_never_false_approves(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    completed = _wo("WO-002", path="src/client.py", dependencies=["WO-001"])
    completed.update({
        "revision": 2,
        "status": "COMPLETED",
        "completed_at": "2026-02-01T00:00:00Z",
        "completion": {"verified_at_revision": 2, "summary": "shipped"},
    })
    _write_wo(root, completed, bucket="COMPLETED")
    for rel in ("src/api.py", "src/client.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def handle(payload):\n    return {'ok': True}\n", encoding="utf-8")

    gate = IntegrationGate(root)
    assert gate._check_artifacts_and_freshness(None, None, None)[0].passed is True

    barrier = threading.Barrier(2)
    observations: list[bool] = []
    errors: list[BaseException] = []

    def _evaluate() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                check, _, _, _ = gate._check_artifacts_and_freshness(None, None, None)
                observations.append(check.passed)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _amend() -> None:
        barrier.wait(timeout=10)
        _gateway(root).update_work_order(
            "WO-001", {"acceptance_criteria": ["returns 404"]},
            reason="interface tightened", expected_revision=1,
        )

    threads = [threading.Thread(target=_evaluate), threading.Thread(target=_amend)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert not errors

    # After the amendment the dependent is stale regardless of evaluation order,
    # and no later evaluation may approve release.
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2
    final_check, issues, _, _ = gate._check_artifacts_and_freshness(None, None, None)
    assert final_check.passed is False
    assert any("WO-002" in issue for issue in issues)
