"""SDP-FIX-009-HF2 Authoring and Execution Integrity Regression Test Suite.

Comprehensive test coverage across:
1. Canonical schema consistency (work-order and index schemas agree, no QA/TEST/test/report).
2. QA authoring and delegation (VALIDATION required, code/doc required, QA rejected).
3. Prompt and repair consistency (prompts never prescribe QA/TEST, repair reinforces canonical contract).
4. Workspace schema initialization (mirrored schemas preserve canonical contract).
5. Journal reconciliation (GovernedToolRuntime boundary, ToolGateway written_files, auto-reconciliation of physical writes).
6. Staged validation (fail-closed on non-canonical enums, pass on canonical).
7. Tool-budget accounting (batch_write_files, static schema read caching/deduplication, budget warnings).
8. Planning finalization (context-preserving final prompt, outcome verification).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from jsonschema import Draft7Validator

from cli.init import ensure_workspace_protocol_scaffolding
from cli.validate import validate as validate_runtime
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.plan import WORK_ORDER_SCHEMA_TEMPLATE
from validators.harness.runner import AgentRunner
from validators.harness.runner.models import GovernedToolRuntime, HarnessDecision, HarnessTask
from validators.harness.task_contract import reconcile_modified_files
from validators.kernel.boundary import OperationJournal, RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.daemon.manager import DaemonStorage, SessionManager
from validators.kernel.identity import AuthorizationPolicy, get_role_policy
from validators.kernel.operations import OperationRequest, OperationType
from validators.kernel.providers.gateway import (
    DEFAULT_MAX_TOOL_CALLS,
    ProviderGateway,
    STANDARD_KERNEL_TOOLS,
    ToolCallRequest,
    ToolLimitExceededError,
    ToolLoopExhaustedError,
)
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace


# ══════════════════════════════════════════════════════════════════
# 1. Canonical Schema Consistency
# ══════════════════════════════════════════════════════════════════

def test_work_order_schema_canonical_enums():
    """work-order.schema.json must strictly accept canonical types and reject QA/TEST/test/report."""
    schema_path = Path("schemas/work-order.schema.json")
    assert schema_path.is_file()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    type_enum = schema["properties"]["type"]["enum"]
    expected_types = [
        "PHASE", "FEATURE", "BUGFIX", "HOTFIX", "REFACTOR",
        "RESEARCH", "AUDIT", "VALIDATION", "FIX"
    ]
    assert set(type_enum) == set(expected_types)
    assert "QA" not in type_enum
    assert "TEST" not in type_enum

    deliv_type_enum = schema["properties"]["deliverable"]["properties"]["type"]["enum"]
    expected_deliv = ["code", "doc", "config", "module"]
    assert set(deliv_type_enum) == set(expected_deliv)
    assert "test" not in deliv_type_enum
    assert "report" not in deliv_type_enum


def test_index_schema_canonical_enums():
    """index.schema.json must strictly accept canonical types and reject QA/TEST."""
    schema_path = Path("schemas/index.schema.json")
    assert schema_path.is_file()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    order_type_enum = schema["properties"]["orders"]["items"]["properties"]["type"]["enum"]
    expected_types = [
        "PHASE", "FEATURE", "BUGFIX", "HOTFIX", "REFACTOR",
        "RESEARCH", "AUDIT", "VALIDATION", "FIX"
    ]
    assert set(order_type_enum) == set(expected_types)
    assert "QA" not in order_type_enum
    assert "TEST" not in order_type_enum


def test_shipped_schema_copies_agreement():
    """All schema copies (schemas/, workspace/schemas/, etc.) must agree on canonical enums."""
    root_wo = json.loads(Path("schemas/work-order.schema.json").read_text(encoding="utf-8"))
    root_idx = json.loads(Path("schemas/index.schema.json").read_text(encoding="utf-8"))

    expected_types = set(root_wo["properties"]["type"]["enum"])
    expected_deliv = set(root_wo["properties"]["deliverable"]["properties"]["type"]["enum"])
    expected_idx_types = set(root_idx["properties"]["orders"]["items"]["properties"]["type"]["enum"])

    assert "QA" not in expected_types
    assert "test" not in expected_deliv

    for candidate in Path(".").glob("**/schemas/work-order.schema.json"):
        if ".venv" in candidate.parts:
            continue
        c_data = json.loads(candidate.read_text(encoding="utf-8"))
        c_types = set(c_data["properties"]["type"]["enum"])
        c_deliv = set(c_data["properties"]["deliverable"]["properties"]["type"]["enum"])
        assert "QA" not in c_types, f"Stale QA in {candidate}"
        assert "test" not in c_deliv, f"Stale test in {candidate}"
        assert c_types == expected_types
        assert c_deliv == expected_deliv


# ══════════════════════════════════════════════════════════════════
# 2. QA Authoring and Delegation
# ══════════════════════════════════════════════════════════════════

def test_authoring_gate_accepts_canonical_validation_work_order(tmp_path: Path):
    """AuthoringGate must pass VALIDATION work order assigned to gemma with deliverable.type code."""
    gate = AuthoringGate(project_root=tmp_path)
    valid_wo = yaml.safe_dump({
        "id": "WO-002",
        "type": "VALIDATION",
        "title": "Verify Calculator Operations",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": ["WO-001"],
        "deliverable": {
            "type": "code",
            "path": "tests/test_calculator.py",
            "description": "Functional test suite for calculator",
        },
    })
    decision = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", valid_wo, agent="claude")
    assert decision.passed, f"Expected pass, got errors: {decision.errors}"


def test_authoring_gate_rejects_qa_type_and_test_deliverable(tmp_path: Path):
    """AuthoringGate must reject work orders declaring type QA or deliverable.type test."""
    gate = AuthoringGate(project_root=tmp_path)

    # 1. Reject type: QA
    bad_type_wo = yaml.safe_dump({
        "id": "WO-002",
        "type": "QA",
        "title": "Verify Calculator Operations",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "path": "tests/test_calc.py",
            "description": "Functional tests",
        },
    })
    dec1 = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", bad_type_wo, agent="claude")
    assert not dec1.passed
    assert any("'QA' is not one of" in err for err in dec1.errors)

    # 2. Reject deliverable.type: test
    bad_deliv_wo = yaml.safe_dump({
        "id": "WO-002",
        "type": "VALIDATION",
        "title": "Verify Calculator Operations",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": [],
        "deliverable": {
            "type": "test",
            "path": "tests/test_calc.py",
            "description": "Functional tests",
        },
    })
    dec2 = gate.validate_artifact_content(".sync/work-orders/ACTIVE/WO-002.yaml", bad_deliv_wo, agent="claude")
    assert not dec2.passed
    assert any("must be code, doc, config, or module" in err or "'test' is not one of" in err for err in dec2.errors)


# ══════════════════════════════════════════════════════════════════
# 3. Prompt and Repair Consistency
# ══════════════════════════════════════════════════════════════════

def test_plan_template_prescribes_canonical_qa_pattern():
    """WORK_ORDER_SCHEMA_TEMPLATE must prescribe type VALIDATION and deliverable.type code."""
    assert 'type: "VALIDATION"' in WORK_ORDER_SCHEMA_TEMPLATE
    assert 'type: "QA"' not in WORK_ORDER_SCHEMA_TEMPLATE
    assert 'type: "test"' not in WORK_ORDER_SCHEMA_TEMPLATE
    assert 'code, doc, config, module' in WORK_ORDER_SCHEMA_TEMPLATE


def test_adhoc_system_message_canonical_guidance(tmp_path: Path):
    """Architect adhoc prompt must explicitly instruct VALIDATION and forbid QA/test."""
    runner = AgentRunner(tmp_path, agent="claude")
    msg = runner._build_adhoc_system_message()

    assert "Testing, QA, and verification tasks MUST declare `type: 'VALIDATION'` (NEVER 'QA')" in msg
    assert "set `type: 'code'` and `path: 'tests/test_...py'` (NEVER `type: 'test'`)" in msg


def test_manager_repair_prompt_enforces_canonical_contract(tmp_path: Path):
    """SessionManager auto-repair prompt must only list canonical enums and forbid QA/test."""
    storage = DaemonStorage(tmp_path / "daemon.json")
    manager = SessionManager(storage)
    session = manager.create_session("claude", "mock", {}, str(tmp_path), session_id="test-repair-prompt")
    session_id = session["session_id"]

    mock_runner = MagicMock()
    mock_result = MagicMock()
    mock_result.status = "blocked"
    mock_result.reason = "staged stackmind validate failed: work-orders/INDEX.yaml: orders.1.type - 'QA' is not one of [...]"
    mock_result.meta = {}
    mock_result.payload = {}
    mock_runner.run_once.return_value = mock_result

    with patch.object(manager, "_runner_factory", return_value=mock_runner):
        with patch.object(manager, "start_turn") as mock_start_turn:
            cancel_event = MagicMock()
            cancel_event.is_set.return_value = False
            _, op_id = manager.begin_operation(session_id, "turn", {"prompt": "run"})
            manager._run_turn(session_id, op_id, cancel_event, "run")

            mock_start_turn.assert_called_once()
            repair_prompt = mock_start_turn.call_args[0][1]
            assert "type: 'VALIDATION' (NEVER 'QA' or 'TEST')" in repair_prompt
            assert "deliverable.type: 'code'" in repair_prompt
            assert "'QA'" not in repair_prompt.split("Allowed 'type' enums")[1].split("]")[0]


# ══════════════════════════════════════════════════════════════════
# 4. Workspace Schema Initialization
# ══════════════════════════════════════════════════════════════════

def test_workspace_initialization_mirrors_canonical_schemas(tmp_path: Path):
    """ensure_workspace_protocol_scaffolding must mirror canonical schemas into new workspaces."""
    ensure_workspace_protocol_scaffolding(tmp_path)

    sync_wo_schema = tmp_path / ".sync" / "schemas" / "work-order.schema.json"
    assert sync_wo_schema.is_file()

    schema_data = json.loads(sync_wo_schema.read_text(encoding="utf-8"))
    types = schema_data["properties"]["type"]["enum"]
    assert "VALIDATION" in types
    assert "QA" not in types
    assert "TEST" not in types

    deliv_types = schema_data["properties"]["deliverable"]["properties"]["type"]["enum"]
    assert "code" in deliv_types
    assert "test" not in deliv_types


# ══════════════════════════════════════════════════════════════════
# 5. Journal Reconciliation
# ══════════════════════════════════════════════════════════════════

def test_governed_tool_runtime_boundary_property(tmp_path: Path):
    """GovernedToolRuntime.boundary must access gateway.boundary."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-rt")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("claude", "WO-000", ("workspace/**",), (), "read-write").freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file",))

    gateway = ToolGateway(ws, boundary, contract, policy, "s", "a", "claude", "mock")
    runtime = GovernedToolRuntime(workspace=ws, gateway=gateway)

    assert runtime.boundary is boundary
    assert runtime.boundary.journal is journal


