"""Tests for stackmind shutdown command."""

from pathlib import Path

import pytest
import yaml

from cli.init import init
from cli.shutdown import (
    archive_handoff,
    find_handoff_report,
    fresh_tree_versions,
    shutdown,
    unprocessed_inbox_items,
    update_boot_snapshot,
    update_tree_status,
)


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


def _session_receipts(sync_path, agent):
    return sorted((sync_path / "runtime" / "receipts").glob(f"{agent}-session-*.yaml"))


class TestShutdownHelpers:
    """Tests for shutdown helper functions."""

    def test_find_handoff_report_none(self, sync_path):
        """Should return None when no handoff report exists."""
        outbox = sync_path / "outbox" / "claude"
        result = find_handoff_report(outbox)
        assert result is None

    def test_find_handoff_report_exists(self, sync_path):
        """Should find handoff report when it exists."""
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text("# Handoff Report\n\nSession complete.", encoding="utf-8")

        result = find_handoff_report(outbox)
        assert result is not None
        assert result.name == "handoff-2026-05-24T12-00-00.md"

    def test_find_handoff_report_most_recent(self, sync_path):
        """Should return most recent handoff report."""
        outbox = sync_path / "outbox" / "claude"
        (outbox / "handoff-2026-05-24T10-00-00.md").write_text("old", encoding="utf-8")
        (outbox / "handoff-2026-05-24T12-00-00.md").write_text("new", encoding="utf-8")

        result = find_handoff_report(outbox)
        assert result.name == "handoff-2026-05-24T12-00-00.md"

    def test_update_tree_status(self, sync_path):
        """Should update agent status to idle."""
        assert update_tree_status(sync_path, "claude")

        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert tree["agents"]["claude"]["status"] == "idle"

    def test_update_tree_status_invalid_agent(self, sync_path):
        """Should return False for non-existent agent."""
        assert not update_tree_status(sync_path, "nonexistent")

    def test_update_boot_snapshot(self, sync_path):
        """Should increment session_count."""
        boot_path = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        boot = yaml.safe_load(boot_path.read_text())
        original_count = boot.get("session_count", 0)

        assert update_boot_snapshot(sync_path, "claude")

        boot = yaml.safe_load(boot_path.read_text())
        assert boot["session_count"] == original_count + 1

    def test_archive_handoff(self, sync_path):
        """Should move handoff to _read/ directory."""
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text("# Handoff", encoding="utf-8")

        archive_handoff(handoff, outbox)

        assert not handoff.exists()
        assert (outbox / "_read" / "handoff-2026-05-24T12-00-00.md").exists()


class TestShutdownCommand:
    """Tests for the shutdown command."""

    def test_shutdown_without_handoff_fails(self, fresh_project, sync_path):
        """Should fail when no handoff report exists."""
        result = shutdown(fresh_project, "claude", force=False)
        assert result is False
        receipts = _session_receipts(sync_path, "claude")
        assert len(receipts) == 1
        receipt = yaml.safe_load(receipts[0].read_text())
        assert receipt["outcome"] == "error"
        assert receipt["handoff_path"] is None

    def test_shutdown_with_handoff_succeeds(self, fresh_project, sync_path):
        """Should succeed when handoff report exists."""
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text("# Handoff Report\n\nSession complete.", encoding="utf-8")
        session_id = (
            yaml.safe_load((sync_path / "runtime" / "boot" / "claude.boot.yaml").read_text())[
                "session_count"
            ]
            + 1
        )

        result = shutdown(fresh_project, "claude", force=False)
        assert result is True

        # Verify state changes
        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert tree["agents"]["claude"]["status"] == "idle"

        # Verify handoff archived
        assert not handoff.exists()
        assert (outbox / "_read" / "handoff-2026-05-24T12-00-00.md").exists()
        receipts = _session_receipts(sync_path, "claude")
        assert len(receipts) == 1
        receipt = yaml.safe_load(receipts[0].read_text())
        assert receipt["agent"] == "claude"
        assert receipt["session_id"] == session_id
        assert receipt["outcome"] == "clean"
        assert receipt["handoff_path"] == "outbox/claude/_read/handoff-2026-05-24T12-00-00.md"
        assert receipt["commit_sha"] is None
        assert receipt["tree_version"] == yaml.safe_load(
            (sync_path / "runtime" / "TREE.yaml").read_text()
        )["tree_version"]
        assert receipts[0].name.startswith(f"claude-session-{session_id}-")

    def test_shutdown_force_without_handoff(self, fresh_project, sync_path):
        """--force should allow shutdown without handoff."""
        result = shutdown(fresh_project, "claude", force=True)
        assert result is True

        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert tree["agents"]["claude"]["status"] == "idle"

    def test_shutdown_invalid_agent(self, fresh_project):
        """Should fail for non-existent agent."""
        result = shutdown(fresh_project, "nonexistent", force=True)
        assert result is False

    def test_shutdown_no_sync_fails(self, tmp_path):
        """Should fail if no .sync/ directory."""
        result = shutdown(tmp_path, "claude", force=True)
        assert result is False

    def test_shutdown_increments_session_count(self, fresh_project, sync_path):
        """Should increment session_count in boot snapshot."""
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text("# Handoff", encoding="utf-8")

        boot_path = sync_path / "runtime" / "boot" / "claude.boot.yaml"
        boot_before = yaml.safe_load(boot_path.read_text())
        count_before = boot_before.get("session_count", 0)

        shutdown(fresh_project, "claude", force=False)

        boot_after = yaml.safe_load(boot_path.read_text())
        assert boot_after["session_count"] == count_before + 1



