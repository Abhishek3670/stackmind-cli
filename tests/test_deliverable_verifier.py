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


class TestHtmlMediaAssetVerification:
    """Tests for HTML media asset verification in DeliverableVerifier."""

    def test_html_with_broken_local_image_fails(self, tmp_path):
        """Referencing a non-existent local image should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Product</h1>
<img src="assets/non_existent_bag.jpg" alt="Handmade bag">
</body></html>""")

        result = verifier.verify(html_file)
        assert not result.passed
        assert any("Referenced local media file does not exist" in c.message for c in result.checks)

    def test_html_with_existing_local_image_passes(self, tmp_path):
        """Referencing an existing local image file should pass verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        img_dir = tmp_path / "assets"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "bag.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")

        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Product</h1>
<img src="assets/bag.jpg" alt="Handmade bag">
</body></html>""")

        result = verifier.verify(html_file)
        assert result.passed

    def test_html_with_search_url_as_image_fails(self, tmp_path):
        """Using a search page or gallery URL as an img src should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Gallery</h1>
<img src="https://unsplash.com/s/photos/crochet-bag" alt="Search page mistakenly in img src">
</body></html>""")

        result = verifier.verify(html_file)
        assert not result.passed
        assert any("Unsplash search gallery page used as image source" in c.message for c in result.checks)

    def test_html_with_empty_or_placeholder_src_fails(self, tmp_path):
        """Empty or placeholder src should fail verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Hero</h1>
<img src="#" alt="placeholder">
</body></html>""")

        result = verifier.verify(html_file)
        assert not result.passed
        assert any("placeholder media src" in c.message.lower() for c in result.checks)

    def test_html_with_valid_remote_image_passes(self, tmp_path):
        """Valid remote CDN image URL should pass verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Hero</h1>
<img src="https://images.unsplash.com/photo-1584992236310-6edddc08acff?auto=format&fit=crop&w=800" alt="Crochet bag">
</body></html>""")

        result = verifier.verify(html_file)
        assert result.passed
        assert any("Verified 1 media/image asset references" in c.message for c in result.checks)

    def test_html_with_data_uri_image_passes(self, tmp_path):
        """Valid data URI image should pass verification."""
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Icon</h1>
<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" alt="Pixel">
</body></html>""")

        result = verifier.verify(html_file)
        assert result.passed

    def test_html_with_remote_404_image_fails(self, tmp_path):
        """Remote image returning 404 Not Found should fail verification."""
        from unittest.mock import patch
        import urllib.error
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Hero</h1>
<img src="https://images.unsplash.com/photo-hallucinated-fake-id" alt="Fake photo">
</body></html>""")

        err = urllib.error.HTTPError("https://images.unsplash.com/photo-hallucinated-fake-id", 404, "Not Found", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            result = verifier.verify(html_file)

        assert not result.passed
        assert any("Remote media asset does not exist (HTTP 404 Not Found)" in c.message for c in result.checks)

    def test_html_with_remote_html_document_image_fails(self, tmp_path):
        """Remote image URL returning an HTML page instead of an image should fail verification."""
        from unittest.mock import patch, MagicMock
        from validators.harness.deliverable_verifier import DeliverableVerifier

        verifier = DeliverableVerifier()
        html_file = tmp_path / "index.html"
        html_file.write_text("""<!DOCTYPE html>
<html><body>
<h1>Hero</h1>
<img src="https://example.com/not-an-image" alt="Not an image">
</body></html>""")

        mock_resp = MagicMock()
        mock_resp.headers.get_content_type.return_value = "text/html"
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = verifier.verify(html_file)

        assert not result.passed
        assert any("Remote URL returned HTML document instead of an image asset" in c.message for c in result.checks)