def test_tool_gateway_written_files_property(tmp_path: Path):
    """ToolGateway.written_files must authoritatively reflect successful writes in the journal."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-gw-written")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("codex", "WO-001", ("workspace/**",), (), "read-write").freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file",))

    gateway = ToolGateway(ws, boundary, contract, policy, "s", "a", "codex", "mock")
    assert gateway.written_files == []

    gateway.write_file("src/app.py", "print('hello')")
    gateway.write_file("README.md", "# My App")

    assert gateway.written_files == ["src/app.py", "README.md"]


def test_reconcile_modified_files_with_empty_model_declaration():
    """reconcile_modified_files must restore written files even when model declared modified_files: []."""
    decision = HarnessDecision(
        status="blocked",
        summary="Unable to complete task",
        report_markdown="",
        blockers=("Task context missing",),
        modified_files=(),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
        commands=(),
    )
    observed = ["PLAN.md", ".sync/work-orders/ACTIVE/WO-001.yaml"]

    merged_decision, auto_added = reconcile_modified_files(decision, observed)

    assert set(merged_decision.modified_files) == set(observed)
    assert set(auto_added) == set(observed)


def test_reconcile_modified_files_handles_duplicates_and_normalizes():
    """reconcile_modified_files must normalize backslashes, strip leading slashes, and deduplicate."""
    decision = HarnessDecision(
        status="completed",
        summary="Done",
        report_markdown="",
        blockers=(),
        modified_files=("src/app.py",),
        release_target=None,
        retrieval_queries=(),
        uncertainty=(),
        commands=(),
    )
    observed = ["src\\app.py", "/src/app.py", "tests/test_app.py", "tests\\test_app.py"]

    merged, auto_added = reconcile_modified_files(decision, observed)

    assert merged.modified_files == ("src/app.py", "tests/test_app.py")
    assert auto_added == ["tests/test_app.py"]


# ══════════════════════════════════════════════════════════════════
# 6. Staged Validation
# ══════════════════════════════════════════════════════════════════

def test_staged_validation_passes_canonical_work_orders(tmp_path: Path):
    """validate_runtime on a staged workspace with canonical VALIDATION work orders must succeed."""
    ensure_workspace_protocol_scaffolding(tmp_path)

    # Author valid index and valid work order
    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "type": "VALIDATION",
        "title": "Verify system",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "path": "tests/test_system.py",
            "description": "Validation test suite",
        },
    }), encoding="utf-8")

    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    index_file.write_text(yaml.safe_dump({
        "schema_version": 1,
        "next_id": 2,
        "orders": [
            {
                "id": "WO-001",
                "type": "VALIDATION",
                "title": "Verify system",
                "status": "ACTIVE",
                "priority": "P1",
                "assigned_agents": ["gemma"],
                "dependencies": [],
                "file": "work-orders/ACTIVE/WO-001.yaml",
                "deliverable": {
                    "type": "code",
                    "path": "tests/test_system.py",
                    "description": "Validation test suite",
                },
            }
        ],
    }), encoding="utf-8")

    result = validate_runtime(tmp_path)
    # Check that there are NO schema errors on work orders or index
    schema_errors = [e.message for e in result.errors if "Schema" in e.layer]
    assert not schema_errors, f"Unexpected schema errors: {schema_errors}"


def test_staged_validation_fails_closed_on_qa_work_order(tmp_path: Path):
    """validate_runtime must fail closed when staged work order contains type: QA."""
    ensure_workspace_protocol_scaffolding(tmp_path)

    wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    wo_dir.mkdir(parents=True, exist_ok=True)
    (wo_dir / "WO-001.yaml").write_text(yaml.safe_dump({
        "id": "WO-001",
        "type": "QA",  # Non-canonical!
        "title": "Verify system",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": ["gemma"],
        "dependencies": [],
        "deliverable": {
            "type": "test",  # Non-canonical!
            "path": "tests/test_system.py",
            "description": "Validation test suite",
        },
    }), encoding="utf-8")

    result = validate_runtime(tmp_path)
    err_msgs = [e.message for e in result.errors]
    assert any("'QA' is not one of" in m for m in err_msgs)


# ══════════════════════════════════════════════════════════════════
# 7. Tool-Budget Accounting & Batch Authoring
# ══════════════════════════════════════════════════════════════════

def test_batch_write_files_authoring_and_validation(tmp_path: Path):
    """batch_write_files must atomically write multiple files with independent AuthoringGate validation."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-batch")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("claude", "WO-000", ("workspace/**",), (), "read-write").freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "batch_write_files"))

    tools = ToolGateway(ws, boundary, contract, policy, "s", "a", "claude", "mock")

    files_to_write = [
        {
            "path": "PLAN.md",
            "content": "# Project Plan\n\n## Architecture\n\n## Roadmap\n",
        },
        {
            "path": ".sync/work-orders/ACTIVE/WO-001.yaml",
            "content": yaml.safe_dump({
                "id": "WO-001",
                "type": "FEATURE",
                "title": "Implement models",
                "status": "ACTIVE",
                "priority": "P1",
                "assigned_agents": ["codex"],
                "dependencies": [],
                "deliverable": {
                    "type": "code",
                    "path": "models.py",
                    "description": "SQLAlchemy models",
                },
            }),
        },
        {
            "path": ".sync/work-orders/ACTIVE/WO-002.yaml",
            "content": yaml.safe_dump({
                "id": "WO-002",
                "type": "VALIDATION",
                "title": "Verify models",
                "status": "ACTIVE",
                "priority": "P1",
                "assigned_agents": ["gemma"],
                "dependencies": ["WO-001"],
                "deliverable": {
                    "type": "code",
                    "path": "tests/test_models.py",
                    "description": "Model verification suite",
                },
            }),
        },
    ]

    res = tools.batch_write_files(files_to_write)
    assert res["status"] == "success"
    assert res["written_count"] == 3

    assert (ws.path_for("PLAN.md")).is_file()
    assert (ws.path_for(".sync/work-orders/ACTIVE/WO-001.yaml")).is_file()
    assert (ws.path_for(".sync/work-orders/ACTIVE/WO-002.yaml")).is_file()

    # Reconciled in tools.written_files
    assert "PLAN.md" in tools.written_files
    assert ".sync/work-orders/ACTIVE/WO-001.yaml" in tools.written_files
    assert ".sync/work-orders/ACTIVE/WO-002.yaml" in tools.written_files


