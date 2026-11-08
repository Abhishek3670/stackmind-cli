"""Ponytail optimization verification tests."""

import json
from pathlib import Path
import yaml

from validators.harness.authoring_yaml import repair_unquoted_yaml_scalars
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.daemon.manager import SessionManager


def test_daemon_storage_compact_save_roundtrip(tmp_path):
    storage = DaemonStorage(tmp_path)
    payload = {"sessions": {"s1": {"state": "RUNNING"}}, "events": [{"sequence": 1, "name": "test"}]}
    storage.save(payload)

    # Check file exists and is compact (no unnecessary multi-space indentation)
    raw = (tmp_path / "daemon-state.json").read_text(encoding="utf-8")
    assert "  " not in raw, "daemon-state.json is saved in compact format"

    # Verify round-trip load
    recovered = storage.load()
    assert recovered["sessions"]["s1"]["state"] == "RUNNING"
    assert recovered["events"][0]["name"] == "test"


def test_manager_event_capping(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    # Simulate generating 600 events
    for i in range(600):
        manager.events.publish("test.event", "session-1", index=i)

    # Trigger save and verify state file only contains latest 500 events
    manager._save()
    saved = storage.load()
    assert len(saved["events"]) == 500, f"Expected 500 events, got {len(saved['events'])}"
    assert saved["events"][-1]["payload"]["index"] == 599


def test_authoring_yaml_auto_repairs_inner_quotes_in_lists():
    malformed_yaml = (
        "acceptance_criteria:\n"
        '  - "Includes <link rel=\\"stylesheet\\"> in <head> and module script src=\\"src/app.js\\""\n'
        '  - "Renders all required elements in <head> and module script src="src/app.js""\n'
        "  - Unquoted item with: a colon inside\n"
    )
    repaired = repair_unquoted_yaml_scalars(malformed_yaml)
    data = yaml.safe_load(repaired)
    assert isinstance(data, dict)
    assert len(data["acceptance_criteria"]) == 3
    assert 'src="src/app.js"' in data["acceptance_criteria"][1]
