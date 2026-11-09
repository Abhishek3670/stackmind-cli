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


def test_work_order_schema_allows_report_type():
    schema_path = Path("schemas/work-order.schema.json")
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    enums = data["properties"]["deliverable"]["properties"]["type"]["enum"]
    assert "report" in enums, f"Expected 'report' in deliverable.type enum, got {enums}"


def test_default_max_tool_calls_headroom():
    from validators.kernel.providers.gateway import DEFAULT_MAX_TOOL_CALLS, DEFAULT_MAX_TURNS

    assert DEFAULT_MAX_TOOL_CALLS >= 60, f"Expected >= 60 tool calls headroom, got {DEFAULT_MAX_TOOL_CALLS}"
    assert DEFAULT_MAX_TURNS >= 60, f"Expected >= 60 turns headroom, got {DEFAULT_MAX_TURNS}"


def test_collect_peer_owned_deliverables_skips_ephemeral_files(tmp_path):
    from validators.harness.contract_gate import _collect_peer_owned_deliverables

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True)
    # Simulate a cleanup WO that declares a log file as deliverable
    (active_dir / "WO-011.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-011",
            "assigned_agents": ["codex"],
            "deliverable": {"path": "tests/.node_test_output.log"},
            "deliverables": [{"path": "src/real.js"}],
        }),
        encoding="utf-8"
    )

    owned = _collect_peer_owned_deliverables(tmp_path, own_wo_id="WO-002", current_agent="gemma")
    assert "tests/.node_test_output.log" not in owned, "Ephemeral logs must not be registered as owned deliverables"
    assert "src/real.js" in owned, "Real code deliverables must remain registered"


