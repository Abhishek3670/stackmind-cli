"""Integration tests for Fix 7 - Mandatory Quality Gates into Production Release.

Tests that production gates are wired into the real release decision path.
"""

import json
import os
import yaml
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from validators.harness.clean_delivery_gate import CleanDeliveryGate
from validators.harness.requirements import RequirementsLedger, Requirement
from validators.harness.orchestration import evaluate_release_gates, release_decision
from validators.harness.d024_gate import D024Gate, D024GateDecision


class TestEvaluateReleaseGates:
    """Tests for centralized gate evaluation function."""

    def test_all_gates_pass_approved(self):
        """Complete project with all gates passing is approved."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert approved is True
        assert reasons == ()

    def test_clean_delivery_fails_blocked(self):
        """Contaminated project is rejected by CleanDeliveryGate."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=False,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert approved is False
        assert "Clean delivery gate failed" in reasons[0]

    def test_d024_fails_blocked(self):
        """Missing QA verdict blocks release."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=False,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert approved is False
        assert "D024 QA gate failed" in reasons[0]

    def test_requirements_missing_blocked(self):
        """Missing requirement WO mapping blocks release."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=False,
            execution_qa_passed=True,
            requirements_blockers=("Requirement R001 has no owning work order",),
        )
        assert approved is False
        assert "Requirements coverage" in reasons[0]

    def test_execution_qa_fails_blocked(self):
        """Required execution check that fails blocks acceptance."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=False,
            execution_blockers=("Tests failed",),
        )
        assert approved is False
        assert "Execution QA" in reasons[0]

    def test_stale_evidence_blocked(self):
        """Stale evidence blocks acceptance."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
            has_stale_evidence=True,
        )
        assert approved is False
        assert "Evidence freshness" in reasons[0]

    def test_all_gates_fail_blocked(self):
        """Multiple gate failures all reported."""
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=False,
            d024_passed=False,
            requirements_passed=False,
            execution_qa_passed=False,
            requirements_blockers=("Requirement R001 has no owning work order",),
            execution_blockers=("Tests failed",),
            has_stale_evidence=True,
        )
        assert approved is False
        assert len(reasons) == 5  # All blocking reasons


class TestCleanDeliveryGateIntegration:
    """Tests that CleanDeliveryGate integrates with release decisions."""

    def test_clean_project_passes(self):
        """Clean project without internal references passes."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create clean files
            (project / "README.md").write_text("# My Project")
            (project / "src").mkdir()
            (project / "src" / "main.py").write_text("print('hello')")

            gate = CleanDeliveryGate(project)
            passed, findings = gate.validate()

            assert passed is True
            assert len(findings) == 0

    def test_contaminated_project_blocked(self):
        """Project with prohibited internal references is rejected."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create file with prohibited reference
            (project / "README.md").write_text("""
# My Project
Managed by stackmind
""")

            gate = CleanDeliveryGate(project)
            passed, findings = gate.validate()

            assert passed is False
            assert any("Prohibited term" in f["issue"] for f in findings)

    def test_internal_sync_excluded(self):
        """Internal .sync/ files are excluded from scanning."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create .sync with prohibited reference (should be excluded)
            sync_dir = project / ".sync"
            sync_dir.mkdir()
            (sync_dir / "README.md").write_text("Managed by stackmind")

            # Create clean product file
            (project / "README.md").write_text("# My Project")

            gate = CleanDeliveryGate(project)
            passed, findings = gate.validate()

            assert passed is True


class TestRequirementsIntegration:
    """Tests that requirements coverage integrates with release decisions."""

    def test_complete_requirements_pass(self):
        """All requirements with evidence pass coverage check."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            sync_path = project / ".sync" / "state"
            sync_path.mkdir(parents=True)

            ledger_path = sync_path / "requirements.yaml"
            ledger = RequirementsLedger()
            ledger.add_requirement(Requirement(
                id="R001",
                text="Complete feature",
                type="FUNCTIONAL",
                priority="HIGH",
                status="PASS",
                work_order_ids=["WO-001"],
                evidence={"test": "passed"},
            ))
            ledger.save(ledger_path)

            from validators.harness.requirements import validate_requirements_coverage
            passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

            assert passed is True
            assert issues == []

    def test_missing_requirement_blocks(self):
        """Requirement without WO assignment blocks release."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            sync_path = project / ".sync" / "state"
            sync_path.mkdir(parents=True)

            ledger_path = sync_path / "requirements.yaml"
            ledger = RequirementsLedger()
            ledger.add_requirement(Requirement(
                id="R001",
                text="Orphan requirement",
                type="FUNCTIONAL",
                priority="HIGH",
                status="PASS",
                work_order_ids=[],
                evidence={"note": "no WO"},
            ))
            ledger.save(ledger_path)

            from validators.harness.requirements import validate_requirements_coverage
            passed, issues = validate_requirements_coverage(ledger, [])

            assert passed is False
            assert any("has no owning work order" in i for i in issues)

    def test_pending_requirement_blocks(self):
        """Pending requirement blocks release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Pending feature",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PENDING",
            work_order_ids=["WO-001"],
        ))

        from validators.harness.requirements import validate_requirements_coverage
        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

        assert passed is False
        assert any("is PENDING" in i for i in issues)