def test_batch_write_files_fails_on_invalid_artifact(tmp_path: Path):
    """batch_write_files must fail closed if any artifact in the batch violates schemas."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-batch-fail")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("claude", "WO-000", ("workspace/**",), (), "read-write").freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file", "batch_write_files"))

    tools = ToolGateway(ws, boundary, contract, policy, "s", "a", "claude", "mock")

    bad_batch = [
        {"path": "README.md", "content": "# Good Readme"},
        {
            "path": ".sync/work-orders/ACTIVE/WO-001.yaml",
            "content": yaml.safe_dump({
                "id": "WO-001",
                "type": "INVALID_TYPE",  # Will fail schema validation!
                "title": "Bad WO",
                "status": "ACTIVE",
            }),
        },
    ]

    with pytest.raises(PermissionError) as exc_info:
        tools.batch_write_files(bad_batch)
    assert "Batch write failed" in str(exc_info.value)
    assert "'INVALID_TYPE' is not one of" in str(exc_info.value)


def test_provider_gateway_caches_repeated_static_schema_reads(tmp_path: Path):
    """ProviderGateway must deduplicate repeated reads of schemas without consuming tool budget."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-gw-cache")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("claude", "WO-000", ("workspace/**",), (), "read-write").freeze()
    policy = AuthorizationPolicy.permit("test", ("read_file", "write_file"))

    tools = ToolGateway(ws, boundary, contract, policy, "s", "a", "claude", "mock")

    # Author a schema file in the workspace
    schema_content = json.dumps({"title": "Mock Schema", "type": "object"})
    tools.write_file("schemas/work-order.schema.json", schema_content)

    mock_adapter = MagicMock()
    pg = ProviderGateway(mock_adapter, tools, contract=contract)

    # First read: consumes 1 tool call
    out1 = pg.execute_tool_call(ToolCallRequest(id="c1", name="read_file", arguments={"path": "schemas/work-order.schema.json"}))
    assert "Mock Schema" in out1
    assert pg.total_tool_calls == 1

    # Second read: served from cache, does NOT increment total_tool_calls
    out2 = pg.execute_tool_call(ToolCallRequest(id="c2", name="read_file", arguments={"path": "schemas/work-order.schema.json"}))
    assert "[CACHE HIT: Reused verified static context" in out2
    assert pg.total_tool_calls == 1, "Repeated static schema read must not increment total_tool_calls"