class TestFreshTreeReread:
    """Tests for CODEX-01: snapshot writes re-read TREE.yaml fresh."""

    def _set_tree(self, sync_path, tree_version=None, graph_version="__keep__"):
        tree_path = sync_path / "runtime" / "TREE.yaml"
        data = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
        if tree_version is not None:
            data["tree_version"] = tree_version
        if graph_version != "__keep__":
            data["graph_version"] = graph_version
        tree_path.write_text(yaml.dump(data), encoding="utf-8")

    def _set_boot(self, sync_path, agent, tree_version=None, graph_version="__keep__"):
        boot_path = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
        data = yaml.safe_load(boot_path.read_text(encoding="utf-8"))
        if tree_version is not None:
            data["tree_version"] = tree_version
        if graph_version != "__keep__":
            data["graph_version"] = graph_version
        boot_path.write_text(yaml.dump(data), encoding="utf-8")

    def test_fresh_tree_versions_reads_current(self, sync_path):
        """Helper returns the current TREE.yaml versions at call time."""
        self._set_tree(sync_path, tree_version=47)
        tv, gv = fresh_tree_versions(sync_path)
        assert tv == 47
        assert gv is None

    def test_fresh_tree_versions_missing_tree(self, sync_path):
        """Helper returns (None, None) when TREE.yaml is absent."""
        (sync_path / "runtime" / "TREE.yaml").unlink()
        assert fresh_tree_versions(sync_path) == (None, None)

    def test_update_boot_syncs_stale_tree_version(self, sync_path):
        """A boot snapshot stale at version 1 is synced to current TREE."""
        self._set_tree(sync_path, tree_version=47)
        self._set_boot(sync_path, "gemma", tree_version=1)

        assert update_boot_snapshot(sync_path, "gemma")

        boot = yaml.safe_load(
            (sync_path / "runtime" / "boot" / "gemma.boot.yaml").read_text()
        )
        assert boot["tree_version"] == 47

    def test_update_boot_syncs_graph_version(self, sync_path):
        """graph_version is synced from the fresh TREE read."""
        graph_hash = "a" * 64
        self._set_tree(sync_path, tree_version=10, graph_version=graph_hash)
        self._set_boot(sync_path, "claude", graph_version=None)

        assert update_boot_snapshot(sync_path, "claude")

        boot = yaml.safe_load(
            (sync_path / "runtime" / "boot" / "claude.boot.yaml").read_text()
        )
        assert boot["graph_version"] == graph_hash

    def test_update_boot_still_increments_session_count(self, sync_path):
        """Fresh re-read does not interfere with session_count increment."""
        self._set_tree(sync_path, tree_version=5)
        boot_path = sync_path / "runtime" / "boot" / "codex.boot.yaml"
        before = yaml.safe_load(boot_path.read_text()).get("session_count", 0)

        assert update_boot_snapshot(sync_path, "codex")

        after = yaml.safe_load(boot_path.read_text())["session_count"]
        assert after == before + 1

    def test_shutdown_leaves_no_version_lag(self, fresh_project, sync_path):
        """After shutdown the snapshot matches TREE (GEMMA-01 lag eliminated)."""
        self._set_tree(sync_path, tree_version=47)
        self._set_boot(sync_path, "gemma", tree_version=1)

        outbox = sync_path / "outbox" / "gemma"
        (outbox / "handoff-2026-06-18T12-00-00.md").write_text("# Handoff", encoding="utf-8")

        assert shutdown(fresh_project, "gemma", force=False)

        boot = yaml.safe_load(
            (sync_path / "runtime" / "boot" / "gemma.boot.yaml").read_text()
        )
        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert boot["tree_version"] == tree["tree_version"] == 47

    def test_update_boot_missing_tree_preserves_version(self, sync_path):
        """When TREE.yaml is unreadable, the snapshot's version is preserved."""
        self._set_boot(sync_path, "claude", tree_version=12)
        (sync_path / "runtime" / "TREE.yaml").unlink()

        assert update_boot_snapshot(sync_path, "claude")

        boot = yaml.safe_load(
            (sync_path / "runtime" / "boot" / "claude.boot.yaml").read_text()
        )
        assert boot["tree_version"] == 12


