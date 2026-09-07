"""Tests for stackmind init command.

Tests M1 acceptance criteria:
- stackmind init ./test-project creates valid runtime
- Generated files match examples/minimal structure
"""

import hashlib
import os
import re
from pathlib import Path

import pytest
import yaml

from cli.init import (
    DEFAULT_AGENTS,
    PLACEHOLDER_PATTERN,
    RUNTIME_VERSION,
    compute_protocol_hash,
    create_sync_structure,
    get_templates_dir,
    init,
    render_template_string,
    validate_result,
)


# ─── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path):
    """Return a clean temporary project path."""
    return tmp_path / "test-project"


@pytest.fixture
def templates_dir():
    """Return the actual templates directory."""
    return get_templates_dir()


# ─── Unit Tests: Template Rendering ────────────────────────────


class TestRenderTemplateString:
    """Tests for placeholder substitution."""

    def test_basic_substitution(self):
        result = render_template_string(
            "Hello {{NAME}}, welcome to {{PROJECT}}!",
            {"NAME": "World", "PROJECT": "STACKMIND"},
        )
        assert result == "Hello World, welcome to STACKMIND!"

    def test_no_placeholders(self):
        result = render_template_string("No placeholders here.", {})
        assert result == "No placeholders here."

    def test_unrecognized_placeholder_preserved(self):
        result = render_template_string(
            "Known: {{KNOWN}}, Unknown: {{UNKNOWN}}",
            {"KNOWN": "yes"},
        )
        assert result == "Known: yes, Unknown: {{UNKNOWN}}"

    def test_placeholder_pattern_matches(self):
        matches = PLACEHOLDER_PATTERN.findall("{{A}} and {{B_C}}")
        assert matches == ["A", "B_C"]

    def test_empty_context(self):
        result = render_template_string("{{FOO}}", {})
        assert result == "{{FOO}}"

    def test_multiline(self):
        template = "line1: {{A}}\nline2: {{B}}\nline3: plain"
        result = render_template_string(template, {"A": "x", "B": "y"})
        assert result == "line1: x\nline2: y\nline3: plain"


# ─── Unit Tests: Protocol Hash ─────────────────────────────────


class TestComputeProtocolHash:
    """Tests for PROTOCOL_DIGEST.hash generation."""

    def test_hash_is_64_char_hex(self, templates_dir):
        protocol_path = templates_dir / "sync" / "PROTOCOL_DIGEST.md"
        h = compute_protocol_hash(protocol_path)
        assert len(h) == 64
        assert re.match(r"^[A-F0-9]{64}$", h)

    def test_hash_is_deterministic(self, templates_dir):
        protocol_path = templates_dir / "sync" / "PROTOCOL_DIGEST.md"
        h1 = compute_protocol_hash(protocol_path)
        h2 = compute_protocol_hash(protocol_path)
        assert h1 == h2

    def test_hash_matches_manual_sha256(self, tmp_path):
        test_file = tmp_path / "test.md"
        test_file.write_bytes(b"Hello, World!")
        computed = compute_protocol_hash(test_file)
        expected = hashlib.sha256(b"Hello, World!").hexdigest().upper()
        assert computed == expected


# ─── Integration Tests: Full Init ──────────────────────────────


