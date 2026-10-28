"""Tests for Architect Blocker Autonomous Resolution and Documentation Auto-Remediation."""

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


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _setup_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    sync = ws / ".sync"
    (sync / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)
    (sync / "work-orders" / "COMPLETED").mkdir(parents=True, exist_ok=True)
    (sync / "contracts").mkdir(parents=True, exist_ok=True)
    (sync / "inbox" / "claude").mkdir(parents=True, exist_ok=True)
    (sync / "runtime").mkdir(parents=True, exist_ok=True)

    _write_yaml(
        sync / "work-orders" / "INDEX.yaml",
        {"orders": [], "total_active": 0, "total_completed": 0, "total_blocked": 0, "next_id": 1},
    )
    _write_yaml(sync / "runtime" / "TREE.yaml", {"nodes": []})
    (ws / "PLAN.md").write_text("# Project Plan\n\n## Milestones\n- M1: Core Feature", encoding="utf-8")
    (ws / "VERSION.md").write_text("0.1.0\n", encoding="utf-8")
    return ws


class TestArchitectBlockerAutomation:
    """Test autonomous resolution of documentation, WO-000, and retry blockers."""

    def test_ensure_release_documentation_synthesizes_changelog_and_readme(self, tmp_path: Path):
        ws = _setup_workspace(tmp_path)
        mgr = MockSessionManager(ws)
        sup = LifecycleSupervisor(mgr)

        state = RunState(
            run_id="run-1",
            product_goal="3D Voice-Activated Particle Globe Assistant",
            workspace=str(ws),
            session_id="sess-1",
            completed_wo_ids=["WO-001"],
            worker_wo_ids=["WO-001"],
        )

        # Initial scaffold boilerplate
        (ws / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n- Initial repository scaffolding\n",
            encoding="utf-8",
        )
        (ws / "README.md").write_text(
            "# clean_test\n\nv3.1.0 scaffold template\nTODO: Add project description\n",
            encoding="utf-8",
        )
        # Create a deliverable for WO-001
        (ws / "index.html").write_text("<div id='orb'></div>", encoding="utf-8")
        _write_yaml(
            ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml",
            {
                "id": "WO-001",
                "title": "Frontend Orb",
                "status": "COMPLETED",
                "deliverable": {"path": "index.html"},
            },
        )

        sup._ensure_release_documentation(ws, state)

        # 1. CHANGELOG.md should have [0.1.0] section with deliverable
        cl_text = (ws / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [0.1.0]" in cl_text
        assert "index.html" in cl_text
        assert "3D Voice-Activated Particle Globe Assistant" in cl_text

        # 2. README.md should be populated with rich sections
        rm_text = (ws / "README.md").read_text(encoding="utf-8")
        assert "# 3D Voice-Activated Particle Globe Assistant" in rm_text
        assert "## Architecture & Verified Deliverables" in rm_text
        assert "index.html" in rm_text
        assert "## Getting Started" in rm_text

    def test_wo_000_reconciled_on_executing_completion(self, tmp_path: Path):
        ws = _setup_workspace(tmp_path)
        mgr = MockSessionManager(ws)
        sup = LifecycleSupervisor(mgr)

        # Put WO-000 in ACTIVE
        _write_yaml(
            ws / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml",
            {
                "id": "WO-000",
                "title": "System Architecture Plan",
                "status": "ACTIVE",
                "deliverable": {"path": "PLAN.md"},
            },
        )

        # Worker WO-001 is completed
        _write_yaml(
            ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml",
            {"id": "WO-001", "status": "COMPLETED", "deliverable": {"path": "src/app.py"}},
        )

        state = RunState(
            run_id="run-1",
            product_goal="Sample App",
            workspace=str(ws),
            session_id="sess-1",
            phase=Phase.EXECUTING,
            worker_wo_ids=["WO-001"],
            completed_wo_ids=["WO-001"],
            planning_wo_id="WO-000",
        )

        res = sup._advance_executing(state)
        assert res == AdvanceResult.TRANSITIONED
        assert state.phase == Phase.INTEGRATION_REVIEW

        # WO-000 must now be COMPLETED on disk and in state.completed_wo_ids
        assert "WO-000" in state.completed_wo_ids
        assert not (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml").exists()
        assert (ws / ".sync" / "work-orders" / "COMPLETED" / "WO-000.yaml").exists()

    def test_integration_review_auto_remediates_packaging_blockers(self, tmp_path: Path):
        ws = _setup_workspace(tmp_path)
        mgr = MockSessionManager(ws)
        sup = LifecycleSupervisor(mgr)

        # Setup completed deliverables
        (ws / "src").mkdir(parents=True, exist_ok=True)
        (ws / "src" / "app.py").write_text("print('hello')", encoding="utf-8")
        _write_yaml(
            ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml",
            {"id": "WO-001", "status": "COMPLETED", "deliverable": {"path": "src/app.py"}},
        )
        _write_yaml(
            ws / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml",
            {"id": "WO-000", "status": "ACTIVE", "deliverable": {"path": "PLAN.md"}},
        )

        state = RunState(
            run_id="run-9651c57e",
            product_goal="Globe Assistant",
            workspace=str(ws),
            session_id="sess-92909a4e",
            phase=Phase.INTEGRATION_REVIEW,
            worker_wo_ids=["WO-001"],
            completed_wo_ids=["WO-001"],
            integration_wo_id="WO-008",
            integration_operation_id="op-rev",
        )

        # Mock review operation reporting the 4 exact blockers from the user screenshot
        mgr.operations["op-rev"] = {
            "operation_id": "op-rev",
            "status": "BLOCKED",
            "result": {
                "status": "blocked",
                "summary": "Core application deliverables are real and verified. Blocked solely by packaging.",
                "blockers": [
                    "CHANGELOG.md does not document version 0.1.0 or the ORB milestones; it still contains only '[Unreleased] Initial repository scaffolding', contradicting GitOps publication claim.",
                    "README.md is untouched template boilerplate ('clean_test', v3.1.0) and does not describe ORB or the 0.1.0 runtime.",
                    "Work-order board shows WO-000 (PLAN.md) still ACTIVE while release declares all work orders complete.",
                    "Supervisor run logged WORKER_TIMEOUT (retry 1/2) anomaly on run-9651c57e.",
                ],
                "blocker_details": [
                    {"finding": "CHANGELOG.md missing 0.1.0", "remediation": "Update CHANGELOG.md", "path": "CHANGELOG.md"},
                    {"finding": "README.md is boilerplate", "remediation": "Update README.md", "path": "README.md"},
                    {"finding": "WO-000 active on board", "remediation": "Archive WO-000", "path": ".sync/work-orders/ACTIVE/WO-000.yaml"},
                    {"finding": "WORKER_TIMEOUT anomaly", "remediation": "Ignore past retry", "path": None},
                ],
            },
        }

        res = sup._advance_integration_review(state)
        # Should autonomously resolve all 4 non-code blockers and transition to PRODUCT_READY
        assert res == AdvanceResult.TRANSITIONED
        assert state.phase == Phase.PRODUCT_READY
        assert len(state.integration_blockers) == 0

        # Verify WO-000 was moved to COMPLETED
        assert (ws / ".sync" / "work-orders" / "COMPLETED" / "WO-000.yaml").exists()
        assert not (ws / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml").exists()

        # Verify CHANGELOG.md and README.md were synthesized
        assert (ws / "CHANGELOG.md").is_file()
        assert "## [0.1.0]" in (ws / "CHANGELOG.md").read_text(encoding="utf-8")
        assert (ws / "README.md").is_file()
        assert "# Globe Assistant" in (ws / "README.md").read_text(encoding="utf-8")

    def test_integration_review_preserves_genuine_code_defects(self, tmp_path: Path):
        ws = _setup_workspace(tmp_path)
        mgr = MockSessionManager(ws)
        sup = LifecycleSupervisor(mgr)

        _write_yaml(
            ws / ".sync" / "work-orders" / "COMPLETED" / "WO-001.yaml",
            {"id": "WO-001", "status": "COMPLETED", "deliverable": {"path": "src/app.py"}},
        )

        state = RunState(
            run_id="run-1",
            product_goal="Globe Assistant",
            workspace=str(ws),
            session_id="sess-1",
            phase=Phase.INTEGRATION_REVIEW,
            worker_wo_ids=["WO-001"],
            completed_wo_ids=["WO-001"],
            integration_wo_id="WO-008",
            integration_operation_id="op-rev",
        )

        # Mock review with 1 documentation blocker and 1 genuine code defect
        mgr.operations["op-rev"] = {
            "operation_id": "op-rev",
            "status": "BLOCKED",
            "result": {
                "status": "blocked",
                "summary": "FastAPI endpoint broken",
                "blockers": [
                    "CHANGELOG.md does not document version 0.1.0",
                    "FastAPI server returns 500 Internal Server Error on /api/voices endpoint",
                ],
                "blocker_details": [
                    {"finding": "CHANGELOG.md missing 0.1.0", "path": "CHANGELOG.md"},
                    {"finding": "FastAPI server returns 500 on /api/voices", "remediation": "Fix voice handler", "path": "src/app.py"},
                ],
            },
        }

        res = sup._advance_integration_review(state)
        # Should NOT transition to PRODUCT_READY; genuine code defect must be reworked
        assert res in (AdvanceResult.WAITING_FOR_OPERATION, AdvanceResult.BLOCKED)
        assert any("500 Internal Server Error" in b for b in state.integration_blockers)
        # The packaging blocker should have been filtered out
        assert not any("CHANGELOG.md" in b for b in state.integration_blockers)

    def test_integration_review_contract_has_governed_read_only_scope(self, tmp_path: Path):
        ws = _setup_workspace(tmp_path)
        mgr = MockSessionManager(ws)
        sup = LifecycleSupervisor(mgr)

        state = RunState(
            run_id="run-1",
            product_goal="Sample App",
            workspace=str(ws),
            session_id="sess-1",
            completed_wo_ids=["WO-001"],
        )

        sup._synthesize_integration_review_artifacts(state, ws, "WO-008", ["src/app.py"])

        contract_file = ws / ".sync" / "contracts" / "WO-008.yaml"
        assert contract_file.is_file()
        cdata = yaml.safe_load(contract_file.read_text(encoding="utf-8"))

        scope = cdata["scope"]
        assert scope["write"] == "read-only"
        allowed_mods = [r["module"] for r in scope["allow"] if isinstance(r, dict)]
        assert "README.md" in allowed_mods
        assert "CHANGELOG.md" in allowed_mods
        assert "PLAN.md" in allowed_mods
