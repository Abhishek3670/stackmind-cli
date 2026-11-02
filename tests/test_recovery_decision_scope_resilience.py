"""Regression test suite for recovery decision turn scope resilience and overwrite conflict handling.

Verifies:
1. Architect recovery decision turns authoring child work orders and contracts
   do not fail `scope_verified` due to worker contract mismatch.
2. Un-declared `.sync/work-orders/INDEX.yaml` modified during child work order
   registration is recognized as bookkeeping and does not fail scope verification.
3. Active work orders undergoing recovery can be amended in-place without
   triggering overwrite conflicts during ARCHITECT_RECOVERY_DECISION.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from validators.harness.authoring_conflict import _check_overwrite_conflict
from validators.harness.runner.models import HarnessDecision, HarnessTask
from validators.harness.runner.verification import _VerificationMixin
from validators.harness.snapshot import WorkspaceDiff, WorkspaceSnapshot


class DummyVerificationRunner(_VerificationMixin):
    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.sync_path = project_path / ".sync"
        self.agent = "claude"
        self._last_verification_evidence = {}

    def _read_yaml(self, p: Path) -> dict[str, Any]:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def test_recovery_decision_architect_scope_not_constrained_by_worker_contract(tmp_path: Path) -> None:
    """Architect executing a recovery decision on a blocked worker task must not be evaluated against worker contract."""
    sync_dir = tmp_path / ".sync"
    contracts_dir = sync_dir / "contracts"
    wos_dir = sync_dir / "work-orders" / "ACTIVE"
    agents_dir = sync_dir / "agents"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    wos_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    # Worker contract for WO-005 (assigned to codex) only allows requirements.txt
    (contracts_dir / "WO-005.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "codex",
            "work_order": "WO-005",
            "scope": {
                "allow": [{"module": "requirements.txt"}],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 5, "max_tokens": 30000},
        }),
        encoding="utf-8",
    )

    # Claude's agent contract allows work orders, contracts, and decisions
    (agents_dir / "claude.contract.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "agent_id": "claude",
            "work_order": "WO-000",
            "scope": {
                "allow": [
                    {"module": ".sync/work-orders/**"},
                    {"module": ".sync/contracts/**"},
                    {"module": ".sync/decisions/**"},
                ],
                "deny": [{"module": ".git/**"}],
                "write": "read-write",
            },
            "budget": {"max_files_touched": 20, "max_tokens": 50000},
        }),
        encoding="utf-8",
    )

    runner = DummyVerificationRunner(tmp_path)
    task = SimpleNamespace(
        identifier="WO-005",
        work_order_id="WO-005",
        is_authoring=True,
        kind="work_order",
        deliverable_path=None,
        deliverable_paths=(),
    )

    # Architect authored new child work order, contract, and recovery decision
    authored_files = [
        ".sync/contracts/WO-010.yaml",
        ".sync/work-orders/ACTIVE/WO-010.yaml",
        ".sync/decisions/recovery/WO-005.decision.json",
    ]
    for rel_path in authored_files:
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("dummy: content\n", encoding="utf-8")

    # Bookkeeping INDEX.yaml was also modified by authoring
    index_file = sync_dir / "work-orders" / "INDEX.yaml"
    index_file.write_text("schema_version: 1\norders: []\n", encoding="utf-8")

    decision = HarnessDecision(
        status="completed",
        summary="Authored replacement child WO-010 and recovery decision",
        report_markdown="# Recovery Decision",
        blockers=(),
        modified_files=tuple(authored_files),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
        commands=(),
    )

    # Snapshots
    before_snap = WorkspaceSnapshot(root=tmp_path, files={}, timestamp=1.0)
    after_snap = WorkspaceSnapshot.capture(tmp_path)
    diff = before_snap.diff(after_snap)

    staged_errors: list[str] = []
    dims = runner._evaluate_verification_dimensions(
        task=task,
        decision=decision,
        diff=diff,
        before_snapshot=before_snap,
        after_snapshot=after_snap,
        staged_root=tmp_path,
        staged_errors=staged_errors,
        declaration_matches=True,
        command_results=[],
        d025_passed=True,
        has_staged_writes=False,
    )

    assert dims.scope_verified is True, f"Scope verification failed: {staged_errors}"
    assert not staged_errors


def test_authoring_conflict_permits_architect_amendment_during_recovery_decision(tmp_path: Path) -> None:
    """Architect amending an active work order during ARCHITECT_RECOVERY_DECISION must not be rejected."""
    sync_dir = tmp_path / ".sync"
    sup_dir = sync_dir / "runtime" / "supervisor"
    wos_dir = sync_dir / "work-orders" / "ACTIVE"
    sup_dir.mkdir(parents=True, exist_ok=True)
    wos_dir.mkdir(parents=True, exist_ok=True)

    # Active supervisor run in ARCHITECT_RECOVERY_DECISION with recovery_wo_id WO-005
    (sup_dir / "run-test.yaml").write_text(
        yaml.safe_dump({
            "run_id": "run-test",
            "phase": "ARCHITECT_RECOVERY_DECISION",
            "recovery_wo_id": "WO-005",
            "worker_blockers": {"WO-005": {"failure_code": "DEPENDENCY_DEADLOCK"}},
        }),
        encoding="utf-8",
    )

    wo_file = wos_dir / "WO-005.yaml"
    existing_content = yaml.safe_dump({
        "id": "WO-005",
        "title": "Initial Task",
        "status": "ACTIVE",
        "revision": 1,
    })
    wo_file.write_text(existing_content, encoding="utf-8")

    new_data = {
        "id": "WO-005",
        "title": "Amended Task",
        "status": "ACTIVE",
        "revision": 1,  # Same revision authored by Architect
    }
    new_content = yaml.safe_dump(new_data)

    errors: list[str] = []
    _check_overwrite_conflict(
        norm_path=".sync/work-orders/ACTIVE/WO-005.yaml",
        content=new_content,
        data=new_data,
        project_root=tmp_path,
        errors=errors,
        author="claude",
    )

    assert not errors, f"Expected no overwrite conflict errors, got: {errors}"
    # Verify revision was auto-bumped and amendment entry added
    assert new_data["revision"] == 2
    assert len(new_data.get("amendments", [])) == 1