class TestInit:
    """Full integration tests for stackmind init."""

    def test_basic_init(self, tmp_project):
        """M1 acceptance: stackmind init ./test-project creates valid runtime."""
        result = init(tmp_project, name="Test Project", no_git=True)
        assert result is True

    def test_creates_agents_md(self, tmp_project):
        """AGENTS.md must exist in project root."""
        init(tmp_project, name="Test Project", no_git=True)
        agents_md = tmp_project / "AGENTS.md"
        assert agents_md.exists()
        content = agents_md.read_text(encoding="utf-8")
        assert "Test Project" in content
        assert "Authority: CEO → Claude → Gemma → Workers" in content

    def test_creates_sync_directory(self, tmp_project):
        """The .sync/ directory must be created."""
        init(tmp_project, name="Test Project", no_git=True)
        assert (tmp_project / ".sync").is_dir()

    def test_creates_protocol_digest_files(self, tmp_project):
        """Both PROTOCOL_DIGEST.md and .hash must exist."""
        init(tmp_project, name="Test Project", no_git=True)
        sync = tmp_project / ".sync"

        assert (sync / "PROTOCOL_DIGEST.md").exists()
        assert (sync / "PROTOCOL_DIGEST.hash").exists()

        # Hash must match digest content
        stored_hash = (sync / "PROTOCOL_DIGEST.hash").read_text(encoding="utf-8").strip()
        computed = compute_protocol_hash(sync / "PROTOCOL_DIGEST.md")
        assert stored_hash == computed

    def test_creates_tree_yaml(self, tmp_project):
        """TREE.yaml must be valid YAML with correct schema."""
        init(tmp_project, name="Test Project", no_git=True)
        tree_path = tmp_project / ".sync" / "runtime" / "TREE.yaml"

        assert tree_path.exists()
        tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))

        assert tree["schema_version"] == 1
        assert tree["tree_version"] == 1
        assert tree["current_phase"] == "INIT"
        assert "agents" in tree
        assert "claude" in tree["agents"]

    def test_creates_boot_snapshots(self, tmp_project):
        """Boot snapshots for all default agents must exist."""
        init(tmp_project, name="Test Project", no_git=True)
        boot_dir = tmp_project / ".sync" / "runtime" / "boot"

        for agent in DEFAULT_AGENTS:
            boot_file = boot_dir / f"{agent}.boot.yaml"
            assert boot_file.exists(), f"Missing boot file for {agent}"

            boot = yaml.safe_load(boot_file.read_text(encoding="utf-8"))
            assert boot["agent"] == agent
            assert boot["session_count"] == 0
            assert boot["schema_version"] == 1

    def test_creates_agent_contracts(self, tmp_project):
        """Agent contract files must exist."""
        init(tmp_project, name="Test Project", no_git=True)
        agents_dir = tmp_project / ".sync" / "agents"

        for agent in DEFAULT_AGENTS:
            assert (agents_dir / f"{agent}.agent.md").exists()

    def test_creates_inbox_outbox(self, tmp_project):
        """Inbox and outbox directories with _read/ subdirs."""
        init(tmp_project, name="Test Project", no_git=True)

        for agent in DEFAULT_AGENTS:
            inbox = tmp_project / ".sync" / "inbox" / agent
            assert inbox.is_dir()
            assert (inbox / "_read").is_dir()

            outbox = tmp_project / ".sync" / "outbox" / agent
            assert outbox.is_dir()

        # CEO inbox with _read/ folder
        ceo_inbox = tmp_project / ".sync" / "inbox" / "CEO"
        assert ceo_inbox.is_dir()
        assert (ceo_inbox / "_read").is_dir()

    def test_creates_work_order_structure(self, tmp_project):
        """Work order directories and INDEX.yaml must exist."""
        init(tmp_project, name="Test Project", no_git=True)
        wo = tmp_project / ".sync" / "work-orders"

        assert (wo / "INDEX.yaml").exists()
        assert (wo / "ACTIVE").is_dir()
        assert (wo / "COMPLETED").is_dir()
        assert (wo / "BLOCKED").is_dir()
        assert (wo / "TEMPLATES").is_dir()

        # INDEX.yaml should have empty orders
        index = yaml.safe_load((wo / "INDEX.yaml").read_text(encoding="utf-8"))
        assert index["orders"] == []
        assert index["next_id"] == 1

    def test_creates_empty_dirs_with_gitkeep(self, tmp_project):
        """Empty directories must have .gitkeep files."""
        init(tmp_project, name="Test Project", no_git=True)
        sync = tmp_project / ".sync"

        gitkeep_dirs = [
            "decisions",
            "reviews",
            "escalations",
            "standup",
            "releases",
            "state",
            "runtime/drafts",
            "runtime/receipts",
        ]

        for d in gitkeep_dirs:
            assert (sync / d / ".gitkeep").exists(), f"Missing .gitkeep in {d}"

    def test_creates_runtime_version(self, tmp_project):
        """RUNTIME_VERSION file must exist with correct version."""
        init(tmp_project, name="Test Project", no_git=True)
        rv_path = tmp_project / ".sync" / "RUNTIME_VERSION"

        assert rv_path.exists()
        rv = yaml.safe_load(rv_path.read_text(encoding="utf-8"))
        assert rv["version"] == RUNTIME_VERSION

    def test_creates_system_context(self, tmp_project):
        """SYSTEM_CONTEXT.md must have project-specific values."""
        init(tmp_project, name="Test Project", no_git=True)
        sc_path = tmp_project / ".sync" / "SYSTEM_CONTEXT.md"

        assert sc_path.exists()
        content = sc_path.read_text(encoding="utf-8")
        assert "Test Project" in content

    def test_creates_readme(self, tmp_project):
        """README.md must have project name."""
        init(tmp_project, name="Test Project", no_git=True)
        readme_path = tmp_project / ".sync" / "README.md"

        assert readme_path.exists()
        content = readme_path.read_text(encoding="utf-8")
        assert "Test Project" in content

    def test_placeholders_fully_resolved(self, tmp_project):
        """No unresolved {{PLACEHOLDER}} should remain in generated files."""
        init(tmp_project, name="Test Project", no_git=True)

        # Walk all text files in .sync/ and check for unresolved placeholders
        sync = tmp_project / ".sync"
        unresolved = []

        for root, _dirs, files in os.walk(sync):
            for f in files:
                fpath = Path(root) / f
                if fpath.suffix in (".yaml", ".yml", ".md"):
                    try:
                        content = fpath.read_text(encoding="utf-8")
                        matches = PLACEHOLDER_PATTERN.findall(content)
                        if matches:
                            rel = fpath.relative_to(sync)
                            unresolved.append((str(rel), matches))
                    except UnicodeDecodeError:
                        pass

        assert unresolved == [], f"Unresolved placeholders: {unresolved}"

    def test_no_template_suffix_in_output(self, tmp_project):
        """Output files should not have .template. in their names."""
        init(tmp_project, name="Test Project", no_git=True)

        for root, _dirs, files in os.walk(tmp_project):
            for f in files:
                assert ".template." not in f, (
                    f"Template suffix found in output: {os.path.join(root, f)}"
                )

    def test_name_defaults_to_directory(self, tmp_project):
        """When no name is given, use directory name."""
        init(tmp_project, no_git=True)
        content = (tmp_project / "AGENTS.md").read_text(encoding="utf-8")
        assert tmp_project.name in content

    def test_rejects_existing_sync(self, tmp_project):
        """Init should refuse if .sync/ already exists."""
        tmp_project.mkdir(parents=True)
        (tmp_project / ".sync").mkdir()

        with pytest.raises(RuntimeError, match="already exists"):
            init(tmp_project, name="Test Project", no_git=True)

    def test_subset_agents(self, tmp_project):
        """Init with a subset of agents should only create those agents."""
        subset = ["claude", "codex"]
        init(tmp_project, name="Test Project", agents=subset, no_git=True)

        boot_dir = tmp_project / ".sync" / "runtime" / "boot"

        # Subset agents should exist
        assert (boot_dir / "claude.boot.yaml").exists()
        assert (boot_dir / "codex.boot.yaml").exists()

        # Non-subset agents should NOT exist
        assert not (boot_dir / "gemini.boot.yaml").exists()
        assert not (boot_dir / "gemma.boot.yaml").exists()
        assert not (boot_dir / "local-llm.boot.yaml").exists()


