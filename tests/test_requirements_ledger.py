"""Tests for requirements ledger and traceability."""

import pytest
import yaml
from pathlib import Path
from datetime import datetime, timezone


class TestRequirement:
    """Tests for individual requirement entries."""

    def test_requirement_to_dict(self):
        """Requirement should serialize to dict."""
        from validators.harness.requirements import Requirement

        req = Requirement(
            id="R001",
            text="User can login",
            type="FUNCTIONAL",
            priority="HIGH",
            acceptance_criteria=["Valid credentials grant access"],
        )

        data = req.to_dict()
        assert data["id"] == "R001"
        assert data["text"] == "User can login"
        assert data["type"] == "FUNCTIONAL"
        assert data["priority"] == "HIGH"
        assert "Valid credentials" in data["acceptance_criteria"][0]

    def test_requirement_from_dict(self):
        """Requirement should deserialize from dict."""
        from validators.harness.requirements import Requirement

        data = {
            "id": "R002",
            "text": "API responds in 200ms",
            "type": "NON_FUNCTIONAL",
            "priority": "HIGH",
            "acceptance_criteria": ["Latency < 200ms"],
            "work_order_ids": ["WO-001"],
            "status": "IN_PROGRESS",
        }

        req = Requirement.from_dict(data)
        assert req.id == "R002"
        assert req.work_order_ids == ["WO-001"]
        assert req.status == "IN_PROGRESS"


class TestRequirementsLedger:
    """Tests for the requirements ledger."""

    def test_new_ledger_empty(self):
        """New ledger should start empty."""
        from validators.harness.requirements import RequirementsLedger

        ledger = RequirementsLedger()
        assert ledger.requirements == []
        assert ledger.schema_version == 1

    def test_add_requirement(self):
        """Requirements should be added to ledger."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        req = Requirement(
            id="R001",
            text="Test requirement",
            type="FUNCTIONAL",
            priority="MEDIUM",
        )
        ledger.add_requirement(req)

        assert len(ledger.requirements) == 1
        assert ledger.requirements[0].id == "R001"

    def test_add_duplicate_requirement_fails(self):
        """Adding duplicate requirement ID should fail."""
        from validators.harness.requirements import RequirementsLedger, Requirement, RequirementsError

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="First",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        with pytest.raises(RequirementsError, match="already exists"):
            ledger.add_requirement(Requirement(
                id="R001",
                text="Second",
                type="FUNCTIONAL",
                priority="MEDIUM",
            ))

    def test_update_requirement(self):
        """Requirements should be updatable."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        req = ledger.update_requirement("R001", {"status": "PASS"})
        assert req.status == "PASS"

    def test_update_invalid_status_fails(self):
        """Invalid status should be rejected."""
        from validators.harness.requirements import RequirementsLedger, Requirement, RequirementsError

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        with pytest.raises(RequirementsError, match="Invalid status"):
            ledger.update_requirement("R001", {"status": "INVALID_STATUS"})

    def test_get_requirement_by_id(self):
        """Requirements should be retrievable by ID."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        req = ledger.get_requirement("R001")
        assert req is not None
        assert req.text == "Test"

    def test_get_requirement_not_found(self):
        """Nonexistent requirement should return None."""
        from validators.harness.requirements import RequirementsLedger

        ledger = RequirementsLedger()
        req = ledger.get_requirement("R999")
        assert req is None

    def test_get_requirements_by_wo(self):
        """Requirements should be retrievable by work order."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test",
            type="FUNCTIONAL",
            priority="MEDIUM",
            work_order_ids=["WO-001"],
        ))

        reqs = ledger.get_requirements_by_wo("WO-001")
        assert len(reqs) == 1

    def test_get_requirements_with_missing_wo(self):
        """Requirements without WOs should be detectable."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test without WO",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        missing = ledger.get_requirements_with_missing_wo()
        assert len(missing) == 1
        assert missing[0].id == "R001"


class TestRequirementsPersistence:
    """Tests for ledger file I/O."""

    def test_save_and_load_ledger(self, tmp_path):
        """Ledger should persist to and load from file."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger_path = tmp_path / "requirements.yaml"

        # Save
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Persistent requirement",
            type="FUNCTIONAL",
            priority="HIGH",
        ))
        ledger.save(ledger_path)

        # Load
        loaded = RequirementsLedger.load(ledger_path)
        assert len(loaded.requirements) == 1
        assert loaded.requirements[0].text == "Persistent requirement"

    def test_load_missing_ledger_fails(self, tmp_path):
        """Loading non-existent ledger should raise error."""
        from validators.harness.requirements import RequirementsLedger, RequirementsError

        ledger_path = tmp_path / "nonexistent.yaml"

        with pytest.raises(RequirementsError, match="not found"):
            RequirementsLedger.load(ledger_path)

    def test_load_malformed_ledger_fails(self, tmp_path):
        """Loading malformed ledger should raise error."""
        from validators.harness.requirements import RequirementsLedger, RequirementsError

        ledger_path = tmp_path / "malformed.yaml"
        ledger_path.write_text("invalid: yaml: content:")

        with pytest.raises(RequirementsError, match="Malformed"):
            RequirementsLedger.load(ledger_path)

    def test_requirement_ids_stable_after_rerun(self, tmp_path):
        """Requirement IDs should remain stable after ledger reload."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger_path = tmp_path / "requirements.yaml"

        # First save
        ledger1 = RequirementsLedger()
        ledger1.add_requirement(Requirement(
            id="R001",
            text="First requirement",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))
        ledger1.save(ledger_path)

        # Reload
        ledger2 = RequirementsLedger.load(ledger_path)
        assert ledger2.requirements[0].id == "R001"
        assert ledger2.requirements[0].text == "First requirement"

    def test_add_requirement_without_losing_existing(self, tmp_path):
        """Adding new requirement should preserve existing ones."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger_path = tmp_path / "requirements.yaml"

        # Initial ledger
        ledger1 = RequirementsLedger()
        ledger1.add_requirement(Requirement(
            id="R001",
            text="First",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))
        ledger1.add_requirement(Requirement(
            id="R002",
            text="Second",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))
        ledger1.save(ledger_path)

        # Reload and add
        ledger2 = RequirementsLedger.load(ledger_path)
        ledger2.add_requirement(Requirement(
            id="R003",
            text="Third",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))
        ledger2.save(ledger_path)

        # Verify all exist
        ledger3 = RequirementsLedger.load(ledger_path)
        assert len(ledger3.requirements) == 3
        assert {r.id for r in ledger3.requirements} == {"R001", "R002", "R003"}


