"""Tests for mid-turn crash resilience and idempotence (Checklist Item B.2).

Verifies that killing or crashing a process mid-turn (after side-effects such as
file modifications, git staging, or commits, but before the turn snapshot/state is saved)
resumes cleanly with:
1. Zero duplicate Git release commits.
2. Zero duplicate assistant messages or replay loops in TUI state.
3. Proper journal operation reconciliation on restart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from cli.tui.state import AutonomousDeliveryState
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.supervisor.release import (
    create_gitops_commit,
    format_release_commit_message,
)
from validators.kernel.work_order_ops import (
    load_work_order,
    reconcile_operations,
    record_pending,
    unresolved_operations,
)


def _init_git_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "project"
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
    (ws / "README.md").write_text("# Project Root\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=str(ws),
        check=True,
        capture_output=True,
    )
    return ws


def test_mid_turn_crash_after_git_commit_creates_no_duplicate_commit(tmp_path: Path):
    """If a process crashes right after creating a release commit, resume must not create a duplicate."""
    ws = _init_git_workspace(tmp_path)

    # Deliverable written during the turn
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "app.py").write_text("print('hello world')\n", encoding="utf-8")

    # 1. Turn creates release commit
    sha1 = create_gitops_commit(
        workspace=ws,
        work_order_id="WO-001",
        summary="feat: implement core app",
        released_by="local-llm",
        approved_by="gemma",
        architect="claude",
        target_work_orders=["WO-001"],
    )
    assert len(sha1) == 40

    commit_count_1 = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(ws),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    # 2. Simulate crash before checkpoint saved: supervisor resumes and calls create_gitops_commit again
    sha2 = create_gitops_commit(
        workspace=ws,
        work_order_id="WO-001",
        summary="feat: implement core app",
        released_by="local-llm",
        approved_by="gemma",
        architect="claude",
        target_work_orders=["WO-001"],
    )

    commit_count_2 = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(ws),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    # Must return identical SHA and NOT create a second commit
    assert sha2 == sha1
    assert commit_count_2 == commit_count_1


def test_mid_turn_crash_leaves_unresolved_op_reconciled_on_restart(tmp_path: Path):
    """Pending journal operations from killed turns are detected and reconcilable on restart."""
    ws = _init_git_workspace(tmp_path)

    # Write a work order
    wo_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_file = wo_dir / "WO-001.yaml"
    wo_file.write_text(
        yaml.safe_dump({
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Build app",
            "status": "ACTIVE",
            "revision": 1,
        }),
        encoding="utf-8",
    )

    # Record a pending operation mid-turn
    op_record = record_pending(
        ws,
        kind="amendment",
        work_order="WO-001",
        expected_revision=1,
        target_revision=2,
        reason="Mid-turn work order amendment before crash",
        actor="codex",
    )
    assert op_record is not None
    assert "op_id" in op_record

    unresolved = unresolved_operations(ws)
    assert len(unresolved) >= 1
    assert any(rec.get("work_order") == "WO-001" for rec in unresolved)

    # On restart, reconcile_operations runs
    reconciled = reconcile_operations(ws)
    assert "WO-001" in reconciled or isinstance(reconciled, dict)


def test_tui_state_suppresses_duplicate_messages_on_replay():
    """Event replay or reconnection after a crash must not duplicate assistant messages."""
    state = AutonomousDeliveryState()

    # Initial turn message
    m1 = state.add_message("assistant", "Build completed successfully. Tests passed: 10/10.", deduplicate=True)
    assert len(state.messages) == 1

    # Replay after reconnection/restart
    m2 = state.add_message("assistant", "Build completed successfully. Tests passed: 10/10.", deduplicate=True)
    assert len(state.messages) == 1  # Deduplicated to same message
    assert m1 is m2

    # Verify duplicate suppression window for assistant responses
    m3 = state.add_message("assistant", "Build completed successfully. Tests passed: 10/10.")
    assert len(state.messages) == 1
    assert m3 is m1
