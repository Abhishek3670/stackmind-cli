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
        '  - "scripts.test" runs node --test against tests/\n'
        "  - Unquoted item with: a colon inside\n"
    )
    repaired = repair_unquoted_yaml_scalars(malformed_yaml)
    data = yaml.safe_load(repaired)
    assert isinstance(data, dict)
    assert len(data["acceptance_criteria"]) == 4
    assert 'src="src/app.js"' in data["acceptance_criteria"][1]
    assert 'scripts.test' in data["acceptance_criteria"][2]


def test_validate_final_verdict_auto_populates_missing_completed_wos(tmp_path, monkeypatch):
    from validators.harness import review_gate

    # Mock subject_hash and plan_hash
    monkeypatch.setattr(review_gate, "subject_hash", lambda ws, wo_id: f"hash_{wo_id}")
    monkeypatch.setattr(review_gate, "plan_hash", lambda ws: "mock_plan_hash")

    verdict = {
        "schema": review_gate.FINAL_REVIEW_SCHEMA,
        "run_id": "run-test",
        "plan_id": "PLAN-001",
        "plan_hash": "mock_plan_hash",
        "status": "PASS",
        "reviewed_work_orders": [{"id": "WO-001", "subject_hash": "hash_WO-001"}],
        "acceptance_criteria": [{"id": "AC-1", "outcome": "PASS"}],
        "evidence": [{"kind": "test", "passed": True}],
        "findings": [],
        "required_actions": [],
    }

    # Calling with required WO-001 and WO-002: WO-002 was omitted by LLM but exists on disk
    ok, why = review_gate.validate_final_verdict(
        verdict, tmp_path, run_id="run-test", plan_id="PLAN-001", required_wo_ids=["WO-001", "WO-002"]
    )
    assert ok is True, f"Expected validation success with auto-population, got {why}"
    reviewed_ids = [w["id"] for w in verdict["reviewed_work_orders"]]
    assert "WO-002" in reviewed_ids


def test_ephemeral_test_files_ignored_in_contract_scope():
    from validators.harness.contract_gate import _is_ephemeral_test_file

    assert _is_ephemeral_test_file("tests/.node_test_output.log") is True
    assert _is_ephemeral_test_file("tests/_smoke_run.tmp") is True
    assert _is_ephemeral_test_file("build/test.log") is True
    assert _is_ephemeral_test_file("src/index.js") is False
    assert _is_ephemeral_test_file("styles.css") is False

