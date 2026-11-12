"""Tests for DaemonStorage atomic replacement, crash safety, and corruption recovery."""

import json
from unittest.mock import patch
import pytest

from validators.kernel.daemon.storage import DaemonStorage


def test_failed_replace_preserves_existing_state_and_does_not_truncate(tmp_path):
    """Verify that if atomic replace fails, existing state file is NOT truncated."""
    storage = DaemonStorage(tmp_path)
    initial_state = {
        "sessions": {"s1": {"state": "RUNNING", "agent": "claude"}},
        "events": [{"sequence": 1, "name": "turn.started"}],
    }
    storage.save(initial_state)

    # Verify initial file is written
    assert storage.path.exists()
    assert storage.path.stat().st_size > 0

    new_state = {
        "sessions": {"s1": {"state": "COMPLETED", "agent": "claude"}},
        "events": [{"sequence": 2, "name": "turn.completed"}],
    }

    # Simulate continuous Windows file contention / lock on os.replace
    with patch("os.replace", side_effect=PermissionError("Simulated file lock")):
        with pytest.raises(OSError) as exc_info:
            storage.save(new_state)
        assert "Failed to atomically replace" in str(exc_info.value)

    # CRITICAL: Existing file must remain completely intact and NOT truncated to 0 bytes
    assert storage.path.exists()
    assert storage.path.stat().st_size > 0
    loaded = storage.load()
    assert loaded["sessions"]["s1"]["state"] == "RUNNING"
    assert loaded["events"][0]["name"] == "turn.started"

    # Pending state must be preserved in .pending.json
    assert storage.pending_path.exists()
    pending_data = json.loads(storage.pending_path.read_text(encoding="utf-8"))
    assert pending_data["sessions"]["s1"]["state"] == "COMPLETED"


def test_load_recovers_from_backup_when_primary_is_corrupt(tmp_path):
    """Verify load() falls back to .json.bak when primary file is truncated or corrupted."""
    storage = DaemonStorage(tmp_path)
    valid_state = {
        "sessions": {"s2": {"state": "RUNNING", "agent": "codex"}},
        "events": [],
    }
    storage.save(valid_state)

    # Save once more so backup copy is populated
    valid_state["sessions"]["s2"]["state"] = "PAUSED"
    storage.save(valid_state)
    assert storage.backup_path.exists()

    # Simulate crash truncation of primary file to 0 bytes
    storage.path.write_bytes(b"")

    recovered = storage.load()
    assert "s2" in recovered["sessions"]
    # Primary path must be restored from backup
    assert storage.path.stat().st_size > 0


def test_load_recovers_from_pending_when_primary_missing(tmp_path):
    """Verify load() recovers from .pending.json when primary file is missing."""
    storage = DaemonStorage(tmp_path)
    pending_state = {
        "sessions": {"s3": {"state": "ACTIVE"}},
        "events": [],
    }
    storage.root.mkdir(parents=True, exist_ok=True)
    storage.pending_path.write_text(json.dumps(pending_state), encoding="utf-8")

    assert not storage.path.exists()
    recovered = storage.load()
    assert recovered["sessions"]["s3"]["state"] == "ACTIVE"
    # Main path restored
    assert storage.path.exists()