class TestEndToEndScenarios:
    """End-to-end integration scenarios."""

    def test_complete_project_approved(self):
        """Complete, clean project with all gates passing is approved."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create clean deliverables
            (project / "README.md").write_text("# My Project")
            (project / "src").mkdir()
            (project / "src" / "main.py").write_text("print('hello')")

            # Create requirements ledger with all PASS
            sync_path = project / ".sync" / "state"
            sync_path.mkdir(parents=True)
            ledger = RequirementsLedger()
            ledger.add_requirement(Requirement(
                id="R001",
                text="Main feature",
                type="FUNCTIONAL",
                priority="HIGH",
                status="PASS",
                work_order_ids=["WO-001"],
                evidence={"test": "passed"},
            ))
            ledger.save(sync_path / "requirements.yaml")

            # Evaluate gates
            from validators.harness.requirements import validate_requirements_coverage
            requirements_passed, _ = validate_requirements_coverage(ledger, ["WO-001"])

            clean_delivery_passed, _ = CleanDeliveryGate(project).validate()

            approved, reasons = evaluate_release_gates(
                clean_delivery_passed=clean_delivery_passed,
                d024_passed=True,  # Simulate D024 pass
                requirements_passed=requirements_passed,
                execution_qa_passed=True,
            )

            assert approved is True
            assert reasons == ()

    def test_contaminated_project_blocked(self):
        """Project with internal references is blocked even if other gates pass."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create contaminated file
            (project / "README.md").write_text("Managed by stackmind")

            # Create clean requirements
            sync_path = project / ".sync" / "state"
            sync_path.mkdir(parents=True)
            ledger = RequirementsLedger()
            ledger.add_requirement(Requirement(
                id="R001",
                text="Main feature",
                type="FUNCTIONAL",
                priority="HIGH",
                status="PASS",
                work_order_ids=["WO-001"],
                evidence={"test": "passed"},
            ))
            ledger.save(sync_path / "requirements.yaml")

            # Evaluate gates
            clean_delivery_passed, _ = CleanDeliveryGate(project).validate()

            approved, reasons = evaluate_release_gates(
                clean_delivery_passed=clean_delivery_passed,
                d024_passed=True,
                requirements_passed=True,
                execution_qa_passed=True,
            )

            assert approved is False
            assert "Clean delivery gate failed" in reasons[0]

    def test_missing_requirement_blocked(self):
        """Project with orphan requirement is blocked even if other gates pass."""
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "test_project"
            project.mkdir()

            # Create clean deliverables
            (project / "README.md").write_text("# My Project")

            # Create requirements with orphan
            sync_path = project / ".sync" / "state"
            sync_path.mkdir(parents=True)
            ledger = RequirementsLedger()
            ledger.add_requirement(Requirement(
                id="R001",
                text="Orphan requirement",
                type="FUNCTIONAL",
                priority="HIGH",
                status="PASS",
                work_order_ids=[],
            ))
            ledger.save(sync_path / "requirements.yaml")

            # Evaluate gates - directly check validation
            from validators.harness.requirements import validate_requirements_coverage
            requirements_passed, requirements_blockers = validate_requirements_coverage(ledger, [])

            assert requirements_passed is False
            assert any("has no owning work order" in i for i in requirements_blockers)

            approved, reasons = evaluate_release_gates(
                clean_delivery_passed=True,
                d024_passed=True,
                requirements_passed=requirements_passed,
                execution_qa_passed=True,
                has_requirements_blocked=True,
                requirements_blockers=requirements_blockers,
            )

            assert approved is False
            assert "Requirements coverage" in reasons[0]
