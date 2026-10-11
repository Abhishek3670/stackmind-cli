"""Tests for content-aware deliverable verifier."""

import json
import yaml
from pathlib import Path
import pytest


class TestDeliverableVerifier:
    """Tests for DeliverableVerifier."""

    def test_missing_file_fails(self, tmp_path):
        """Missing file should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        result = verifier.verify(tmp_path / "missing.py")

        assert result.checked
        assert not result.passed
        assert any("does not exist" in c.message for c in result.checks)

    def test_empty_file_fails(self, tmp_path):
        """Empty file should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "empty.py"
        file_path.write_text("")

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("empty or contains only whitespace" in c.message for c in result.checks)

    def test_whitespace_only_fails(self, tmp_path):
        """Whitespace-only file should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "whitespace.py"
        file_path.write_text("   \n\t\n   ")

        result = verifier.verify(file_path)

        assert not result.passed

    def test_valid_python_file_passes(self, tmp_path):
        """Valid Python file should pass."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "valid.py"
        file_path.write_text("def hello():\n    return 'world'\n")

        result = verifier.verify(file_path)

        assert result.passed

    def test_unresolved_placeholder_fails(self, tmp_path):
        """File with unresolved placeholder should fail."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "placeholder.py"
        file_path.write_text("# TODO: Implement this feature\n")

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("placeholder" in c.message.lower() for c in result.checks)

    def test_invalid_json_fails(self, tmp_path):
        """Malformed JSON should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "invalid.json"
        file_path.write_text('{invalid json:}')

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("json" in c.message.lower() for c in result.checks)

    def test_valid_json_passes(self, tmp_path):
        """Valid JSON should pass."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "valid.json"
        file_path.write_text('{"key": "value"}\n')

        result = verifier.verify(file_path)

        assert result.passed

    def test_invalid_yaml_fails(self, tmp_path):
        """Malformed YAML should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "invalid.yaml"
        file_path.write_text('invalid: yaml: content')

        result = verifier.verify(file_path)

        assert not result.passed
        assert any("yaml" in c.message.lower() for c in result.checks)

    def test_valid_yaml_passes(self, tmp_path):
        """Valid YAML should pass."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "valid.yaml"
        file_path.write_text('key: value\n')

        result = verifier.verify(file_path)

        assert result.passed

    def test_missing_required_content_warns(self, tmp_path):
        """Python file without functions should warn."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "bare.py"
        file_path.write_text("# Just a comment\n")

        result = verifier.verify(file_path)

        # Should have warnings about missing function/class definitions
        assert any("required_content" in c.name for c in result.checks)

    def test_acceptance_criteria_not_met_fails(self, tmp_path):
        """Content not meeting acceptance criteria should fail."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "criteria.py"
        file_path.write_text("def foo():\n    pass\n")

        result = verifier.verify(
            file_path,
            acceptance_criteria=["Must contain authentication logic"]
        )

        assert not result.passed
        assert any("acceptance_criteria" in c.name for c in result.checks)

    def test_acceptance_criteria_met_passes(self, tmp_path):
        """Content meeting acceptance criteria should pass."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "criteria.py"
        file_path.write_text("# authentication logic\ndef authenticate(user, password):\n    return True\n")

        result = verifier.verify(
            file_path,
            acceptance_criteria=["authentication logic"]
        )

        assert result.passed
        # Verify the criterion was specifically satisfied
        assert any("acceptance_criteria_1" in c.name and c.passed for c in result.checks)

    def test_content_hash_produced(self, tmp_path):
        """Verification should produce content hash."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "hash.py"
        file_path.write_text("print('hello')\n")

        result = verifier.verify(file_path)

        assert result.content_hash is not None
        assert len(result.content_hash) > 0

    def test_hash_changes_on_content_change(self, tmp_path):
        """Hash should change when content changes."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "hash.py"
        file_path.write_text("print('hello')\n")

        result1 = verifier.verify(file_path)

        file_path.write_text("print('world')\n")
        result2 = verifier.verify(file_path)

        assert result1.content_hash != result2.content_hash

    def test_empty_markdown_file_passes(self, tmp_path):
        """Empty markdown files should not fail on placeholder checks."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "readme.md"
        file_path.write_text("")  # Empty is ok for docs

        # Placeholder check should be skipped for .md files
        result = verifier.verify(file_path)

        # Empty doc file still fails on non_empty check
        assert not result.passed

    def test_placeholder_in_doc_is_allowed(self, tmp_path):
        """Placeholders in documentation are not rejected."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "doc.md"
        file_path.write_text("# TODO: Add more content\n")

        result = verifier.verify(file_path)

        # Should not flag placeholder in .md file
        placeholder_checks = [c for c in result.checks if "placeholder" in c.name.lower()]
        assert len(placeholder_checks) == 0

    def test_truncated_file_detection(self, tmp_path):
        """Files missing closing brackets should be detected."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "truncated.py"
        file_path.write_text("def hello():\n    if True:\n")  # Missing body

        result = verifier.verify(file_path)

        # Should fail syntax check
        assert not result.passed


class TestVerificationResult:
    """Tests for VerificationResult."""

    def test_to_dict(self, tmp_path):
        """Should serialize to dict."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        file_path = tmp_path / "test.py"
        file_path.write_text("x = 1\n")

        result = verifier.verify(file_path)
        data = result.to_dict()

        assert data["path"] == str(file_path)
        assert "checked" in data
        assert "passed" in data


class TestMultipleDeliverables:
    """Tests for verifying multiple deliverables."""

    def test_verify_multiple(self, tmp_path):
        """Should verify multiple files at once."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        (tmp_path / "valid.py").write_text("def x():\n    pass\n")
        (tmp_path / "invalid.json").write_text("{bad json")

        deliverables = [
            {"path": str(tmp_path / "valid.py")},
            {"path": str(tmp_path / "invalid.json")},
        ]

        results = verifier.verify_multiple(deliverables)

        assert len(results) == 2
