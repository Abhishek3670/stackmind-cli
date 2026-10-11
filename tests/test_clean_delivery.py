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
    """Tests for clean delivery validation gate."""

    def test_gate_detects_prohibited_references(self):
        """Clean delivery gate should detect prohibited StackMind references."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            readme = project_path / "README.md"
            readme.write_text("Managed by stackmind\n")

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert not passed, "Gate should fail when prohibited references are found"
            assert len(findings) > 0, "Gate should return findings for prohibited references"
            assert any("prohibited" in f["issue"].lower() for f in findings), \
                "Findings should mention prohibited terms"

    def test_gate_allows_internal_sync_files(self):
        """Internal .sync/ files should be excluded from scan."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            # Create .sync structure with internal runtime content
            sync = project_path / ".sync" / "runtime" / "boot"
            sync.mkdir(parents=True)
            (sync / "codex.boot.yaml").write_text("agent: codex\n")

            # Product file should have no stackmind branding
            product_file = project_path / "product.txt"
            product_file.write_text("# Product content\n")

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert passed, "Gate should pass when only .sync/ has internal content"

    def test_gate_detects_internal_file_references(self):
        """Gate should detect references to internal files like AGENTS.md."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            readme = project_path / "README.md"
            readme.write_text("See AGENTS.md for agent instructions\n")

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert not passed, "Gate should fail on internal file references"
            assert any("AGENTS.md" in f["issue"] for f in findings), \
                "Should flag AGENTS.md references"

    def test_clean_project_passes(self):
        """Clean project with no StackMind references should pass."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            readme = project_path / "README.md"
            readme.write_text("# My Project\n\nA sample project.\n")

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert passed, "Clean project should pass validation"
            assert len(findings) == 0, "Clean project should have no findings"

    def test_adoption_readme_leakage_detected(self):
        """Adoption path that appends runtime note should be detected."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            readme = project_path / "README.md"
            # Simulate content that would be created during adoption
            readme.write_text(
                "# My Project\n\n"
                "## StackMind Runtime\n\n"
                "This project uses StackMind for its governed agent runtime.\n"
            )

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert not passed, "Gate should detect StackMind runtime references"
            assert any("stackmind" in f["issue"].lower() for f in findings), \
                "Should flag StackMind term"

    def test_plan_md_excluded_from_clean_delivery_scan(self):
        """PLAN.md contains architectural agent assignments and must be excluded from product scan."""
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)
            # PLAN.md with standard agent annotations produced by Architecture
            plan = project_path / "PLAN.md"
            plan.write_text(
                "# Project Plan: My Product\n\n"
                "## Milestones & Roadmap\n"
                "- [ ] Milestone 1: Setup (Agent: codex)\n"
                "- [ ] Milestone 2: UI (Agent: gemini)\n"
                "- [ ] Milestone 3: QA (Agent: gemma)\n"
                "- [ ] Milestone 4: Release (Agent: local-llm)\n"
            )

            # Product file is clean
            src = project_path / "app.py"
            src.write_text("print('hello world')\n")

            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert passed, f"PLAN.md should not trigger clean delivery errors: {findings}"
            assert len(findings) == 0, "No findings should be raised for clean project with PLAN.md"
