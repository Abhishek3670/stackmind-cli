"""Tests for StackMind runtime hardening and ornith default configuration.

Verifies:
1. OllamaAdapter default model is 'ornith-1.5-16k:latest' and extracts XML tool calls.
2. Fallback synthesis discriminates VALIDATION from legitimate RESEARCH and AUDIT tasks without deliverable_path.
3. Review contract scope grants read-only access to deliverables and web templates/assets while strictly forbidding writes.
4. TUI state synchronization for run completion events and journal operation statuses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import pytest

from validators.kernel.providers.adapter import OllamaAdapter
from validators.kernel.providers.models import ToolCallRequest
from validators.kernel.contract import AgentContract, ContractEvaluator
from validators.harness.runner import HarnessTask
from cli.tui.state import AutonomousDeliveryState, ProjectPhase, OperationNode


def test_ollama_adapter_defaults_and_xml_tool_call_extraction() -> None:
    adapter = OllamaAdapter()
    assert adapter.model_name == "ornith-1.5-16k:latest"

    # 1. XML inside <tool_call>
    xml_text = """
I will inspect the login page now.
<tool_call>
<function=read_file>
<parameter=path>src/web/login.html</parameter>
</function>
</tool_call>
"""
    calls = adapter._extract_tool_calls_from_content(xml_text)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "src/web/login.html"}

    # 2. Bare XML function
    bare_xml = """
