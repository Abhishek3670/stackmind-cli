"""Tests for stackmind lock (PLAT-03 write-lock mechanism)."""

from pathlib import Path

import pytest
import yaml

from cli.init import init
from cli.lock import (
    acquire_lock,
    get_lock_path,
    lock_held_by_other,
    lock_is_malformed,
    read_lock,
    release_lock,
)
from cli.shutdown import shutdown
from cli.validate import Severity, validate


@pytest.fixture
def fresh_project(tmp_path):
    """Create a fresh valid stackmind project."""
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


@pytest.fixture
def sync_path(fresh_project):
    """Return the .sync/ path of the fresh project."""
    return fresh_project / ".sync"


# ─── Lock module: read ──────────────────────────────────────────


class TestReadLock:
    def test_no_lock_returns_none(self, sync_path):
        assert read_lock(sync_path) is None

    def test_fresh_runtime_has_no_lock(self, sync_path):
        assert not get_lock_path(sync_path).exists()

    def test_reads_well_formed_lock(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=30)
        data = read_lock(sync_path)
        assert data is not None
        assert data["held_by"] == "claude"
        assert data["session_id"] == 30
        assert "acquired_at" in data

    def test_malformed_lock_returns_none(self, sync_path):
        get_lock_path(sync_path).write_text("just a string", encoding="utf-8")
        assert read_lock(sync_path) is None

    def test_lock_missing_held_by_is_malformed(self, sync_path):
        get_lock_path(sync_path).write_text("session_id: 5\n", encoding="utf-8")
        assert read_lock(sync_path) is None
        assert lock_is_malformed(sync_path)

    def test_no_lock_is_not_malformed(self, sync_path):
        assert not lock_is_malformed(sync_path)


# ─── Lock module: acquire ───────────────────────────────────────


class TestAcquireLock:
    def test_acquire_on_free_runtime(self, sync_path):
        ok, msg = acquire_lock(sync_path, "claude", session_id=1)
        assert ok
        assert read_lock(sync_path)["held_by"] == "claude"

    def test_reacquire_by_same_agent_succeeds(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = acquire_lock(sync_path, "claude", session_id=2)
        assert ok
        assert read_lock(sync_path)["session_id"] == 2

    def test_acquire_held_by_other_fails(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = acquire_lock(sync_path, "codex", session_id=1)
        assert not ok
        assert "claude" in msg
        # Lock unchanged
        assert read_lock(sync_path)["held_by"] == "claude"

    def test_force_acquire_steals_lock(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = acquire_lock(sync_path, "codex", session_id=2, force=True)
        assert ok
        assert read_lock(sync_path)["held_by"] == "codex"

        # Verify LOCK_STOLEN compliance event was written
        receipts_dir = sync_path / "runtime" / "receipts"
        events = list(receipts_dir.glob("LOCK_STOLEN_codex_*.yaml"))
        assert len(events) == 1
        event_data = yaml.safe_load(events[0].read_text(encoding="utf-8"))
        assert event_data["event_type"] == "LOCK_STOLEN"
        assert event_data["agent"] == "codex"
        assert event_data["session_id"] == 2
        assert event_data["stolen_from"] == "claude"

    def test_acquire_without_runtime_dir_fails(self, tmp_path):
        ok, msg = acquire_lock(tmp_path / "nope" / ".sync", "claude")
        assert not ok


# ─── Lock module: release ───────────────────────────────────────


class TestReleaseLock:
    def test_release_held_lock(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = release_lock(sync_path, "claude")
        assert ok
        assert read_lock(sync_path) is None

    def test_release_when_no_lock_is_noop(self, sync_path):
        ok, msg = release_lock(sync_path, "claude")
        assert ok
        assert "nothing to release" in msg.lower()

    def test_release_held_by_other_fails(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = release_lock(sync_path, "codex")
        assert not ok
        # Lock still held by claude
        assert read_lock(sync_path)["held_by"] == "claude"

    def test_force_release_held_by_other(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        ok, msg = release_lock(sync_path, "codex", force=True)
        assert ok
        assert read_lock(sync_path) is None

    def test_release_cleans_malformed_lock(self, sync_path):
        get_lock_path(sync_path).write_text("garbage", encoding="utf-8")
        ok, msg = release_lock(sync_path, "claude")
        assert ok
        assert not get_lock_path(sync_path).exists()


# ─── Lock module: held_by_other ─────────────────────────────────


class TestLockHeldByOther:
    def test_no_lock(self, sync_path):
        assert not lock_held_by_other(sync_path, "claude")

    def test_held_by_same(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        assert not lock_held_by_other(sync_path, "claude")

    def test_held_by_other(self, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        assert lock_held_by_other(sync_path, "codex")


# ─── Shutdown integration ───────────────────────────────────────


class TestShutdownReleasesLock:
    def _write_handoff(self, sync_path, agent):
        outbox = sync_path / "outbox" / agent
        (outbox / "handoff-2026-06-18T12-00-00.md").write_text(
            "# Handoff", encoding="utf-8"
        )

    def test_shutdown_releases_own_lock(self, fresh_project, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        self._write_handoff(sync_path, "claude")

        assert shutdown(fresh_project, "claude", force=False)
        assert read_lock(sync_path) is None

    def test_shutdown_leaves_other_agents_lock(self, fresh_project, sync_path):
        acquire_lock(sync_path, "codex", session_id=1)
        self._write_handoff(sync_path, "claude")

        assert shutdown(fresh_project, "claude", force=False)
        # codex's lock must remain
        assert read_lock(sync_path)["held_by"] == "codex"

    def test_force_shutdown_steals_other_lock(self, fresh_project, sync_path):
        acquire_lock(sync_path, "codex", session_id=1)

        assert shutdown(fresh_project, "claude", force=True)
        assert read_lock(sync_path) is None

    def test_shutdown_without_lock_succeeds(self, fresh_project, sync_path):
        self._write_handoff(sync_path, "claude")
        assert shutdown(fresh_project, "claude", force=False)
        assert read_lock(sync_path) is None


# ─── Validate integration ───────────────────────────────────────


class TestValidateLock:
    def test_valid_held_lock_no_issue(self, fresh_project, sync_path):
        acquire_lock(sync_path, "claude", session_id=1)
        result = validate(fresh_project)
        lock_issues = [i for i in result.issues if "LOCK" in i.message]
        assert len(lock_issues) == 0

    def test_malformed_lock_is_error(self, fresh_project, sync_path):
        get_lock_path(sync_path).write_text("not a mapping", encoding="utf-8")
        result = validate(fresh_project)
        lock_issues = [i for i in result.issues if "LOCK" in i.message and "malformed" in i.message]
        assert len(lock_issues) == 1
        assert lock_issues[0].severity == Severity.ERROR

    def test_unknown_holder_is_warning(self, fresh_project, sync_path):
        acquire_lock(sync_path, "ghost-agent", session_id=1)
        result = validate(fresh_project)
        lock_issues = [i for i in result.issues if "unknown agent" in i.message]
        assert len(lock_issues) == 1
        assert lock_issues[0].severity == Severity.WARN

    def test_no_lock_no_issue(self, fresh_project):
        result = validate(fresh_project)
        lock_issues = [i for i in result.issues if "LOCK" in i.message]
        assert len(lock_issues) == 0
