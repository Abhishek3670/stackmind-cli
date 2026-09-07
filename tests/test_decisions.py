"""Tests for stackmind decisions (PLAT-04 NORMALIZATION audit entries)."""

from pathlib import Path

import pytest
import yaml

from cli.init import init
from cli.decisions import (
    build_canonical_change,
    next_decision_id,
    write_normalization_decision,
)


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


@pytest.fixture
def sync_path(fresh_project):
    return fresh_project / ".sync"


class TestNextDecisionId:
    def test_first_id_on_empty(self, sync_path):
        assert next_decision_id(sync_path) == "D-001"

    def test_increments_from_existing(self, sync_path):
        (sync_path / "decisions" / "D-007-normalization.yaml").write_text("id: D-007\n", encoding="utf-8")
        assert next_decision_id(sync_path) == "D-008"

    def test_uses_highest(self, sync_path):
        for n in (1, 5, 3):
            (sync_path / "decisions" / f"D-{n:03d}-normalization.yaml").write_text("x", encoding="utf-8")
        assert next_decision_id(sync_path) == "D-006"


class TestWriteNormalizationDecision:
    def test_writes_file_with_expected_fields(self, sync_path):
        change = build_canonical_change(
            "runtime/boot/codex.boot.yaml",
            {"session_count": 4, "tree_version": 46},
            {"session_count": 5, "tree_version": 47},
        )
        dest = write_normalization_decision(
            sync_path,
            authored_by="claude",
            changes=[change],
            reason="Promoted codex draft.",
            session=5,
        )
        assert dest.exists()
        data = yaml.safe_load(dest.read_text())
        assert data["id"] == "D-001"
        assert data["type"] == "NORMALIZATION"
        assert data["authored_by"] == "claude"
        assert data["session"] == 5
        assert data["changes"][0]["file"] == "runtime/boot/codex.boot.yaml"
        assert data["changes"][0]["tree_version_from"] == 46
        assert data["changes"][0]["tree_version_to"] == 47

    def test_sequential_ids(self, sync_path):
        write_normalization_decision(sync_path, "claude", [], "first")
        second = write_normalization_decision(sync_path, "claude", [], "second")
        assert "D-002" in second.name


class TestBuildCanonicalChange:
    def test_only_changed_fields_recorded(self):
        change = build_canonical_change(
            "runtime/boot/codex.boot.yaml",
            {"session_count": 5, "tree_version": 47},
            {"session_count": 6, "tree_version": 47},
        )
        assert change["session_count_from"] == 5
        assert change["session_count_to"] == 6
        # tree_version unchanged -> not recorded
        assert "tree_version_from" not in change

    def test_file_always_present(self):
        change = build_canonical_change("runtime/TREE.yaml", {}, {})
        assert change["file"] == "runtime/TREE.yaml"
