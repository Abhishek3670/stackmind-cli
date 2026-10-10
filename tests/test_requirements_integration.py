"""Integration tests for requirements ledger with work orders and release gates."""

import pytest
from pathlib import Path
from validators.harness.requirements import (
    RequirementsLedger, Requirement, validate_requirements_coverage, RequirementsError
)
from validators.harness.authoring_gate import AuthoringGate


class TestWorkOrderRequirementReferences:
    """Tests that work orders can reference requirements."""

    def test_work_order_with_requirement_refs(self, tmp_path):
        """Work order with requirement_refs should validate."""
        import yaml

        wo_path = tmp_path / "WO-001.yaml"
        wo_data = {
            "id": "WO-001",
            "type": "FEATURE",
            "title": "Implement user login",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "requirement_refs": ["R001", "R002"],
            "deliverable": {
                "type": "code",
                "path": "src/auth.py",
                "description": "Authentication handler",
            }
        }
        wo_path.write_text(yaml.dump(wo_data))

        gate = AuthoringGate(project_root=tmp_path)
        content = wo_path.read_text()
        decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-001.yaml", content)

        assert decision.passed

    def test_work_order_invalid_requirement_refs_fails(self, tmp_path):
        """Invalid requirement_refs should be rejected."""
        import yaml

        wo_path = tmp_path / "WO-002.yaml"
        wo_data = {
            "id": "WO-002",
            "type": "FEATURE",
            "title": "Test",
            "status": "ACTIVE",
            "priority": "P0",
            "assigned_agents": ["codex"],
            "dependencies": [],
            "requirement_refs": ["INVALID"],  # Should start with R
            "deliverable": {
                "type": "code",
                "path": "test.py",
                "description": "Test",
            }
        }
        wo_path.write_text(yaml.dump(wo_data))

        gate = AuthoringGate(project_root=tmp_path)
        content = wo_path.read_text()
        decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", content)

        assert not decision.passed
        assert any("requirement_refs" in e for e in decision.errors)


class TestRequirementsCoverageGate:
    """Tests for release blocking based on requirements coverage."""

    def test_release_blocked_with_missing_requirement(self):
        """Missing requirement WO mapping should block release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Required feature",
            type="FUNCTIONAL",
            priority="HIGH",
        ))

        passed, issues = validate_requirements_coverage(ledger, [])

        assert not passed
        assert len(issues) > 0

    def test_release_blocked_with_pending_requirement(self):
        """PENDING requirement should block release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Pending feature",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PENDING",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

        assert not passed

    def test_release_blocked_with_fail_requirement(self):
        """FAIL requirement should block release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Failed feature",
            type="FUNCTIONAL",
            priority="HIGH",
            status="FAIL",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

        assert not passed

    def test_release_blocked_with_blocked_requirement(self):
        """BLOCKED requirement should block release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Blocked feature",
            type="FUNCTIONAL",
            priority="HIGH",
            status="BLOCKED",
            work_order_ids=["WO-001"],
            evidence={"reason": "Dependency unavailable"},
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

        assert not passed

    def test_release_blocked_without_evidence(self):
        """PASS without evidence should block release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="No evidence",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PASS",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])

        assert not passed
        assert any("no verification evidence" in i for i in issues)

    def test_release_passed_with_complete_requirements(self):
        """All PASS with evidence should allow release."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Complete",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PASS",
            work_order_ids=["WO-001"],
            evidence={"test_result": "PASS", "verified_by": "gemma"},
        ))
        ledger.add_requirement(Requirement(
            id="R002",
            text="Complete too",
            type="FUNCTIONAL",
            priority="MEDIUM",
            status="PASS",
            work_order_ids=["WO-002"],
            evidence={"test_result": "PASS"},
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001", "WO-002"])

        assert passed
        assert issues == []

    def test_requirements_without_work_orders_still_blocked(self):
        """Requirements without work orders are blocked even if status is PASS."""
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Orphan",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PASS",
            work_order_ids=[],
            evidence={"note": "No WO assigned"},
        ))

        passed, issues = validate_requirements_coverage(ledger, [])

        assert not passed
        assert any("has no owning work order" in i for i in issues)


class TestRequirementsInWorkflow:
    """Tests for requirements in the full workflow."""

    def test_requirement_id_stable_after_multiple_updates(self, tmp_path):
        """Requirement ID should remain stable through updates."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger_path = tmp_path / "requirements.yaml"

        # Create and save
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Original",
            type="FUNCTIONAL",
            priority="HIGH",
        ))
        ledger.save(ledger_path)

        # Reload and update multiple times
        ledger = RequirementsLedger.load(ledger_path)
        ledger.update_requirement("R001", {"status": "IN_PROGRESS"})
        ledger.save(ledger_path)

        ledger = RequirementsLedger.load(ledger_path)
        ledger.update_requirement("R001", {"status": "PASS", "evidence": {"test": "ok"}})
        ledger.save(ledger_path)

        # Verify ID and status
        ledger = RequirementsLedger.load(ledger_path)
        assert ledger.requirements[0].id == "R001"
        assert ledger.requirements[0].status == "PASS"

    def test_new_requirement_added_without_losing_old(self, tmp_path):
        """Adding new requirements should not lose existing ones."""
        ledger_path = tmp_path / "requirements.yaml"

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="First",
            type="FUNCTIONAL",
            priority="HIGH",
        ))
        ledger.add_requirement(Requirement(
            id="R002",
            text="Second",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))
        ledger.save(ledger_path)

        # Add third
        ledger = RequirementsLedger.load(ledger_path)
        ledger.add_requirement(Requirement(
            id="R003",
            text="Third",
            type="FUNCTIONAL",
            priority="LOW",
        ))
        ledger.save(ledger_path)

        # Verify all exist
        ledger = RequirementsLedger.load(ledger_path)
        ids = {r.id for r in ledger.requirements}
        assert ids == {"R001", "R002", "R003"}

    def test_requirement_split_to_multiple_wos(self, tmp_path):
        """A requirement can be split across multiple work orders."""
        ledger_path = tmp_path / "requirements.yaml"

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Large feature split across modules",
            type="FUNCTIONAL",
            priority="HIGH",
            work_order_ids=["WO-001", "WO-002", "WO-003"],
        ))
        ledger.save(ledger_path)

        ledger = RequirementsLedger.load(ledger_path)
        assert len(ledger.requirements[0].work_order_ids) == 3
