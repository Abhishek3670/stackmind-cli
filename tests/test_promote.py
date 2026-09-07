"""Tests for stackmind promote (CLAUDE-01 draft-promotion gate)."""

from pathlib import Path

import pytest
import yaml

from cli.init import init
from cli.promote import (
    canonical_path,
    draft_path,
    promote_draft,
    validate_boot_text,
)


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


@pytest.fixture
def sync_path(fresh_project):
    return fresh_project / ".sync"


def _valid_draft(agent="codex", session_count=5):
    return yaml.dump({
        "schema_version": 1,
        "agent": agent,
        "session_count": session_count,
        "next_action": "Resume work",
        "tree_version": 3,
    })


def _write_draft(sync_path, agent, text):
    p = draft_path(sync_path, agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ─── validate_boot_text ─────────────────────────────────────────


class TestValidateBootText:
    def test_valid_passes(self):
        assert validate_boot_text(_valid_draft()) == []

    def test_invalid_schema_version(self):
        errors = validate_boot_text(_valid_draft().replace("schema_version: 1", "schema_version: 9"))
        assert any("schema_version" in e for e in errors)

    def test_missing_required_field(self):
        errors = validate_boot_text("schema_version: 1\nagent: codex\n")  # no session_count
        assert any("session_count" in e for e in errors)

    def test_malformed_yaml(self):
        errors = validate_boot_text("not: valid: [")
        assert len(errors) == 1
        assert "Invalid YAML" in errors[0]


# ─── promote_draft ──────────────────────────────────────────────


class TestPromoteDraft:
    def test_no_draft_fails(self, sync_path):
        ok, msg = promote_draft(sync_path, "codex")
        assert not ok
        assert "No draft" in msg

    def test_valid_draft_promotes(self, sync_path):
        _write_draft(sync_path, "codex", _valid_draft(session_count=7))

        ok, msg = promote_draft(sync_path, "codex")
        assert ok

        promoted = yaml.safe_load(canonical_path(sync_path, "codex").read_text())
        assert promoted["session_count"] == 7

    def test_invalid_draft_aborts_without_touching_canonical(self, sync_path):
        # Capture the original canonical content.
        canonical = canonical_path(sync_path, "codex")
        original = canonical.read_text(encoding="utf-8")

        _write_draft(sync_path, "codex", "schema_version: 1\nagent: codex\n")  # missing session_count

        ok, msg = promote_draft(sync_path, "codex")
        assert not ok
        assert "validation failed" in msg
        # Canonical untouched
        assert canonical.read_text(encoding="utf-8") == original

    def test_invalid_draft_writes_blocker(self, sync_path):
        _write_draft(sync_path, "codex", "schema_version: 1\nagent: codex\n")

        ok, msg = promote_draft(sync_path, "codex")
        assert not ok

        blockers = list((sync_path / "inbox" / "claude").glob("*promote-blocked_codex.md"))
        assert len(blockers) == 1
        assert "session_count" in blockers[0].read_text(encoding="utf-8")

    def test_malformed_draft_yaml_aborts(self, sync_path):
        canonical = canonical_path(sync_path, "codex")
        original = canonical.read_text(encoding="utf-8")

        _write_draft(sync_path, "codex", "not: valid: yaml: [")

        ok, msg = promote_draft(sync_path, "codex")
        assert not ok
        assert canonical.read_text(encoding="utf-8") == original

    def test_promoted_canonical_passes_validation(self, sync_path):
        """After a successful promote, the canonical file is schema-valid."""
        _write_draft(sync_path, "gemma", _valid_draft(agent="gemma"))
        ok, _ = promote_draft(sync_path, "gemma")
        assert ok
        assert validate_boot_text(canonical_path(sync_path, "gemma").read_text()) == []



class TestPromoteWritesNormalizationDecision:
    """PLAT-04: a successful promotion records a NORMALIZATION decision."""

    def test_decision_written_on_success(self, sync_path):
        _write_draft(sync_path, "codex", _valid_draft(session_count=8))
        ok, _ = promote_draft(sync_path, "codex")
        assert ok

        decisions = list((sync_path / "decisions").glob("*-normalization.yaml"))
        assert len(decisions) == 1
        data = yaml.safe_load(decisions[0].read_text())
        assert data["type"] == "NORMALIZATION"
        assert data["authored_by"] == "claude"
        assert data["changes"][0]["file"] == "runtime/boot/codex.boot.yaml"

    def test_no_decision_on_failed_promotion(self, sync_path):
        _write_draft(sync_path, "codex", "schema_version: 1\nagent: codex\n")  # invalid
        ok, _ = promote_draft(sync_path, "codex")
        assert not ok
        decisions = list((sync_path / "decisions").glob("*-normalization.yaml"))
        assert len(decisions) == 0

    def test_authored_by_override(self, sync_path):
        _write_draft(sync_path, "gemma", _valid_draft(agent="gemma"))
        ok, _ = promote_draft(sync_path, "gemma", authored_by="ceo")
        assert ok
        decisions = list((sync_path / "decisions").glob("*-normalization.yaml"))
        data = yaml.safe_load(decisions[0].read_text())
        assert data["authored_by"] == "ceo"
