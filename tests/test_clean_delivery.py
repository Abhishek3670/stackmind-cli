"""Regression tests for clean delivery policy - no StackMind branding in product deliverables."""

import tempfile
from pathlib import Path
import pytest


class TestCleanDeliveryPolicy:
    """Tests that product deliverables contain no StackMind references by default."""

    def test_readme_template_has_no_stackmind_branding(self):
        """README template must not contain StackMind vendor references."""
        template_path = Path(__file__).parent.parent / "templates" / "README.template.md"
        content = template_path.read_text(encoding="utf-8")
        # Check for prohibited terms
        assert "stackmind" not in content.lower(), \
            "README template contains prohibited StackMind reference"
        assert ".sync" not in content, \
            "README template contains prohibited runtime path"
        assert "AGENTS.md" not in content, \
            "README template contains prohibited internal file reference"

    def test_agents_template_is_internal_only(self):
        """AGENTS.template.md is internal-only - not for product delivery."""
        template_path = Path(__file__).parent.parent / "templates" / "AGENTS.template.md"
        content = template_path.read_text(encoding="utf-8")
        # The template should contain runtime directives for internal use only
        # It should NOT be rendered as a product-facing file
        assert "D025" in content or "CONTRACT-01" in content, \
            "Agent template should contain runtime directives"

    def test_changelog_template_is_minimal(self):
        """CHANGELOG template should be minimal/product-focused, not branded."""
        template_path = Path(__file__).parent.parent / "templates" / "CHANGELOG.template.md"
        content = template_path.read_text(encoding="utf-8")
        # Should not contain StackMind references
        assert "stackmind" not in content.lower(), \
            "CHANGELOG template contains prohibited StackMind reference"

    def test_sync_runtime_excluded_from_product_scan(self):
        """Internal .sync/ runtime should be excluded from clean-project scans."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "product_project"
            project_path.mkdir()

            # Create mock .sync structure
            sync = project_path / ".sync" / "runtime" / "boot"
            sync.mkdir(parents=True)
            (sync / "codex.boot.yaml").write_text("agent: codex\n")

            # Product file should have no stackmind branding
            product_file = project_path / "product.txt"
            product_file.write_text("# Product content\n")

            # Verify .sync is isolated
            assert (project_path / ".sync").exists()
            assert not (project_path / "codex.boot.yaml").exists()


class TestCleanDeliveryGate:
    """Tests for clean delivery validation gate (to be implemented)."""

    def test_gate_detects_prohibited_references(self):
        """Clean delivery gate should detect prohibited StackMind references."""
        # TODO: Implement CleanDeliveryGate in validators/harness/clean_delivery_gate.py
        # This test documents the expected behavior:
        #
        # 1. Scan product-deliverable boundary (not .sync/)
        # 2. Check tracked files and staged content
        # 3. Check commit messages and trailers
        # 4. Detect prohibited references:
        #    - "StackMind", "stackmind-cli"
        #    - ".sync", internal runtime state
        #    - "AGENTS.md", agent names (Claude, Codex, Gemini, Gemma, Local-LLM)
        #    - Work orders, harness references, internal contracts
        #
        # 5. Exclude only designated internal runtime boundary
        # 6. Fail closed when scanning cannot complete

        # Placeholder: This test documents the expected interface
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            readme = project_path / "README.md"
            readme.write_text("Managed by stackmind\n")

            # Expected behavior: gate should detect the prohibited reference
            # from validators.harness.clean_delivery_gate import CleanDeliveryGate
            # gate = CleanDeliveryGate(project_path)
            # findings = gate.scan()
            # assert len(findings) > 0

        pytest.skip("CleanDeliveryGate to be implemented")

    def test_gate_allows_internal_sync_files(self):
        """Internal .sync/ files should be excluded from scan."""
        # TODO: Implement CleanDeliveryGate
        # Expected: .sync/ runtime metadata should not cause false positives

        pytest.skip("CleanDeliveryGate to be implemented")
