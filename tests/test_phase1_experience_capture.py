"""Comprehensive test suite for Phase 1 (Experience Capture).

Covers:
1. EXP- node ID birth-hashing and symbol registry integration.
2. ExperienceRecord schema validation and lossless serialization.
3. ExperienceStore atomic persistence, loading, listing, and filtering.
4. ExperienceRecorder transformation from harness execution contexts.
5. AgentRunner automatic experience artifact generation and event logging.
6. Experience CLI commands (list, show, stats).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
import pytest
import yaml
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.experience.models import (
    ExperienceAction,
    ExperienceCorrection,
    ExperienceFailure,
    ExperienceObservation,
    ExperienceRecord,
    ExperienceVerification,
)
from validators.experience.recorder import ExperienceRecorder
from validators.experience.store import ExperienceStore
from validators.harness.runner import AgentRunner, EchoLLMProvider
from validators.harness.snapshot import (
    TrustLevel,
    VerificationDimensions,
    WorkspaceDiff,
)
from validators.knowledge.api import ContextBundle
from validators.knowledge.registry import node_id_for


@pytest.fixture
def temp_workspace(tmp_path):
    project = tmp_path / "exp-workspace"
    init(project, name="Experience Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


# ─── 1. Identity & Node ID Birth-Hashing ─────────────────────────────────────

def test_experience_node_id_minting():
    signature = "diagnose:database_pool_exhaustion"
    timestamp = "2026-09-01T12:00:00+00:00"
    exp_id = ExperienceRecord.mint_id(signature, timestamp)
    assert exp_id.startswith("EXP-")
    assert len(exp_id) == 20  # EXP- + 16 hex chars
    assert exp_id == node_id_for("experience", f"{signature}:{timestamp}")


# ─── 2. ExperienceRecord Data Model & JSON Schema Validation ─────────────────

def test_experience_record_schema_validation(temp_workspace):
    exp_id = ExperienceRecord.mint_id("fix:db_leak", "2026-09-01T12:00:00+00:00")
    dimensions = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )
    diff = WorkspaceDiff(modified=("src/db.py",))
    verification = ExperienceVerification(
        dimensions=dimensions,
        observed_diff=diff,
        contract_id="WO-101",
        declaration_matches=True,
        tests_passed=True,
    )
    record = ExperienceRecord(
        experience_id=exp_id,
        task_id="task-001",
        agent_id="codex",
        task_signature="fix:db_leak",
        environment={
            "git_commit": "abcdef123456",
            "platform": "Windows",
            "python_version": "3.12.0",
            "runtime_version": "v3.1.0",
        },
        actions=(
            ExperienceAction(
                tool="bash",
                command_or_symbol="pytest tests/test_db.py",
                input_summary="Run DB tests",
                timestamp="2026-09-01T12:00:00+00:00",
            ),
        ),
        observations=(
            ExperienceObservation(
                output_summary="1 passed",
                is_error=False,
                exit_code=0,
            ),
        ),
        verification=verification,
        trust_level=TrustLevel.LEARNING_ELIGIBLE,
        learning_eligible=True,
        outcome="completed",
        duration_ms=450,
        recorded_at="2026-09-01T12:00:01+00:00",
    )

    schema_file = temp_workspace / "schemas" / "experience.schema.json"
    record.validate_schema(schema_file)

    # Lossless roundtrip
    serialized = record.to_dict()
    deserialized = ExperienceRecord.from_dict(serialized)
    assert deserialized.experience_id == record.experience_id
    assert deserialized.learning_eligible is True
    assert deserialized.verification.dimensions.all_passed is True
    assert deserialized.actions[0].command_or_symbol == "pytest tests/test_db.py"


# ─── 3. ExperienceStore Persistence & Querying ───────────────────────────────

def test_experience_store_crud_and_filtering(temp_workspace):
    store = ExperienceStore(temp_workspace)
    dimensions_pass = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )
    diff = WorkspaceDiff(added=("src/service.py",))
    verification_pass = ExperienceVerification(
        dimensions=dimensions_pass,
        observed_diff=diff,
    )

    # Record 1: Learning-eligible (codex)
    rec1 = ExperienceRecord(
        experience_id=ExperienceRecord.mint_id("task1", "2026-09-01T10:00:00+00:00"),
        task_id="task-1",
        agent_id="codex",
        task_signature="task1",
        environment={"runtime_version": "v3.1.0"},
        verification=verification_pass,
        trust_level=TrustLevel.LEARNING_ELIGIBLE,
        learning_eligible=True,
        outcome="completed",
        recorded_at="2026-09-01T10:00:00+00:00",
    )
    store.save_record(rec1)

    # Record 2: Observable only (gemini)
    dimensions_fail = VerificationDimensions(scope_verified=False)
    rec2 = ExperienceRecord(
        experience_id=ExperienceRecord.mint_id("task2", "2026-09-01T11:00:00+00:00"),
        task_id="task-2",
        agent_id="gemini",
        task_signature="task2",
        environment={"runtime_version": "v3.1.0"},
        verification=ExperienceVerification(dimensions=dimensions_fail, observed_diff=diff),
        trust_level=TrustLevel.OBSERVABLE,
        learning_eligible=False,
        outcome="blocked",
        recorded_at="2026-09-01T11:00:00+00:00",
    )
    store.save_record(rec2)

    # Load by ID
    loaded = store.load_record(rec1.experience_id)
    assert loaded is not None
    assert loaded.task_id == "task-1"

    # List records
    all_recs = store.list_records()
    assert len(all_recs) == 2

    # Filter eligible only
    eligible = store.list_records(only_learning_eligible=True)
    assert len(eligible) == 1
    assert eligible[0].experience_id == rec1.experience_id

    # Filter by agent
    gemini_recs = store.list_records(agent_id="gemini")
    assert len(gemini_recs) == 1
    assert gemini_recs[0].agent_id == "gemini"

    # Stats
    stats = store.stats()
    assert stats["total"] == 2
    assert stats["learning_eligible"] == 1
    assert stats["observable"] == 1


# ─── 4. ExperienceRecorder from Harness Context ──────────────────────────────

def test_experience_recorder_from_stage_inputs(temp_workspace):
    class MockTask:
        identifier = "task-300"
        kind = "work_order"
        query = "Refactor payment router"
        work_order_id = "WO-040"

    class MockDecision:
        status = "completed"
        summary = "Completed payment router refactoring"
        commands = ["pytest tests/test_payment.py"]
        blockers = ()
        uncertainty = ()

    stage_inputs = {
        "task": MockTask(),
        "decision": MockDecision(),
        "context": ContextBundle(
            entries=(),
            estimated_tokens=100,
            git_commit="git-1234",
            revision=1,
            semantic=False,
            stale=False,
            text="Mock context text",
            token_budget=1000,
            truncated=False,
            truncation_reason=None,
        ),
        "run_at": datetime.now(timezone.utc),
        "declaration_matches": True,
    }

    diff = WorkspaceDiff(modified=("payments/router.py",))
    dimensions = VerificationDimensions(
        scope_verified=True,
        state_verified=True,
        code_verified=True,
        behavioral_verified=True,
        security_verified=True,
        outcome_verified=True,
    )

    rec = ExperienceRecorder.capture_from_stage_inputs(
        temp_workspace,
        "codex",
        stage_inputs,
        diff=diff,
        dimensions=dimensions,
        trust_level=TrustLevel.LEARNING_ELIGIBLE,
        duration_ms=320,
    )

    assert rec.experience_id.startswith("EXP-")
    assert rec.work_order_id == "WO-040"
    assert rec.learning_eligible is True
    assert len(rec.actions) == 1
    assert rec.actions[0].command_or_symbol == "pytest tests/test_payment.py"
    assert rec.final_state["files_modified_count"] == 1


# ─── 5. AgentRunner Generates Experience Artifact Automatically ──────────────

def test_agent_runner_creates_experience_record_on_run(temp_workspace):
    # Setup work order in inbox
    inbox_dir = temp_workspace / ".sync" / "inbox" / "codex"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    task_file = inbox_dir / "2026-09-01_claude_WO-040-assignment.md"
    task_file.write_text(
        "---\n"
        "work_order_id: WO-040\n"
        "task_id: task-wo-040\n"
        "kind: work_order\n"
        "---\n"
        "# Work Order: Implement test service\n"
        "Please implement the test service.\n",
        encoding="utf-8",
    )

    # Setup contract
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-040",
        "scope": {
            "allow": [{"module": "*", "depth": 2}],
            "deny": [],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5},
    }
    contracts_dir = temp_workspace / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-040.yaml").write_text(yaml.dump(contract_data), encoding="utf-8")

    class MockHarnessLLM(EchoLLMProvider):
        def complete(self, request):
            payload = {
                "status": "completed",
                "summary": "Implemented test service successfully",
                "report_markdown": "Done",
                "modified_files": [],
                "commands": [],
                "blockers": [],
                "uncertainty": [],
            }
            from validators.harness.runner import CompletionRecord
            return CompletionRecord(
                provider="mock",
                model="mock-model",
                payload=payload,
                prompt_tokens=50,
                completion_tokens=50,
                cost_estimate=0.001,
            )

    runner = AgentRunner(temp_workspace, "codex", llm_provider=MockHarnessLLM())
    result = runner.run_once()
    assert result.status == "completed"

    # Verify experience record was automatically written to .sync/experience/records/
    store = ExperienceStore(temp_workspace)
    records = store.list_records()
    assert len(records) == 1
    exp_rec = records[0]
    assert exp_rec.experience_id.startswith("EXP-")
    assert exp_rec.task_id == "2026-09-01_claude_WO-040-assignment.md"
    assert exp_rec.learning_eligible is True

    # Verify event logged with experience_id
    events_file = temp_workspace / ".sync" / "state" / "harness" / "events.jsonl"
    assert events_file.exists()
    event_data = json.loads(events_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert event_data["experience_id"] == exp_rec.experience_id
    assert event_data["learning_eligible"] is True


# ─── 6. Experience CLI Commands ──────────────────────────────────────────────

def test_experience_cli_commands(temp_workspace):
    # Create test record
    store = ExperienceStore(temp_workspace)
    rec = ExperienceRecord(
        experience_id=ExperienceRecord.mint_id("test_cli", "2026-09-01T15:00:00+00:00"),
        task_id="task-cli-test",
        agent_id="codex",
        task_signature="test_cli",
        environment={"runtime_version": "v3.1.0"},
        verification=ExperienceVerification(
            dimensions=VerificationDimensions(
                scope_verified=True,
                state_verified=True,
                code_verified=True,
                behavioral_verified=True,
                security_verified=True,
                outcome_verified=True,
            ),
            observed_diff=WorkspaceDiff(),
        ),
        trust_level=TrustLevel.LEARNING_ELIGIBLE,
        learning_eligible=True,
        outcome="completed",
        recorded_at="2026-09-01T15:00:00+00:00",
    )
    store.save_record(rec)

    runner = CliRunner(env={"COLUMNS": "160"})

    # 1. Test 'experience list'
    res_list = runner.invoke(cli, ["experience", "list", "-p", str(temp_workspace)])
    assert res_list.exit_code == 0
    assert rec.experience_id in res_list.output
    assert "LEARNING_ELIGIBLE" in res_list.output

    # 2. Test 'experience show'
    res_show = runner.invoke(cli, ["experience", "show", rec.experience_id, "-p", str(temp_workspace)])
    assert res_show.exit_code == 0
    assert rec.experience_id in res_show.output
    assert "Verification Evidence" in res_show.output

    # 3. Test 'experience stats'
    res_stats = runner.invoke(cli, ["experience", "stats", "-p", str(temp_workspace)])
    assert res_stats.exit_code == 0
    assert "Experience Subsystem Statistics" in res_stats.output
    assert "Learning Eligible" in res_stats.output


def test_capture_from_work_order_and_session(temp_workspace):
    wo_dir = temp_workspace / ".sync" / "work-orders"
    wo_dir.mkdir(parents=True, exist_ok=True)
    wo_file = wo_dir / "WO-100.yaml"
    wo_file.write_text(
        """schema_version: 1