# ─── Unit Tests: Validation ────────────────────────────────────


class TestValidateResult:
    """Tests for the post-init validation function."""

    def test_valid_project_has_no_errors(self, tmp_project):
        """A freshly initialized project should pass validation."""
        init(tmp_project, name="Test Project", no_git=True)
        errors = validate_result(tmp_project, DEFAULT_AGENTS)
        assert errors == []

    def test_missing_agents_md_detected(self, tmp_project):
        """Validation detects missing AGENTS.md."""
        init(tmp_project, name="Test Project", no_git=True)
        (tmp_project / "AGENTS.md").unlink()
        errors = validate_result(tmp_project, DEFAULT_AGENTS)
        assert any("AGENTS.md" in e for e in errors)

    def test_missing_boot_file_detected(self, tmp_project):
        """Validation detects missing boot snapshots."""
        init(tmp_project, name="Test Project", no_git=True)
        boot_file = tmp_project / ".sync" / "runtime" / "boot" / "codex.boot.yaml"
        boot_file.unlink()
        errors = validate_result(tmp_project, DEFAULT_AGENTS)
        assert any("codex.boot.yaml" in e for e in errors)


# ─── Comparison Test: Match examples/minimal ───────────────────


class TestMatchMinimalExample:
    """Verify generated structure matches examples/minimal."""

    def test_structure_matches_example(self, tmp_project):
        """Generated files should cover the same structure as examples/minimal."""
        init(tmp_project, name="Minimal Example", no_git=True)

        example_dir = get_templates_dir().parent / "examples" / "minimal"
        if not example_dir.exists():
            pytest.skip("examples/minimal not found")

        # Check that all files in example exist in generated output
        for root, _dirs, files in os.walk(example_dir):
            for f in files:
                if f == ".gitkeep":
                    continue
                example_file = Path(root) / f
                rel = example_file.relative_to(example_dir)
                generated_file = tmp_project / rel

                assert generated_file.exists(), (
                    f"Expected file from example not found in generated output: {rel}"
                )

    def test_tree_yaml_schema_matches(self, tmp_project):
        """Generated TREE.yaml should have same keys as example."""
        init(tmp_project, name="Test", no_git=True)

        example_tree = get_templates_dir().parent / "examples" / "minimal" / ".sync" / "runtime" / "TREE.yaml"
        if not example_tree.exists():
            pytest.skip("examples/minimal/TREE.yaml not found")

        example = yaml.safe_load(example_tree.read_text(encoding="utf-8"))
        generated = yaml.safe_load(
            (tmp_project / ".sync" / "runtime" / "TREE.yaml").read_text(encoding="utf-8")
        )

        # Top-level keys should match
        assert set(example.keys()) <= set(generated.keys()), (
            f"Missing keys: {set(example.keys()) - set(generated.keys())}"
        )

class TestGitIsolation:
    """Verify .sync is properly ignored and not tracked by project git repo."""

    def test_init_renders_gitignore(self, tmp_project):
        init(tmp_project, name="Test", no_git=True)
        gi = tmp_project / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        assert ".sync/" in content
