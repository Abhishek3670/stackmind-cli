"""Tests for autonomous Architect dispatch (trigger_goal and delegate_task hybrid pattern)."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from validators.kernel.operations import OperationType
from validators.kernel.identity import get_role_policy
from validators.kernel.providers.gateway import (
    get_curated_tools_for_phase,
    STANDARD_KERNEL_TOOLS,
)
from validators.kernel.contract import AgentContract
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.operations import OperationJournal
from validators.kernel.workspace import ScratchWorkspace
from validators.kernel.tools import ToolGateway


def test_operation_types_and_policy():
    """Verify TRIGGER_GOAL and DELEGATE_TASK are registered and authorized for Claude."""
    assert OperationType.TRIGGER_GOAL.value == "trigger_goal"
    assert OperationType.DELEGATE_TASK.value == "delegate_task"

    policy = get_role_policy("claude")
    assert policy.permits("trigger_goal")
    assert policy.permits("delegate_task")

    arch_policy = get_role_policy("architecture")
    assert arch_policy.permits("trigger_goal")
    assert arch_policy.permits("delegate_task")


def test_curated_tools_for_architect():
    """Verify trigger_goal and delegate_task are available to the Architect in ad-hoc mode."""
    tools = get_curated_tools_for_phase("claude", phase=None)
    tool_names = {t.name for t in tools}
    assert "trigger_goal" in tool_names
    assert "delegate_task" in tool_names


def test_tool_gateway_trigger_goal():
    """Test ToolGateway.trigger_goal recording."""
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ws = ScratchWorkspace.create(tmp_path, "test-attempt")
        contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=("workspace/**",),
            deny=(),
            write_mode="read-write",
        ).freeze()
        policy = get_role_policy("claude")
        boundary = RuntimeBoundary(OperationJournal())

        gw = ToolGateway(
            workspace=ws,
            boundary=boundary,
            contract=contract,
            policy=policy,
            session_id="test-session",
            attempt_id="test-attempt",
            actor_id="claude",
            provider_id="anthropic",
        )

        res = gw.trigger_goal(
            goal="Add interactive floating particles and whimsical dust animation",
            rationale="User requested interactive atmospheric effects",
        )
        assert res["status"] == "triggered"
        assert gw.triggered_goal is not None
        assert gw.triggered_goal["goal"] == "Add interactive floating particles and whimsical dust animation"
        assert gw.triggered_goal["triggered_by"] == "claude"


def test_tool_gateway_delegate_task():
    """Test ToolGateway.delegate_task recording."""
    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ws = ScratchWorkspace.create(tmp_path, "test-attempt")
        contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=("workspace/**",),
            deny=(),
            write_mode="read-write",
        ).freeze()
        policy = get_role_policy("claude")
        boundary = RuntimeBoundary(OperationJournal())

        gw = ToolGateway(
            workspace=ws,
            boundary=boundary,
            contract=contract,
            policy=policy,
            session_id="test-session",
            attempt_id="test-attempt",
            actor_id="claude",
            provider_id="anthropic",
        )

        res = gw.delegate_task(
            target_agent="frontend",
            instructions="Inject floating petals canvas script and CSS transitions",
            target_files=["index.html", "assets/js/main.js"],
        )
        assert res["status"] == "delegated"
        assert res["agent"] == "gemini"
        assert res["role"] == "frontend"
        assert len(gw.delegated_tasks) == 1
        d = gw.delegated_tasks[0]
        assert d["agent"] == "gemini"
        assert d["role"] == "frontend"
        assert d["target_files"] == ["index.html", "assets/js/main.js"]
        assert "floating petals" in d["instructions"]

        # Check that assignment notice was written to inbox
        inbox_file = list((ws.root / ".sync" / "inbox" / "gemini").glob("*_claude_delegation.md"))
        assert len(inbox_file) == 1
        content = inbox_file[0].read_text(encoding="utf-8")
        assert "Floating Petals" in content or "floating petals" in content


def test_provider_gateway_translation_and_write_file_guidance():
    """Test ProviderGateway tool execution and Architect guidance on denied writes."""
    from validators.kernel.providers.gateway import ProviderGateway
    from validators.kernel.session import Attempt

    with TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        ws = ScratchWorkspace.create(tmp_path, "test-attempt")
        # Contract denies index.html for Claude
        contract = AgentContract(
            agent_id="claude",
            work_order="WO-000",
            allow=("workspace/.sync/**",),
            deny=("workspace/index.html",),
            write_mode="read-write",
        ).freeze()
        policy = get_role_policy("claude")
        boundary = RuntimeBoundary(OperationJournal())

        gw = ToolGateway(
            workspace=ws,
            boundary=boundary,
            contract=contract,
            policy=policy,
            session_id="test-session",
            attempt_id="test-attempt",
            actor_id="claude",
            provider_id="anthropic",
        )
        attempt = Attempt("test-attempt", contract)
        pgw = ProviderGateway(None, gw, attempt=attempt, contract=contract)

        # 1. Test execute_tool_call for delegate_task
        res1 = pgw.execute_tool_call("delegate_task", cancellation_token={
            "agent": "frontend",
            "instructions": "Add petals effect",
            "files": ["index.html"],
        })
        assert "Successfully delegated task to Frontend (gemini)" in res1
        assert len(gw.delegated_tasks) == 1

        # 2. Test execute_tool_call for trigger_goal
        res2 = pgw.execute_tool_call("trigger_goal", cancellation_token={
            "goal": "Build checkout flow",
            "rationale": "High priority",
        })
        assert "Autonomous goal triggered" in res2
        assert gw.triggered_goal is not None

        # 3. Test write_file on index.html returning guidance
        denied_res = pgw.execute_tool_call("write_file", cancellation_token={
            "path": "index.html",
            "content": "<h1>Hello</h1>",
        })
        assert "PermissionError" in denied_res
        assert "GUIDANCE FOR ARCHITECT" in denied_res
        assert "delegate_task" in denied_res
        assert "trigger_goal" in denied_res


def test_auto_dispatch_delegated_task_in_manager():
    """Verify that manager._run_turn dispatches delegated tasks without parent operation terminal errors."""
    from unittest.mock import MagicMock
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage

    with TemporaryDirectory() as tmpdir:
        ws_path = Path(tmpdir)
        mgr = SessionManager(DaemonStorage(ws_path))
        sess = mgr.create_session("claude", "test", {}, str(ws_path))
        session_id = sess["session_id"]

        mock_result = MagicMock()
        mock_result.status = "completed"
        mock_result.persisted = True
        mock_result.reason = None
        mock_result.report_path = None
        mock_result.meta = {
            "summary": "Delegated task to frontend",
            "report_markdown": "Delegated task to frontend",
            "delegated_tasks": [
                {
                    "agent": "gemini",
                    "role": "frontend",
                    "instructions": "Update index.html with crochet",
                    "target_files": ["index.html"],
                }
            ],
        }
        mock_result.payload = {}

        # Mock runner factory to return delegated_tasks only for claude
        def _factory(ws, ag):
            mock_runner = MagicMock()
            if ag == "claude":
                mock_runner.run_once.return_value = mock_result
            else:
                worker_res = MagicMock()
                worker_res.status = "completed"
                worker_res.persisted = True
                worker_res.reason = None
                worker_res.report_path = None
                worker_res.meta = {"summary": "Gemini updated index.html"}
                worker_res.payload = {"modified_files": ["index.html"]}
                mock_runner.run_once.return_value = worker_res
            mock_runner.backend_id = "test"
            mock_runner.backend_model = "test-model"
            return mock_runner

        mgr._runner_factory = _factory

        turn_op = mgr.start_turn(session_id, "continue")
        op_id = turn_op["operation_id"]

        if op_id in mgr._turn_threads:
            mgr._turn_threads[op_id].join(timeout=5.0)

        # Also wait for any child/delegated turn threads
        for tid, t in list(mgr._turn_threads.items()):
            if t != mgr._turn_threads.get(op_id):
                t.join(timeout=5.0)

        events = mgr.events.events(session_id)
        error_events = [
            e for e in events
            if (getattr(e, "name", None) or (e.get("name") if isinstance(e, dict) else None)) == "error"
        ]
        assert not error_events, f"Found error events: {error_events}"

        delegated_events = [
            e for e in events
            if (getattr(e, "name", None) or (e.get("name") if isinstance(e, dict) else None)) == "task.auto_delegated"
        ]
        assert len(delegated_events) == 1
        d_payload = getattr(delegated_events[0], "payload", {}) if hasattr(delegated_events[0], "payload") else delegated_events[0].get("payload", {})
        assert d_payload.get("agent_id") == "gemini"

        gemini_ops = [
            op for op in mgr.list_operations(session_id)
            if op.get("agent_id") == "gemini" or op.get("role") == "frontend"
        ]
        assert len(gemini_ops) >= 1