class TestUnprocessedInboxGate:
    """Tests for GEMMA-02: shutdown requires a drained inbox."""

    def _add_handoff(self, sync_path, agent):
        (sync_path / "outbox" / agent / "handoff-2026-06-18T12-00-00.md").write_text(
            "# Handoff", encoding="utf-8"
        )

    def _add_inbox_item(self, sync_path, agent, name):
        (sync_path / "inbox" / agent / name).write_text("message", encoding="utf-8")

    def test_fresh_inbox_has_no_unprocessed(self, sync_path):
        assert unprocessed_inbox_items(sync_path, "claude") == []

    def test_gitkeep_not_counted(self, sync_path):
        # .gitkeep is present in a fresh inbox and must be ignored.
        assert unprocessed_inbox_items(sync_path, "claude") == []

    def test_read_dir_not_counted(self, sync_path):
        # A processed message in _read/ must not count as unprocessed.
        (sync_path / "inbox" / "claude" / "_read" / "old-msg.md").write_text(
            "done", encoding="utf-8"
        )
        assert unprocessed_inbox_items(sync_path, "claude") == []

    def test_top_level_message_counted(self, sync_path):
        self._add_inbox_item(sync_path, "claude", "2026-06-18_review.md")
        items = unprocessed_inbox_items(sync_path, "claude")
        assert len(items) == 1
        assert items[0].name == "2026-06-18_review.md"

    def test_shutdown_blocked_by_unprocessed_inbox(self, fresh_project, sync_path):
        self._add_handoff(sync_path, "claude")
        self._add_inbox_item(sync_path, "claude", "2026-06-18_review.md")

        # Handoff exists, so only the inbox gate should block shutdown.
        assert shutdown(fresh_project, "claude", force=False) is False

        # State unchanged: agent still not idle-via-shutdown.
        tree = yaml.safe_load((sync_path / "runtime" / "TREE.yaml").read_text())
        assert tree["agents"]["claude"]["last_task"] != "Session ended via shutdown"

    def test_force_bypasses_inbox_gate(self, fresh_project, sync_path):
        self._add_handoff(sync_path, "claude")
        self._add_inbox_item(sync_path, "claude", "2026-06-18_review.md")

        assert shutdown(fresh_project, "claude", force=True) is True

    def test_defer_handles_unprocessed_inbox(self, fresh_project, sync_path):
        self._add_handoff(sync_path, "claude")
        self._add_inbox_item(sync_path, "claude", "2026-06-18_review.md")

        assert shutdown(fresh_project, "claude", force=False, defer=True) is True

        # Verify item moved to _deferred
        deferred_file = sync_path / "inbox" / "claude" / "_deferred" / "2026-06-18_review.md"
        assert deferred_file.exists()

        # Verify receipt stub written
        receipts = list((sync_path / "runtime" / "receipts").glob("*_2026-06-18_review.md.receipt.yaml"))
        assert len(receipts) == 1
        receipt_data = yaml.safe_load(receipts[0].read_text())
        assert receipt_data["deferred_by"] == "claude"

        session_receipts = _session_receipts(sync_path, "claude")
        assert len(session_receipts) == 1
        session_receipt = yaml.safe_load(session_receipts[0].read_text())
        assert session_receipt["outcome"] == "deferred"
        assert session_receipt["handoff_path"] == "outbox/claude/_read/handoff-2026-06-18T12-00-00.md"

    def test_shutdown_succeeds_with_drained_inbox(self, fresh_project, sync_path):
        self._add_handoff(sync_path, "claude")
        # No top-level inbox items.
        assert shutdown(fresh_project, "claude", force=False) is True

    def test_shutdown_validates_handoff_preflight(self, fresh_project, sync_path):
        """Malformed handoff report should fail shutdown unless forced."""
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text(
            "📋 MY NEXT TASKS (when I resume):\n"
            "- WO-001 - finish tasks without source\n",
            encoding="utf-8"
        )
        # Shutdown without force should fail
        assert shutdown(fresh_project, "claude", force=False) is False
        assert handoff.exists()
        
        # Shutdown with force should succeed
        assert shutdown(fresh_project, "claude", force=True) is True

    def test_shutdown_syncs_work_order_totals(self, fresh_project, sync_path):
        """Shutdown should automatically sync TREE.yaml work_orders totals from INDEX.yaml."""
        tree_path = sync_path / "runtime" / "TREE.yaml"
        index_path = sync_path / "work-orders" / "INDEX.yaml"
        
        tree_data = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
        index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
        
        tree_data["work_orders"]["total_completed"] = 10
        index_data["total_completed"] = 25
        tree_path.write_text(yaml.dump(tree_data), encoding="utf-8")
        index_path.write_text(yaml.dump(index_data), encoding="utf-8")
        
        outbox = sync_path / "outbox" / "claude"
        handoff = outbox / "handoff-2026-05-24T12-00-00.md"
        handoff.write_text("# Clean handoff", encoding="utf-8")
        
        assert shutdown(fresh_project, "claude", force=False) is True
        tree_after = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
        assert tree_after["work_orders"]["total_completed"] == 25