def test_provider_gateway_warns_when_budget_is_low(tmp_path: Path):
    """ProviderGateway must append a budget warning when remaining tool calls <= 3."""
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-gw-warn")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = KernelContract("codex", "WO-000", ("workspace/**",), (), "read-write", budget={"max_tool_calls": 5}).freeze()
    policy = AuthorizationPolicy.permit("test", ("write_file",))

    tools = ToolGateway(ws, boundary, contract, policy, "s", "a", "codex", "mock")

    mock_adapter = MagicMock()
    del mock_adapter.stream
    from validators.kernel.providers.models import ProviderResponse, Message, ToolCallRequest, TokenUsage
    tc = ToolCallRequest(id="tc-1", name="write_file", arguments={"path": "file1.txt", "content": "1"})
    mock_resp = ProviderResponse(
        message=Message.assistant("", tool_calls=[tc]),
        usage=TokenUsage(),
    )
    mock_adapter.complete.return_value = mock_resp

    pg = ProviderGateway(mock_adapter, tools, contract=contract)

    # Execute 3 tool calls manually
    pg.execute_tool_call(ToolCallRequest(id="1", name="write_file", arguments={"path": "f1.txt", "content": "c1"}))
    pg.execute_tool_call(ToolCallRequest(id="2", name="write_file", arguments={"path": "f2.txt", "content": "c2"}))
    pg.execute_tool_call(ToolCallRequest(id="3", name="write_file", arguments={"path": "f3.txt", "content": "c3"}))
    assert pg.total_tool_calls == 3

    # Now run a turn in run_loop: effective_max is 5, total is 3, remaining is 2 (<= 3)
    messages = [Message.user("Do work")]
    try:
        pg.run_loop(messages, max_turns=1, max_tool_calls=5)
    except (ToolLimitExceededError, ToolLoopExhaustedError):
        pass

    # A budget warning must have been delivered into messages
    warning_msgs = [m.content for m in messages if "[BUDGET WARNING:" in str(m.content)]
    assert len(warning_msgs) > 0, "Budget warning must be appended when tool budget is low"