id: WO-100
title: "Implement Fast Cache"
status: COMPLETED
assigned_agents:
  - codex
qa_verdict: APPROVED
deliverable:
  type: module
  path: app/cache.py
required_changes:
  - id: R1
    summary: "Add Redis Cache Layer"
    detail: "Implement connect and get/set methods"
""",
        encoding="utf-8",
    )

    # Test direct capture
    rec = ExperienceRecorder.capture_from_work_order(temp_workspace, "WO-100", agent="codex")
    assert rec is not None
    assert rec.work_order_id == "WO-100"
    assert rec.learning_eligible is True
    assert len(rec.actions) == 1
    assert rec.actions[0].command_or_symbol == "Add Redis Cache Layer"

    # Test CLI capture command
    runner = CliRunner(env={"COLUMNS": "160"})
    res = runner.invoke(cli, ["experience", "capture", "--work-order", "WO-100", "-p", str(temp_workspace)])
    assert res.exit_code == 0
    assert "Captured experience" in res.output

    # Test CLI backfill
    res_backfill = runner.invoke(cli, ["experience", "capture", "--backfill", "-p", str(temp_workspace)])
    assert res_backfill.exit_code == 0
    assert "Backfilled" in res_backfill.output

    # Test capture from session handoff
    outbox = temp_workspace / ".sync" / "outbox" / "codex"
    outbox.mkdir(parents=True, exist_ok=True)
    handoff = outbox / "handoff-2026-09-01T160000.md"
    handoff.write_text(
        """# Session Handoff - Codex
## COMPLETED
- Completed WO-100: Redis cache layer implementation.
## Quality Metrics
- commit: abc1234
- branch: main
- tested_at: 2026-09-01T16:00:00Z
""",
        encoding="utf-8",
    )

    session_rec = ExperienceRecorder.capture_from_session(temp_workspace, "codex", handoff_path=handoff)
    assert session_rec is not None
    assert session_rec.work_order_id == "WO-100"
    assert session_rec.learning_eligible is True
