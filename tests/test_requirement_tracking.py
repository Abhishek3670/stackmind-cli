"""Tests for requirement tracking and decompos validation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import yaml

from validators.harness.requirement_tracking import (
    RequirementOwnership,
    RequirementDecompositionValidator,
    compile_requirements_from_milestones,
    validate_requirement_coverage,
)
from validators.harness.requirements import (
    Requirement,
    RequirementsLedger,
)


class TestRequirementOwnership:
    def test_requirement_ownership_creation(self):
        ownership = RequirementOwnership(
            requirement_id="R001",
            work_order_id="WO-abc123",
            milestone_id="M1",
        )
        assert ownership.requirement_id == "R001"
        assert ownership.work_order_id == "WO-abc123"
        assert ownership.milestone_id == "M1"

    def test_to_dict(self):
        ownership = RequirementOwnership(
            requirement_id="R001",
            work_order_id="WO-abc123",
            milestone_id="M1",
            created_at="2024-01-01T00:00:00+00:00",
        )
        d = ownership.to_dict()
        assert d["requirement_id"] == "R001"
        assert d["work_order_id"] == "WO-abc123"
        assert d["milestone_id"] == "M1"


class TestRequirementDecompositionValidator:
    def test_valid_requirement_reference(self):
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test requirement",
            type="FUNCTIONAL",
            priority="HIGH",
            milestone_id="M1",
        ))
        validator = RequirementDecompositionValidator(ledger, known_milestones={"M1"})

        valid, errors = validator.validate_wo_requires_requirement("WO-1", "R001")
        assert valid
        assert len(errors) == 0

    def test_unknown_requirement_reference(self):
        ledger = RequirementsLedger()
        validator = RequirementDecompositionValidator(ledger)

        valid, errors = validator.validate_wo_requires_requirement("WO-1", "R999")
        assert not valid
        assert "R999" in errors[0]

    def test_unknown_milestone_reference(self):
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test requirement",
            type="FUNCTIONAL",
            priority="HIGH",
            milestone_id="M999",
        ))
        validator = RequirementDecompositionValidator(ledger, known_milestones={"M1"})

        valid, errors = validator.validate_wo_requires_requirement("WO-1", "R001")
        assert not valid
        assert "M999" in errors[0]


class TestCompileRequirementsFromMilestones:
    def test_compile_requirements(self):
        with TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "requirements.yaml"
            milestones = {
                "M1": {
                    "title": "Implement authentication",
                    "priority": "HIGH",
                    "acceptance_criteria": ["User can log in", "User can log out"],
                },
                "M2": {
                    "title": "Add database support",
                    "priority": "MEDIUM",
                    "acceptance_criteria": ["CRUD operations work"],
                },
            }

            ledger = compile_requirements_from_milestones(ledger_path, milestones)

            assert len(ledger.requirements) == 2
            assert ledger.requirements[0].text == "Implement authentication"
            assert ledger.requirements[0].priority == "HIGH"
            assert len(ledger.requirements[0].acceptance_criteria) == 2


class TestValidateRequirementCoverage:
    def test_all_requirements_have_owners(self):
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Test requirement",
            type="FUNCTIONAL",
            priority="HIGH",
            work_order_ids=["WO-1"],
        ))

        passed, issues = validate_requirement_coverage(ledger, ["WO-1"])
        assert passed
        assert len(issues) == 0

    def test_requirement_without_owner_or_blocker(self):
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Orphan requirement",
            type="FUNCTIONAL",
            priority="HIGH",
        ))

        passed, issues = validate_requirement_coverage(ledger, ["WO-1"])
        assert not passed
        assert "R001" in issues[0]

    def test_requirement_with_blocker(self):
        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Blocked requirement",
            type="FUNCTIONAL",
            priority="HIGH",
            blocker_ids=["WO-2"],
        ))

        passed, issues = validate_requirement_coverage(ledger, ["WO-1", "WO-2"])
        assert passed


class TestMilestoneIdField:
    def test_requirement_with_milestone_id(self):
        req = Requirement(
            id="R001",
            text="Test",
            type="FUNCTIONAL",
            priority="HIGH",
            milestone_id="M1",
        )
        assert req.milestone_id == "M1"

        d = req.to_dict()
        assert d["milestone_id"] == "M1"

    def test_requirement_from_dict_with_milestone_id(self):
        data = {
            "id": "R001",
            "text": "Test",
            "type": "FUNCTIONAL",
            "milestone_id": "M1",
        }
        req = Requirement.from_dict(data)
        assert req.milestone_id == "M1"
