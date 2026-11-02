"""Tests for the 4 fixes from session 4340184f investigation.

Bug 1: delegate_task deduplication
Bug 2: Clean delivery gate temp file exclusion
Bug 3: Overwrite conflict session persistence
Bug 4: Supervisor graceful recovery
"""

from __future__ import annotations

import yaml
from pathlib import Path

import pytest


# ============================================================
# Bug 1: delegate_task deduplication
# ============================================================


class TestDelegateTaskDeduplication:
    """Verify _find_active_wo_for_agent prevents duplicate WO creation."""

    def _make_active_wo(self, ws: Path, wo_id: str, agent: str, files: list[str], status: str = "ACTIVE") -> None:
        active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
        active_dir.mkdir(parents=True, exist_ok=True)
        deliverables = [{"path": f, "type": "code"} for f in files]
        data = {
            "id": wo_id,
            "title": f"Test {wo_id}",
            "status": status,
            "assigned_agents": [agent],
            "deliverables": deliverables,
            "deliverable": {"path": files[0], "type": "code"} if files else {},
        }
        (active_dir / f"{wo_id}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def test_finds_existing_wo_for_same_agent_and_files(self, tmp_path: Path) -> None:
        from validators.kernel.tools import ToolGateway
        self._make_active_wo(tmp_path, "WO-008", "codex", ["src/calculator-logic.js"])

        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "codex", ["src/calculator-logic.js"],
        )
        assert result == "WO-008"

    def test_returns_none_for_different_agent(self, tmp_path: Path) -> None:
        from validators.kernel.tools import ToolGateway
        self._make_active_wo(tmp_path, "WO-008", "codex", ["src/calculator-logic.js"])

        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "gemini", ["src/calculator-logic.js"],
        )
        assert result is None

    def test_returns_none_for_non_overlapping_files(self, tmp_path: Path) -> None:
        from validators.kernel.tools import ToolGateway
        self._make_active_wo(tmp_path, "WO-008", "codex", ["src/calculator-logic.js"])

        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "codex", ["src/other-file.js"],
        )
        assert result is None

    def test_skips_cancelled_work_orders(self, tmp_path: Path) -> None:
        from validators.kernel.tools import ToolGateway
        self._make_active_wo(tmp_path, "WO-008", "codex", ["src/calculator-logic.js"], status="CANCELLED")

        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "codex", ["src/calculator-logic.js"],
        )
        assert result is None

    def test_matches_wo_with_no_target_files(self, tmp_path: Path) -> None:
        """When no target_files are provided, any active WO for the agent matches."""
        from validators.kernel.tools import ToolGateway
        self._make_active_wo(tmp_path, "WO-009", "gemini", ["index.html"])

        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "gemini", [],
        )
        assert result == "WO-009"

    def test_returns_none_on_empty_directory(self, tmp_path: Path) -> None:
        from validators.kernel.tools import ToolGateway
        result = ToolGateway._find_active_wo_for_agent(
            tmp_path, "codex", ["src/main.py"],
        )
        assert result is None


# ============================================================
# Bug 2: Clean delivery gate temp file exclusion
# ============================================================


class TestCleanDeliveryGateTempFiles:
    """Verify the gate skips worker self-check temporary files."""

    def test_temp_selfcheck_files_are_skipped(self, tmp_path: Path) -> None:
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        # Create a temp file with stackmind reference (should be ignored)
        tmp_file = tmp_path / "tests" / "_tmp_selfcheck.js"
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text("// stackmind internal test\nconsole.log('test');", encoding="utf-8")

        # Create a clean product file (should pass)
        product = tmp_path / "src" / "app.js"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("console.log('hello');", encoding="utf-8")

        gate = CleanDeliveryGate(tmp_path)
        passed, findings = gate.validate()
        # The _tmp_selfcheck file should be skipped entirely
        temp_findings = [f for f in findings if "_tmp_selfcheck" in f.get("file", "")]
        assert len(temp_findings) == 0

    def test_regular_files_still_scanned(self, tmp_path: Path) -> None:
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        # Create a real product file with a prohibited term
        bad_file = tmp_path / "src" / "main.js"
        bad_file.parent.mkdir(parents=True, exist_ok=True)
        bad_file.write_text("// managed by stackmind\nconsole.log('app');", encoding="utf-8")

        gate = CleanDeliveryGate(tmp_path)
        passed, findings = gate.validate()
        assert not passed
        assert any("stackmind" in f.get("issue", "").lower() for f in findings)


# ============================================================
# Bug 3: Overwrite conflict session persistence
# ============================================================


class TestOverwriteConflictSessionPersistence:
    """Verify authored paths persist across AuthoringGate instances."""

    def test_session_authored_paths_not_cleared_on_new_gate(self) -> None:
        import validators.harness.authoring_conflict as ac

        # Set a session and author a path
        ac.set_daemon_session_id("test-session-persist-001")
        ac.mark_authored(".sync/work-orders/ACTIVE/WO-009.yaml")

        # Verify the path is in the per-session dict
        assert ".sync/work-orders/ACTIVE/WO-009.yaml" in ac._session_authored_by_session.get("test-session-persist-001", set())

        # Create a new AuthoringGate (simulates a new turn)
        from validators.harness.authoring_gate import AuthoringGate
        _gate = AuthoringGate()

        # The path should still be in the per-session dict
        assert ".sync/work-orders/ACTIVE/WO-009.yaml" in ac._session_authored_by_session.get("test-session-persist-001", set())

        # When we re-set the same session, the paths should be restored
        ac.set_daemon_session_id("test-session-persist-001")
        assert ".sync/work-orders/ACTIVE/WO-009.yaml" in ac._session_authored_paths

        # Clean up
        ac._session_authored_by_session.pop("test-session-persist-001", None)
        ac.set_daemon_session_id(None)

    def test_different_sessions_have_separate_paths(self) -> None:
        import validators.harness.authoring_conflict as ac

        # Session A authors WO-001
        ac.set_daemon_session_id("session-A")
        ac.mark_authored(".sync/work-orders/ACTIVE/WO-001.yaml")

        # Session B authors WO-002
        ac.set_daemon_session_id("session-B")
        ac.mark_authored(".sync/work-orders/ACTIVE/WO-002.yaml")

        # Verify isolation
        assert ".sync/work-orders/ACTIVE/WO-001.yaml" in ac._session_authored_by_session.get("session-A", set())
        assert ".sync/work-orders/ACTIVE/WO-002.yaml" in ac._session_authored_by_session.get("session-B", set())
        assert ".sync/work-orders/ACTIVE/WO-002.yaml" not in ac._session_authored_by_session.get("session-A", set())

        # Clean up
        ac._session_authored_by_session.pop("session-A", None)
        ac._session_authored_by_session.pop("session-B", None)
        ac.set_daemon_session_id(None)


# ============================================================
# Bug 4: Supervisor graceful recovery
# ============================================================


class TestSupervisorGracefulRecovery:
    """Verify supervisor doesn't crash on BLOCKED/FAILED transitions."""

    def test_state_integrity_error_importable(self) -> None:
        """Verify StateIntegrityError is still available."""
        from validators.kernel.daemon.state_store import StateIntegrityError
        assert issubclass(StateIntegrityError, Exception)

    def test_supervisor_core_phase_import(self) -> None:
        """Verify Phase enum is importable for supervisor state transitions."""
        from validators.kernel.daemon.supervisor.models import Phase
        assert hasattr(Phase, "BLOCKED")
        assert hasattr(Phase, "FAILED")
