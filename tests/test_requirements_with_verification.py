"""Tests for requirements verification integration."""

import pytest
from pathlib import Path
import yaml


class TestRequirementVerification:
    """Tests for verifying requirements with content-aware checks."""

    def test_requirement_pass_with_verification_details(self, tmp_path):
        """Requirements should store detailed verification results."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Implement login feature",
            type="FUNCTIONAL",
            priority="HIGH",
            verification_details=[
                {"name": "file_exists", "passed": True, "path": "src/auth.py"},
                {"name": "acceptance_check", "passed": True, "criteria": "Must authenticate users"},
            ]
        ))

        req = ledger.get_requirement("R001")
        assert len(req.verification_details) == 2
        assert req.verification_details[0]["passed"]

    def test_requirement_evidence_with_content_hash(self, tmp_path):
        """Requirements should track content hashes for freshness."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Implement login",
            type="FUNCTIONAL",
            priority="HIGH",
            evidence={
                "content_hash": "abc123def456",
                "artifact_path": "src/auth.py",
                "verified_at": "2026-10-10T10:00:00Z",
            }
        ))

        req = ledger.get_requirement("R001")
        assert req.evidence["content_hash"] == "abc123def456"

    def test_verification_evidence_used_for_pass(self, tmp_path):
        """Content-aware verification evidence should satisfy requirements."""
        from validators.harness.requirements import RequirementsLedger, Requirement

        ledger = RequirementsLedger()
        ledger.add_requirement(Requirement(
            id="R001",
            text="Implement feature",
            type="FUNCTIONAL",
            priority="HIGH",
            status="PASS",
            work_order_ids=["WO-001"],
            evidence={"content_hash": "abc123"},
            verification_details=[{"name": "acceptance_check", "passed": True}],
        ))

        # Should pass coverage check
        from validators.harness.requirements import validate_requirements_coverage

        passed, issues = validate_requirements_coverage(ledger, ["WO-001"])
        assert passed


class TestFreshnessAndInvalidation:
    """Tests for verification freshness and invalidation."""

    def test_evidence_invalidated_on_content_change(self):
        """Evidence should be stale when content hash changes."""
        from validators.harness.requirements import Requirement

        req = Requirement(
            id="R001",
            text="Feature",
            type="FUNCTIONAL",
            priority="HIGH",
            evidence={"content_hash": "original_hash"},
        )

        # Current hash doesn't match - evidence is stale
        current_hash = "new_hash"
        stale = req.evidence.get("content_hash") != current_hash
        assert stale

    def test_evidence_valid_when_hash_matches(self):
        """Evidence should be valid when content hash matches."""
        from validators.harness.requirements import Requirement

        req = Requirement(
            id="R001",
            text="Feature",
            type="FUNCTIONAL",
            priority="HIGH",
            evidence={"content_hash": "matching_hash"},
        )

        current_hash = "matching_hash"
        stale = req.evidence.get("content_hash") != current_hash
        assert not stale


class TestAdversarialCases:
    """Tests for adversarial verification scenarios."""

    def test_placeholder_in_source_fails(self, tmp_path):
        """Source file with unresolved placeholder should fail."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "bad.py"
        file_path.write_text("# TODO: Implement this feature\n")

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("placeholder" in c.message.lower() for c in result.checks)

    def test_malformed_json_fails(self, tmp_path):
        """Malformed JSON should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "bad.json"
        file_path.write_text("{invalid json:}")

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("json" in c.message.lower() for c in result.checks)

    def test_whitespace_only_fails(self, tmp_path):
        """Whitespace-only file should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "whitespace.py"
        file_path.write_text("   \n\t\n   ")

        result = verifier.verify(file_path)

        assert not result.passed

    def test_doc_with_placeholder_allowed(self, tmp_path):
        """Placeholders in documentation are allowed."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "readme.md"
        file_path.write_text("# TODO: Add content\n")

        result = verifier.verify(file_path)

        # Placeholder check should not run on .md files
        placeholder_checks = [
            c for c in result.checks
            if "placeholder" in c.name.lower()
        ]
        assert len(placeholder_checks) == 0

    def test_truncated_python_fails(self, tmp_path):
        """Truncated Python file should fail syntax check."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "truncated.py"
        file_path.write_text("def hello():\n    if True:\n")

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("syntax" in c.message.lower() for c in result.checks)

    def test_multiple_acceptance_criteria_partial_failure(self, tmp_path):
        """Multiple criteria with one failure should fail overall."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "test.py"
        file_path.write_text("def authenticate():\n    pass\n")

        result = verifier.verify(
            file_path,
            acceptance_criteria=["Must authenticate", "Must authorize"]
        )

        assert not result.passed
        assert len([c for c in result.checks if "acceptance_criteria" in c.name]) == 2


class TestCleanDeliveryAndVerificationIntegration:
    """Tests that clean delivery and verification work together."""

    def test_clean_delivery_separate_from_verification(self, tmp_path):
        """Clean delivery and verification are independent checks."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate
        from validators.harness.deliverable_verifier import DeliverableVerifier

        # Create a deliverable with StackMind reference (clean delivery violation)
        file_path = tmp_path / "product.py"
        file_path.write_text("# Managed by StackMind\ndef x(): pass\n")

        gate = CleanDeliveryGate(tmp_path)
        passed, findings = gate.validate()

        # Clean delivery should fail
        assert not passed

        # But verification might still pass if content is valid
        verifier = DeliverableVerifier()
        result = verifier.verify(file_path)
        # Verification doesn't check clean delivery
        assert result.checked

    def test_verification_without_clean_delivery_still_blocks(self, tmp_path):
        """Verification failures should block even without clean delivery issues."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        file_path = tmp_path / "bad.py"
        file_path.write_text("# TODO: Implement\n")

        verifier = DeliverableVerifier()
        result = verifier.verify(file_path)

        # Should fail on placeholder
        assert not result.passed