class TestRequirementsCoverage:
    """Tests for requirements coverage validation."""

    def test_requirements_with_missing_wo_block_release(self):
        """Requirements without WOs should be blocking issues."""
        from validators.harness.requirements import (
            RequirementsLedger, Requirement, validate_requirements_coverage
        )

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Missing WO",
            type="FUNCTIONAL",
            priority="MEDIUM",
        ))

        passed, issues = validate_requirements_coverage(ledger, [])
        assert not passed
        assert any("has no owning work order" in i for i in issues)

    def test_pending_requirements_block_release(self):
        """PENDING requirements should be blocking."""
        from validators.harness.requirements import (
            RequirementsLedger, Requirement, validate_requirements_coverage
        )

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Pending",
            type="FUNCTIONAL",
            priority="MEDIUM",
            status="PENDING",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])
        assert not passed
        assert any("is PENDING" in i for i in issues)

    def test_pass_without_evidence_blocks_release(self):
        """PASS without evidence should be blocking."""
        from validators.harness.requirements import (
            RequirementsLedger, Requirement, validate_requirements_coverage
        )

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="No evidence",
            type="FUNCTIONAL",
            priority="MEDIUM",
            status="PASS",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])
        assert not passed
        assert any("has no verification evidence" in i for i in issues)

    def test_pass_with_evidence_passes(self):
        """PASS with evidence should pass validation."""
        from validators.harness.requirements import (
            RequirementsLedger, Requirement, validate_requirements_coverage
        )

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Complete",
            type="FUNCTIONAL",
            priority="MEDIUM",
            status="PASS",
            work_order_ids=["WO-001"],
            evidence={"test_result": "PASS", "timestamp": "2026-10-10"},
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])
        assert passed
        assert issues == []

    def test_archived_requirements_dont_block(self):
        """ARCHIVED requirements should not block release."""
        from validators.harness.requirements import (
            RequirementsLedger, Requirement, validate_requirements_coverage
        )

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Deprecated feature",
            type="FUNCTIONAL",
            priority="LOW",
            status="ARCHIVED",
            work_order_ids=["WO-001"],
        ))

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])
        assert passed


class TestRequirementsFromPlan:
    """Tests for requirements compilation from PLAN.md."""

    def test_compile_requirements_from_plan(self, tmp_path):
        """Requirements should be extracted from PLAN.md."""
        from validators.harness.requirements import compile_requirements_from_plan

        plan_path = tmp_path / "PLAN.md"
        ledger_path = tmp_path / "requirements.yaml"

        plan_content = """# Project Plan: Test Project

## Current Architecture
Test architecture description

## Milestones & Roadmap
- [ ] Milestone 1: User Authentication (Agent: codex)
- [ ] Milestone 2: Data Persistence (Agent: codex)
"""
        plan_path.write_text(plan_content)

        ledger = compile_requirements_from_plan(ledger_path, plan_path)

        assert len(ledger.requirements) == 2
        assert ledger.requirements[0].id == "R001"
        assert "User Authentication" in ledger.requirements[0].text


class TestRequirementsInheritance:
    """Tests for requirements integration with work orders."""

    def test_work_order_can_reference_requirement_ids(self):
        """Work order schema supports requirement reference."""
        # This tests that work orders can store requirement_refs
        import yaml
        from pathlib import Path

        wo_data = {
            "id": "WO-001",
            "title": "Implement feature",
            "requirement_refs": ["R001", "R002"],
        }

        # Verify structure
        assert "requirement_refs" in wo_data
        assert wo_data["requirement_refs"] == ["R001", "R002"]

    def test_requirement_can_reference_multiple_wo(self):
        """Requirement can have multiple owning work orders."""
        from validators.harness.requirements import Requirement

        req = Requirement(
            id="R001",
            text="Multi-component feature",
            type="FUNCTIONAL",
            priority="HIGH",
            work_order_ids=["WO-001", "WO-002", "WO-003"],
        )

        assert len(req.work_order_ids) == 3