def test_evaluate_final_acceptance_defaults_plan_id_when_plan_exists(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from validators.harness import review_gate

    # Mock plan_hash and required_wo_ids_for_run
    monkeypatch.setattr(review_gate, "plan_hash", lambda ws: "mock_plan_hash")
    monkeypatch.setattr(review_gate, "required_wo_ids_for_run", lambda state, ws: [])

    state = SimpleNamespace(plan_id=None, completed_wo_ids=[], integration_wo_id=None, blocked_wo_ids=[])
    report = review_gate.evaluate_final_acceptance(state, tmp_path)
    assert "no user-approved plan is recorded for this run" not in report.reasons
    assert state.plan_id == "PLAN-001"

def test_authoring_gate_allows_qa_report_deliverable_without_role_mismatch():
    from validators.harness.authoring_gate import AuthoringGate

    gate = AuthoringGate()
    wo_data = {
        "id": "WO-012",
        "type": "VALIDATION",
        "assigned_agents": ["gemma"],
        "deliverable": {
            "type": "report",
            "description": "Run automated test suite and report evidence."
        }
    }
    errors = []
    gate._validate_work_order("work-orders/WO-012.yaml", wo_data, errors)
    mismatch_errors = [e for e in errors if "Role-deliverable mismatch" in e]
    assert not mismatch_errors, f"Unexpected role-deliverable mismatch error: {mismatch_errors}"


def test_prepare_run_for_resume_clears_final_review_budget_and_routes_to_dispatching(tmp_path):
    from types import SimpleNamespace
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.daemon.supervisor import Phase

    storage = DaemonStorage(tmp_path / "daemon")
    mgr = SessionManager(storage=storage)
    mgr.list_plans = lambda sid: [{"plan_id": "PLAN-001"}]
    mgr.supervisor = SimpleNamespace(
        unblock_in_flight_work_orders=lambda state: None,
        _discover_worker_wos=lambda state: None,
        _archive_verdicts_for_wo=lambda wo, ws: None,
        _find_latest_contract_for_wo=lambda *args, **kwargs: None,
        _transition=lambda state, phase, **kwargs: setattr(state, "phase", phase),
    )

    state = SimpleNamespace(
        session_id="s1",
        run_id="run-1",
        planning_wo_id=None,
        completed_wo_ids=["WO-001"],
        worker_wo_ids=["WO-001", "WO-002"],
        gitops_wo_id=None,
        transitions=[{"from": "FINAL_REVIEW", "to": "BLOCKED"}],
        budget_usage={"FINAL_REVIEW": {"RUN": 2}},
        final_review_rounds=2,
        final_review_history=[{"event": "exhausted"}],
        plan_id=None,
        blocked_wo_ids=[],
        failed_wo_ids=[],
        retry_counts={},
        recovery_attempts={},
        worker_retries={},
        qa_reworks={},
        dependency_escalation_retries={},
        worker_blockers={},
        integration_blockers={},
        error="Blocked",
    )
    session = {"state": "RUNNING", "journal": []}

    mgr._prepare_run_for_resume(state, session, tmp_path)
    assert state.final_review_rounds == 0
    assert len(state.budget_usage.get("FINAL_REVIEW", {})) == 0
    assert state.phase == Phase.DISPATCHING


def test_recovery_decision_archives_and_normalizes_dependency_action(tmp_path):
    """Ensure recovery decision is archived upon apply and amend_contract is normalized for dependency failures."""
    from types import SimpleNamespace
    from pathlib import Path
    import json
    from validators.kernel.daemon.supervisor.recovery import _RecoveryMixin
    from validators.kernel.daemon.supervisor.models import AdvanceResult

    class FakeSup(_RecoveryMixin):
        def __init__(self):
            self.archived = []
        def _archive_recovery_artifact(self, ws, run_id, path):
            self.archived.append(path)
        def _enter_recovery_repair(self, state, ws, record):
            return AdvanceResult.TRANSITIONED, None
        def _get_wo_deliverable_path(self, wo_id, ws):
            return None
        def _find_latest_contract_for_wo(self, ws, run_id, wo_id):
            return None

    sup = FakeSup()
    state = SimpleNamespace(
        run_id="run-1",
        worker_blockers={"WO-013": {"failure_code": "INVALID_DEPENDENCY", "dependency_code": "SUPERSEDED_DEPENDENCY"}},
        contract_revisions={},
        recovery_decisions=[],
    )

    decision = {
        "work_order": "WO-013",
        "action": "amend_contract",
        "reason": "Remove superseded dependency",
        "failure_code": "INVALID_DEPENDENCY",
    }

    advance, err = sup._apply_recovery_decision(state, tmp_path, "WO-013", decision)
    assert err is None
    # Must archive decision artifact immediately
    assert ".sync/decisions/recovery/WO-013.decision.json" in sup.archived
    # Must normalize amend_contract to architect_fix for dependency failures
    assert decision["action"] == "architect_fix"
    assert state.repair_context["action"] == "architect_fix"


def test_plan_synthesizer_adopts_existing_active_work_orders(tmp_path):
    """Verify that synthesize_child_work_orders adopts existing active work orders without duplicating or renumbering."""
    from validators.kernel.daemon.authoring import synthesize_child_work_orders
    from validators.kernel.io import durable_write_yaml

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = tmp_path / ".sync" / "contracts"
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    # Simulate Architect proactively authoring WO-001 in Turn 1 via LLM batch write
    wo1_data = {
        "id": "WO-001",
        "title": "Project Scaffolding & Setup",
        "milestone_id": "Milestone 1",
        "type": "FEATURE",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["gemini"],
        "dependencies": [],
        "deliverable": {"type": "code", "path": "index.html", "description": "HTML entrypoint"},
        "description": "Initialize project structure and index.html",
        "acceptance_criteria": ["index.html exists and is valid HTML5"],
    }
    durable_write_yaml(active_dir / "WO-001.yaml", wo1_data)

    plan = {
        "title": "PineApple Marketing Site",
        "content": (
            "# PineApple Marketing Site\n\n"
            "## Architecture\n- Frontend: HTML/Tailwind/GSAP\n\n"
            "## Milestones\n"
            "### Milestone 1: Project Scaffolding & Setup\n"
            "- Role: Frontend\n"
            "- Deliverable: index.html\n"
            "- Task 1.1: Initialize project structure\n"
        ),
        "metadata": {
            "work_orders": [
                {
                    "id": "WO-001",
                    "title": "Project Scaffolding & Setup",
                    "milestone_id": "Milestone 1",
                    "type": "FEATURE",
                    "priority": "P0",
                    "assigned_agents": ["gemini"],
                    "dependencies": [],
                    "deliverable": {"type": "code", "path": "index.html"},
                }
            ]
        }
    }

    result = synthesize_child_work_orders(tmp_path, plan)
    assert len(result) == 1
    assert result[0]["id"] == "WO-001", "Must adopt WO-001 instead of renumbering to WO-002"
    # Preserves Architect's authored acceptance criteria
    assert result[0]["acceptance_criteria"] == ["index.html exists and is valid HTML5"]

    # Verify no duplicate work order files were written
    active_files = [f.name for f in active_dir.glob("*.yaml")]
    assert active_files == ["WO-001.yaml"], f"Expected only WO-001.yaml, got: {active_files}"


def test_format_work_orders_filters_cancelled_duplicates():
    """Verify that format_work_orders suppresses cancelled duplicate work orders in TUI sidebar."""
    from cli.tui.runtime_panel import format_work_orders

    orders = [
        {"id": "WO-000", "title": "Planning", "status": "COMPLETED"},
        {"id": "WO-001", "title": "Scaffolding", "status": "ACTIVE"},
        {"id": "WO-007", "title": "Scaffolding Duplicate", "status": "CANCELLED"},
        {"id": "WO-009", "title": "Switcher Duplicate", "status": "CANCELLED"},
    ]
    rendered_lines = format_work_orders(orders)
    rendered_text = "\n".join(line.plain for line in rendered_lines)

    assert "WO-000" in rendered_text
    assert "WO-001" in rendered_text
    assert "WO-007" not in rendered_text, "Cancelled duplicate WO-007 must be filtered out"
    assert "WO-009" not in rendered_text, "Cancelled duplicate WO-009 must be filtered out"


def test_reconcile_promotes_active_with_completion(tmp_path):
    """Verify that work orders in ACTIVE with completion blocks are promoted to COMPLETED."""
    from validators.kernel.daemon.authoring import reconcile_active_work_orders_with_plan
    from validators.kernel.io import durable_write_yaml

    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    active_dir.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    # Write compliant PLAN.md with Milestone 1
    (tmp_path / "PLAN.md").write_text(
        "# Project Plan: Web Design\n\n"
        "## Current Architecture\n"
        "- Backend: Node\n"
        "- Frontend: Tailwind\n"
        "- Database: None\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Setup\n"
        "  - Role: Backend\n"
        "  - Deliverable: package.json\n"
        "  - Task 1.1: Initialize package.json\n",
        encoding="utf-8",
    )

    wo_data = {
        "id": "WO-001",
        "title": "Setup",
        "milestone_id": "Milestone 1",
        "status": "BLOCKED",
        "deliverable": {"type": "config", "path": "package.json"},
        "completion": {
            "work_order_id": "WO-001",
            "completed_at": "2026-11-09T12:00:00Z",
            "deliverable_hashes": {"package.json": "abc"},
        },
    }
    durable_write_yaml(active_dir / "WO-001.yaml", wo_data)

    reconciled = reconcile_active_work_orders_with_plan(tmp_path)
    assert "WO-001" in reconciled
    assert not (active_dir / "WO-001.yaml").exists(), "WO-001 must be moved out of ACTIVE"
    assert (completed_dir / "WO-001.yaml").exists(), "WO-001 must be moved to COMPLETED"

    loaded = yaml.safe_load((completed_dir / "WO-001.yaml").read_text(encoding="utf-8"))
    assert loaded["status"] == "COMPLETED"


def test_reconcile_staged_auto_routes_completed_status(tmp_path):
    """Verify that staged work orders with status COMPLETED are moved to COMPLETED directory."""
    from validators.harness.runner import HarnessRunner
    from validators.kernel.io import durable_write_yaml

    sync_dir = tmp_path / ".sync"
    active_dir = sync_dir / "work-orders" / "ACTIVE"
    completed_dir = sync_dir / "work-orders" / "COMPLETED"
    active_dir.mkdir(parents=True, exist_ok=True)

    wo_data = {
        "id": "WO-015",
        "title": "Completed By Architect",
        "status": "COMPLETED",
        "type": "FEATURE",
    }
    durable_write_yaml(active_dir / "WO-015.yaml", wo_data)

    HarnessRunner._reconcile_staged_work_order_collisions(tmp_path)
    assert not (active_dir / "WO-015.yaml").exists(), "Must be moved out of ACTIVE"
    assert (completed_dir / "WO-015.yaml").exists(), "Must be placed in COMPLETED"




