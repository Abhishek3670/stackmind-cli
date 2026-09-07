"""Tests for stackmind doctor command.

Tests M3 acceptance criteria:
- Doctor reports version compatibility
- Doctor summarizes agent state
- Doctor integrates validate results
- Doctor handles missing runtime gracefully
"""

from pathlib import Path

import pytest

from cli.doctor import _check_compatibility, _parse_version, doctor
from cli.init import init


# ─── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def fresh_project(tmp_path):
    """Create a fresh valid STACKMIND project."""
    project = tmp_path / "test-project"
    init(project, name="Doctor Test", no_git=True)
    return project


# ─── Unit Tests: Version Parsing ──────────────────────────────


class TestVersionParsing:
    """Tests for version parsing and compatibility."""

    def test_parse_simple_version(self):
        assert _parse_version("1.0.0") == (1, 0, 0)

    def test_parse_prerelease_version(self):
        assert _parse_version("1.2.3-alpha") == (1, 2, 3)

    def test_parse_build_metadata(self):
        assert _parse_version("2.1.0+build123") == (2, 1, 0)

    def test_compatible_same_major(self):
        assert _check_compatibility("1.0.0", "1.0.0") == "FULL"

    def test_compatible_cli_newer_minor(self):
        assert _check_compatibility("1.0.0", "1.2.0") == "FULL"

    def test_partial_cli_older_minor(self):
        assert _check_compatibility("1.3.0", "1.1.0") == "PARTIAL"

    def test_read_only_cli_newer_major(self):
        assert _check_compatibility("1.0.0", "2.0.0") == "READ_ONLY"
        assert _check_compatibility("1.1.0", "2.0.0") == "READ_ONLY"

    def test_incompatible_cli_older_major(self):
        assert _check_compatibility("2.0.0", "1.0.0") == "INCOMPATIBLE"

    def test_cli_2_compatible_with_runtime_1_2(self):
        # CLI 2.x is fully compatible with Runtime 1.2.x
        assert _check_compatibility("1.2.0", "2.0.0") == "FULL"
        assert _check_compatibility("1.2.5", "2.1.0") == "FULL"


# ─── Integration Tests ────────────────────────────────────────


class TestDoctor:
    """Integration tests for doctor command."""

    def test_doctor_on_fresh_project(self, fresh_project, capsys):
        result = doctor(fresh_project)
        assert result is True

    def test_doctor_on_missing_sync(self, tmp_path, capsys):
        result = doctor(tmp_path)
        assert result is False

    def test_doctor_reports_agent_count(self, fresh_project, capsys):
        doctor(fresh_project)
        captured = capsys.readouterr()
        assert "claude" in captured.out
        assert "codex" in captured.out

    def test_doctor_reports_migration_status(self, fresh_project, capsys):
        doctor(fresh_project)
        captured = capsys.readouterr()
        assert "Migration Status" in captured.out
        assert "Runtime is up to date" in captured.out
