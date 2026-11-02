"""Tests for cancellation end-state cleanup semantics (Checklist Item C.4).

Verifies that cancelling an operation or session:
1. Deterministically transitions on-disk work orders to CANCELLED.
2. Syncs INDEX.yaml and records audit log entries.
3. Cleans up uncommitted staged Git index changes without leaving the workspace dirty.
4. Integrates cleanly with SessionManager.cancel_operation and cancel_session.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.work_order_ops import (
    cancel_work_order,
    cleanup_cancelled_operation,
    load_work_order,
)


def _init_git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "cancel_project"
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "StackMind Test"],
        cwd=str(ws),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@stackmind.dev"],
        cwd=str(ws),
        check=True,
        capture_output=True,
    )
    (ws / "README.md").write_text("# Cancel Project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=str(ws),
        check=True,
        capture_output=True,
    )
    return ws


def _setup_work_order(ws: Path, wo_id: str = "WO-001") -> Path:
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_path = wo_dir / f"{wo_id}.yaml"
    wo_path.write_text(
        yaml.safe_dump({
            "id": wo_id,
            "type": "FEATURE",
            "title": f"Feature {wo_id}",
            "status": "ACTIVE",
            "revision": 1,
        }),
        encoding="utf-8",
    )
    index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
    index_path.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "orders": [{"id": wo_id, "title": f"Feature {wo_id}", "status": "ACTIVE"}],
            "next_id": 2,
        }),
        encoding="utf-8",
    )
    return wo_path


def test_cancel_work_order_transitions_status_and_updates_index(tmp_path: Path):
    """cancel_work_order must update work order status, INDEX.yaml, and write audit entry."""
    ws = _init_git_workspace(tmp_path)
    wo_path = _setup_work_order(ws, "WO-001")

    cancelled_path = cancel_work_order(
        ws,
        "WO-001",
        reason="Operator paused delivery",
        actor="operator",
    )
    assert cancelled_path is not None
    assert cancelled_path == wo_path

    ref = load_work_order(ws, "WO-001")
    assert ref is not None
    assert ref.data["status"] == "CANCELLED"
    assert ref.data["cancellation_reason"] == "Operator paused delivery"

    # Verify INDEX.yaml updated
    index_data = yaml.safe_load((ws / ".sync" / "work-orders" / "INDEX.yaml").read_text(encoding="utf-8"))
    assert index_data["orders"][0]["status"] == "CANCELLED"

    # Verify audit log entry
    audit_file = ws / ".sync" / "reports" / "work_order_amendments.jsonl"
    assert audit_file.is_file()
    assert "cancel_work_order" in audit_file.read_text(encoding="utf-8")


def test_cleanup_cancelled_operation_unstages_git_index(tmp_path: Path):
    """cleanup_cancelled_operation must unstage git changes without destroying working tree files."""
    ws = _init_git_workspace(tmp_path)
    _setup_work_order(ws, "WO-002")

    # Simulate in-flight staged changes
    staged_file = ws / "scratch_work.py"
    staged_file.write_text("x = 42\n", encoding="utf-8")
    subprocess.run(["git", "add", "scratch_work.py"], cwd=str(ws), check=True)

    # Verify staged before cleanup
    diff_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(ws),
        capture_output=True,
        text=True,
    )
    assert "scratch_work.py" in diff_before.stdout

    # Run cleanup
    cleanup_res = cleanup_cancelled_operation(
        ws,
        operation_id="op-test-123",
        work_order_id="WO-002",
        reason="Aborted by user",
    )
    assert cleanup_res["git_reset"] is True
    assert cleanup_res["cancelled_wo_path"] is not None

    # Verify index is clean
    diff_after = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(ws),
    )
    assert diff_after.returncode == 0  # Clean index

    # Working tree file is preserved
    assert staged_file.is_file()


def test_session_manager_cancel_operation_triggers_cleanup(tmp_path: Path):
    """SessionManager.cancel_operation must trigger cancellation cleanup for associated work orders."""
    ws = _init_git_workspace(tmp_path)
    _setup_work_order(ws, "WO-003")

    # Stage a file
    (ws / "partial.txt").write_text("partial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "partial.txt"], cwd=str(ws), check=True)

    storage = DaemonStorage(ws / ".sync" / "runtime" / "daemon" / "state.json")
    manager = SessionManager(storage)
    session = manager.create_session(
        agent="codex",
        provider="mock",
        contract={"scope": {"allow": ["src/**"]}},
        workspace=str(ws),
    )
    cancel_ev, op_id = manager.begin_operation(
        session["session_id"],
        "worker_turn",
        work_order_id="WO-003",
        role="backend",
    )

    # Cancel the operation
    cancelled_rec = manager.cancel_operation(op_id)
    assert cancelled_rec["status"] == "CANCELLED"

    # Verify on-disk work order is CANCELLED
    ref = load_work_order(ws, "WO-003")
    assert ref is not None
    assert ref.data["status"] == "CANCELLED"

    # Verify git index was cleaned
    diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(ws))
    assert diff_check.returncode == 0