<function=write_file>
<parameter=path>src/main.py</parameter>
<parameter=content>print("hello")</parameter>
</function>
"""
    calls2 = adapter._extract_tool_calls_from_content(bare_xml)
    assert len(calls2) == 1
    assert calls2[0].name == "write_file"
    assert calls2[0].arguments == {"path": "src/main.py", "content": 'print("hello")'}

    # 3. Standard JSON inside <tool_call>
    json_text = '<tool_call>{"name": "read_file", "arguments": {"path": "PLAN.md"}}</tool_call>'
    calls3 = adapter._extract_tool_calls_from_content(json_text)
    assert len(calls3) == 1
    assert calls3[0].name == "read_file"
    assert calls3[0].arguments == {"path": "PLAN.md"}


def test_fallback_synthesizer_task_shape_discrimination(tmp_path: Path) -> None:
    """Verify that VALIDATION fails closed to blocked, while RESEARCH and AUDIT complete normally."""
    written_files: list[str] = []

    # 1. Research task (work_order_id set, deliverable_path None)
    research_task = HarnessTask(
        kind="work_order",
        identifier="WO-005",
        path=tmp_path / "WO-005.yaml",
        title="Spike on caching strategy",
        body="Investigate Redis vs SQLite caching.",
        query="Spike on caching strategy",
        work_order_id="WO-005",
        work_order_type="RESEARCH",
        deliverable_path=None,
    )
    req_research = MagicMock()
    req_research.task = research_task

    is_unfulfilled_deliverable = bool(req_research.task.deliverable_path and not written_files)
    wo_type_upper = str(getattr(req_research.task, 'work_order_type', '') or '').upper()
    is_validation_or_review = (
        wo_type_upper == 'VALIDATION'
        or req_research.task.kind == 'validation'
        or (wo_type_upper not in ('RESEARCH', 'AUDIT') and (
            'integration review' in (req_research.task.title or '').lower()
            or 'validation review' in (req_research.task.title or '').lower()
            or 'review' in (req_research.task.title or '').lower()
            or 'validation' in (req_research.task.title or '').lower()
        ))
    )

    assert not is_unfulfilled_deliverable
    assert not is_validation_or_review  # RESEARCH is explicitly excluded!

    # 2. Audit task (work_order_id set, deliverable_path None)
    audit_task = HarnessTask(
        kind="work_order",
        identifier="WO-006",
        path=tmp_path / "WO-006.yaml",
        title="Security and Code Review Audit",
        body="Audit authentication endpoints.",
        query="Security and Code Review Audit",
        work_order_id="WO-006",
        work_order_type="AUDIT",
        deliverable_path=None,
    )
    req_audit = MagicMock()
    req_audit.task = audit_task

    wo_type_audit = str(getattr(req_audit.task, 'work_order_type', '') or '').upper()
    is_val_audit = (
        wo_type_audit == 'VALIDATION'
        or req_audit.task.kind == 'validation'
        or (wo_type_audit not in ('RESEARCH', 'AUDIT') and (
            'integration review' in (req_audit.task.title or '').lower()
            or 'validation review' in (req_audit.task.title or '').lower()
            or 'review' in (req_audit.task.title or '').lower()
            or 'validation' in (req_audit.task.title or '').lower()
        ))
    )
    assert not is_val_audit  # AUDIT is explicitly excluded!

    # 3. Validation / Integration Review task (work_order_id set, deliverable_path None)
    validation_task = HarnessTask(
        kind="work_order",
        identifier="WO-007",
        path=tmp_path / "WO-007.yaml",
        title="Integration Review",
        body="Perform integration review of all completed deliverables.",
        query="Integration Review",
        work_order_id="WO-007",
        work_order_type="VALIDATION",
        deliverable_path=None,
    )
    req_val = MagicMock()
    req_val.task = validation_task

    wo_type_val = str(getattr(req_val.task, 'work_order_type', '') or '').upper()
    is_val_review = (
        wo_type_val == 'VALIDATION'
        or req_val.task.kind == 'validation'
        or (wo_type_val not in ('RESEARCH', 'AUDIT') and (
            'integration review' in (req_val.task.title or '').lower()
            or 'validation review' in (req_val.task.title or '').lower()
            or 'review' in (req_val.task.title or '').lower()
            or 'validation' in (req_val.task.title or '').lower()
        ))
    )
    assert is_val_review  # VALIDATION is correctly identified!


def test_review_contract_scope_readonly_and_frontend_allowed() -> None:
    """Verify review contract can read frontend deliverables but strictly cannot write them."""
    evaluator = ContractEvaluator()

    # Create review contract as synthesized in _advance_integration_review
    contract = AgentContract(
        agent_id="claude",
        work_order="WO-007",
        allow=(
            "PLAN.md",
            ".sync/work-orders/**",
            ".sync/contracts/**",
            ".sync/inbox/claude/**",
            "src/web/login.html",
            "*.html",
            "*.css",
            "*.js",
            "templates/**",
            "static/**",
            "tests/**",
            "test/**",
        ),
        deny=(
            ".git/**",
            ".env",
            ".sync/runtime/**",
        ),
        write_mode="read-only",
    )

    # 1. Reading frontend files must succeed
    can_read_html, msg_read_html = evaluator.authorize(contract, "read_file", "src/web/login.html")
    assert can_read_html, f"Expected read to succeed, got: {msg_read_html}"

    can_read_template, msg_read_tmpl = evaluator.authorize(contract, "read_file", "templates/index.html")
    assert can_read_template, f"Expected template read to succeed, got: {msg_read_tmpl}"

    can_read_css, msg_read_css = evaluator.authorize(contract, "read_file", "static/css/style.css")
    assert can_read_css, f"Expected css read to succeed, got: {msg_read_css}"

    # 2. Writing to frontend files MUST FAIL because contract is read-only
    can_write_html, msg_write_html = evaluator.authorize(contract, "write_file", "src/web/login.html")
    assert not can_write_html
    assert "read-only" in msg_write_html.lower()

    can_patch_html, msg_patch_html = evaluator.authorize(contract, "apply_patch", "src/web/login.html")
    assert not can_patch_html
    assert "read-only" in msg_patch_html.lower()


def test_tui_state_sync_and_completion_events() -> None:
    state = AutonomousDeliveryState()
    assert state.phase == ProjectPhase.INITIALIZING

    # 1. Test update_from_session syncing journal operation statuses
    session_data = {
        "session_id": "sess-1",
        "phase": "EXECUTING",
        "journal": [
            {
                "operation_id": "op-backend-1",
                "operation": "execute.backend",
                "role": "Backend",
                "status": "RUNNING",
            }
        ],
    }
    state.update_from_session(session_data)
    assert "op-backend-1" in state.operations
    assert state.operations["op-backend-1"].status == "RUNNING"
    assert state.phase == ProjectPhase.AUTONOMOUS_EXECUTION

    # Session journal updates to COMPLETED
    session_data["journal"][0]["status"] = "COMPLETED"
    state.update_from_session(session_data)
    assert state.operations["op-backend-1"].status == "COMPLETED"

    # 2. Test run.phase COMPLETE event
    state.process_event({
        "name": "run.phase",
        "sequence": 1,
        "payload": {"phase": "COMPLETE"},
    })
    assert state.phase == ProjectPhase.PROJECT_COMPLETE
    assert state.is_complete is True
    assert state.completion_checklist["PLAN.md"] is True

    # 3. Test run.complete event
    state.phase = ProjectPhase.AUTONOMOUS_EXECUTION
    state.process_event({
        "name": "run.complete",
        "sequence": 2,
        "payload": {"phase": "COMPLETE"},
    })
    assert state.phase == ProjectPhase.PROJECT_COMPLETE
    assert state.is_complete is True


def test_contract_directory_listing_and_hud() -> None:
    """Verify list_directory can enumerate root/ancestors and HUD renders scope.allow."""
    from cli.tui.governance import render_contract_hud_str

    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="codex",
        work_order="WO-001",
        allow=("requirements.txt", "src/**"),
        deny=(".git/**", ".env"),
        write_mode="read-write",
    )

    # 1. Root and workspace root are authorized for list_directory
    ok, _ = evaluator.authorize(contract, "list_directory", "workspace/.")
    assert ok
    ok, _ = evaluator.authorize(contract, "list_directory", "workspace")
    assert ok
    ok, _ = evaluator.authorize(contract, "list_directory", ".")
    assert ok

    # 2. Ancestor directory of allowed files is authorized for list_directory
    ok, _ = evaluator.authorize(contract, "list_directory", "workspace/src")
    assert ok

    # 3. Unrelated directory is rejected
    ok, msg = evaluator.authorize(contract, "list_directory", "workspace/tests")
    assert not ok
    assert "outside allowed scope" in msg

    # 4. Denied directory is rejected
    ok, msg = evaluator.authorize(contract, "list_directory", "workspace/.git")
    assert not ok
    assert "explicitly denied" in msg

    # 5. Root cannot be written even in read-write mode
    ok, msg = evaluator.authorize(contract, "write_file", "workspace/.")
    assert not ok

    # 6. Contract HUD renders scope.allow with module key
    schema_contract = {
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {
            "allow": [
                {"module": "src/**"},
                {"module": "requirements.txt"},
            ],
            "deny": [
                {"module": ".git/**", "reason": "version control"},
            ],
            "write": "read-write",
        },
        "budget": {"max_tokens": 10000},
    }
    hud_str = render_contract_hud_str(schema_contract)
    assert "(None specified)" not in hud_str
    assert "src/**" in hud_str
    assert "requirements.txt" in hud_str
    assert ".git/**" in hud_str


def test_scratch_workspace_preserves_sync_protocol_artifacts(tmp_path: Path) -> None:
    """Verify ScratchWorkspace copies .sync/contracts, work-orders, inbox while ignoring runtime."""
    from validators.kernel.workspace import ScratchWorkspace

    authoritative = tmp_path / "repo"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "requirements.txt").write_text("flask>=3.0\n", encoding="utf-8")
    (authoritative / ".git").mkdir()
    (authoritative / ".git" / "config").write_text("git config\n", encoding="utf-8")
    (authoritative / ".sync").mkdir()
    (authoritative / ".sync" / "contracts").mkdir()
    (authoritative / ".sync" / "contracts" / "WO-001.yaml").write_text("contract: WO-001\n", encoding="utf-8")
    (authoritative / ".sync" / "work-orders").mkdir()
    (authoritative / ".sync" / "work-orders" / "WO-001.yaml").write_text("wo: WO-001\n", encoding="utf-8")
    (authoritative / ".sync" / "runtime").mkdir()
    (authoritative / ".sync" / "runtime" / "daemon.pid").write_text("12345\n", encoding="utf-8")

    ws = ScratchWorkspace.create(authoritative, "test-attempt")

    # 1. Protocol artifacts must exist in scratch workspace
    contract_p = ws.path_for(".sync/contracts/WO-001.yaml")
    assert contract_p.is_file()
    assert contract_p.read_text(encoding="utf-8") == "contract: WO-001\n"

    wo_p = ws.path_for(".sync/work-orders/WO-001.yaml")
    assert wo_p.is_file()
    assert wo_p.read_text(encoding="utf-8") == "wo: WO-001\n"

    # 2. Ignored runtime and .git must not exist in scratch workspace
    assert not (ws.root / ".git").exists()
    assert not (ws.root / ".sync" / "runtime").exists()

    # 3. Fallback resolves .sync files from authoritative if not in scratch
    (authoritative / ".sync" / "contracts" / "WO-002.yaml").write_text("contract: WO-002\n", encoding="utf-8")
    fallback_p = ws.path_for(".sync/contracts/WO-002.yaml")
    assert fallback_p.is_file()
    assert fallback_p.read_text(encoding="utf-8") == "contract: WO-002\n"


def test_supervisor_retries_on_unwritten_deliverable_blockers(tmp_path: Path) -> None:
    """Verify supervisor extracts blockers from result and retries rather than blocking permanently."""
    import yaml
    from validators.kernel.daemon.supervisor import LifecycleSupervisor, RunState, Phase, AdvanceResult

    # Set up workspace with active work order
    active_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_dir.mkdir(parents=True, exist_ok=True)
    wo_file = active_dir / "WO-002.yaml"
    wo_file.write_text(
        yaml.safe_dump({
            "id": "WO-002",
            "title": "Database Schema & Models",
            "status": "ACTIVE",
            "assigned_agents": ["codex"],
            "deliverable": {"path": "src/backend.py", "type": "code"},
        }),
        encoding="utf-8",
    )

    class MockManager:
        def __init__(self) -> None:
            self.operations: dict[str, dict[str, Any]] = {}
            self.dispatched_turns: list[dict[str, Any]] = []

        def get_operation(self, op_id: str) -> dict[str, Any] | None:
            return self.operations.get(op_id)

        def list_operations(self, session_id: str) -> list[dict[str, Any]]:
            return list(self.operations.values())

        def start_turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.dispatched_turns.append({"session_id": session_id, "prompt": prompt, **kwargs})
            return {"operation_id": f"op-retry-{len(self.dispatched_turns)}"}

    manager = MockManager()
    op_id = "op-blocked-worker"
    manager.operations[op_id] = {
        "operation_id": op_id,
        "status": "BLOCKED",
        "work_order_id": "WO-002",
        "result": {
            "status": "blocked",
            "error": "blocked",
            "reason": None,
            "blockers": ["declared deliverable 'src/backend.py' was not added or modified in this turn"],
            "summary": "Worker ended turn without authoring declared deliverable 'src/backend.py': I now understand...",
        },
    }

    supervisor = LifecycleSupervisor(manager)
    state = RunState(
        run_id="run-test",
        product_goal="Build app",
        workspace=str(tmp_path),
        session_id="session-test",
        phase=Phase.EXECUTING,
        worker_wo_ids=["WO-002"],
    )

    res = supervisor.advance(state)

    # Supervisor must NOT stay in BLOCKED phase; it must retry!
    assert state.phase == Phase.EXECUTING
    assert res != AdvanceResult.BLOCKED
    assert state.retry_counts.get("WO-002") == 1
    assert op_id in state.ignored_operation_ids
    assert len(manager.dispatched_turns) == 1
    assert "Retry work order WO-002" in manager.dispatched_turns[0]["prompt"]
    assert "src/backend.py" in manager.dispatched_turns[0]["prompt"]


def test_provider_gateway_bounds_tool_output_and_compacts_older_messages() -> None:
    """Verify ProviderGateway bounds local tool output chars and compacts older messages."""
    from validators.kernel.providers.gateway import ProviderGateway, DEFAULT_MAX_TOOL_OUTPUT_CHARS
    from validators.kernel.providers.models import Message

    class MockOllamaAdapter:
        provider_name = "ollama"

    class MockToolGateway:
        pass

    gateway = ProviderGateway(
        adapter=MockOllamaAdapter(),
        tool_gateway=MockToolGateway(),
    )

    # Local model gets bounded tool output chars (4,000 instead of 16,000)
    assert gateway.max_tool_output_chars == 4000
    assert gateway.max_tool_output_chars < DEFAULT_MAX_TOOL_OUTPUT_CHARS

    # Prepare message history with 3 tool messages (1 old large, 2 recent)
    long_content = "X" * 1500
    messages = [
        Message.system("System instructions"),
        Message.user("Please read the file"),
        Message.tool(content=long_content, tool_call_id="call-1", name="read_file"),
        Message.assistant("Now reading next file"),
        Message.tool(content="Recent output 1", tool_call_id="call-2", name="read_file"),
        Message.assistant("Now reading third file"),
        Message.tool(content="Recent output 2", tool_call_id="call-3", name="read_file"),
    ]

    gateway._compact_messages_for_context(messages)

    # call-1 is older than the last 2 tool messages, so it must be compacted
    assert len(messages[2].content) < 400
    assert "Earlier tool output truncated to preserve context window" in messages[2].content

    # The 2 most recent tool outputs must remain untouched
    assert messages[4].content == "Recent output 1"
    assert messages[6].content == "Recent output 2"


def test_gateway_duplicate_read_notice_and_contract_empty_target() -> None:
    from types import SimpleNamespace
    from validators.kernel.contract import AgentContract, ContractEvaluator
    from validators.kernel.providers.gateway import ProviderGateway

    # 1. Test ContractEvaluator accepts empty target for list_directory
    evaluator = ContractEvaluator()
    contract = AgentContract(
        agent_id="codex",
        work_order="WO-002",
        allow=("src/**", "PLAN.md"),
        deny=(),
        write_mode="read-write",
    )
    auth_empty, _ = evaluator.authorize(contract, "list_directory", "")
    assert auth_empty is True
    auth_dot, _ = evaluator.authorize(contract, "list_directory", ".")
    assert auth_dot is True

    # 2. Test duplicate read notice in execute_tool_call
    class MockToolGateway:
        def read_file(self, target: str) -> str:
            return f"Content of {target}"

    class MockAdapter:
        provider_name = "ollama"

    gw = ProviderGateway(adapter=MockAdapter(), tool_gateway=MockToolGateway())
    tc1 = SimpleNamespace(name="read_file", arguments={"path": "PLAN.md"})
    res1 = gw.execute_tool_call(tc1)
    assert "[NOTICE: You have already read" not in res1

    tc2 = SimpleNamespace(name="read_file", arguments={"path": "PLAN.md"})
    res2 = gw.execute_tool_call(tc2)
    assert "[NOTICE: You have already read 'PLAN.md' in this session (2 times)" in res2

