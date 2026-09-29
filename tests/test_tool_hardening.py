"""Tests for Phase B: Governed Tool & Context Hardening.

Covers:
1. Maximum tool-call limits (cumulative, per-turn, turn exhaustion)
2. No-progress and repeated-call detection (identical calls, ping-pong cycles)
3. Consecutive failure detection
4. Bounded context & tool-result growth (truncation)
5. Cancellation and timeout handling
6. Structured tool failure diagnostics
7. Deterministic termination in AgentRunner (blocked / cancelled)
8. Normal agent completion preservation
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

import pytest
import yaml

from cli.init import init
from validators.kernel.contract import AgentContract, ContractNormalizer
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.providers.adapter import ProviderAdapter
from validators.kernel.providers.errors import (
    BudgetExceededError,
    ConsecutiveToolFailureError,
    NoProgressLoopError,
    OperationCancelledError,
    TimeoutError,
    ToolLimitExceededError,
    ToolLoopExhaustedError,
)
from validators.kernel.providers.gateway import (
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    ProviderGateway,
    detect_tool_cycle,
    truncate_tool_output,
)
from validators.kernel.providers.models import (
    Message,
    ProviderResponse,
    TokenUsage,
    ToolCallRequest,
)
from validators.kernel.session import Attempt
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace
from validators.harness.runner import AgentRunner


class MockSequenceAdapter(ProviderAdapter):
    """Mock adapter returning a predefined sequence of ProviderResponse objects."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        super().__init__(provider_name="mock-provider", model_name="mock-model")
        self.responses = list(responses)
        self.call_count = 0

    def complete(self, messages: list[Message], **kwargs) -> ProviderResponse:
        self.call_count += 1
        if not self.responses:
            return ProviderResponse(
                message=Message.assistant("Done"),
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )
        return self.responses.pop(0)

    def stream(self, messages: list[Message], **kwargs):
        raise NotImplementedError()


def _setup_mock_gateway(tmp_path: Path, adapter: ProviderAdapter) -> tuple[ProviderGateway, ScratchWorkspace]:
    ws_dir = tmp_path / "scratch"
    ws_dir.mkdir(parents=True, exist_ok=True)
    authoritative = tmp_path / "project"
    authoritative.mkdir(parents=True, exist_ok=True)

    contract = ContractNormalizer.normalize({
        "agent_id": "codex",
        "work_order": "WO-001",
        "scope": {"allow": ["*"], "deny": [], "write": "read-write"},
        "budget": {"max_tokens": 10000, "max_tool_calls": 10, "max_turns": 5},
    })
    boundary = type("MockBoundary", (), {
        "submit": lambda *args: type("Record", (), {
            "authorized": True,
            "reason": None,
            "request": type("Req", (), {"operation_id": "op-1"})(),
        })(),
        "journal": type("Journal", (), {"complete": lambda *args: None})(),
    })()
    policy = AuthorizationPolicy.permit(
        "policy-1",
        ("read_file", "write_file", "run_command", "query_graph"),
    )
    workspace = ScratchWorkspace(authoritative_root=authoritative, root=ws_dir, attempt_id="attempt-1")
    tool_gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-1",
        attempt_id="attempt-1",
        actor_id="codex",
        provider_id="mock",
    )
    attempt = Attempt("attempt-1", contract)
    gateway = ProviderGateway(adapter, tool_gateway, attempt=attempt, contract=contract)
    return gateway, workspace


# ─── 1. Maximum Tool-Call Limits ──────────────────────────────────────────


def test_cumulative_tool_call_limit_halts_loop(tmp_path: Path):
    """Loop halts when cumulative tool calls hit max_tool_calls."""
    # Create responses that each emit 1 tool call
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(10)
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    # create files so tool calls succeed
    for i in range(10):
        (ws.root / f"f{i}.py").write_text("code", encoding="utf-8")

    with pytest.raises(ToolLimitExceededError) as exc_info:
        gateway.run_loop([Message.user("go")], max_tool_calls=3)

    assert "cumulative tool call limit (3) reached" in str(exc_info.value).lower()
    assert exc_info.value.max_tool_calls == 3


def test_per_turn_tool_call_limit_halts_turn(tmp_path: Path):
    """Turn halts if model emits more than DEFAULT_MAX_TOOL_CALLS_PER_TURN in one turn."""
    excessive_calls = [
        ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.py"})
        for i in range(DEFAULT_MAX_TOOL_CALLS_PER_TURN + 2)
    ]
    adapter = MockSequenceAdapter([
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=excessive_calls),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
    ])
    gateway, _ = _setup_mock_gateway(tmp_path, adapter)

    with pytest.raises(ToolLimitExceededError) as exc_info:
        gateway.execute_turn([Message.user("go")])

    assert "exceeding maximum allowed per turn" in str(exc_info.value)


def test_turn_exhaustion_without_completion_raises_error(tmp_path: Path):
    """If loop exhausts all max_turns while still emitting tool calls, raises ToolLoopExhaustedError."""
    # 5 turns, each emitting a tool call
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(5)
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    for i in range(5):
        (ws.root / f"f{i}.py").write_text("code", encoding="utf-8")

    with pytest.raises(ToolLoopExhaustedError) as exc_info:
        gateway.run_loop([Message.user("go")], max_turns=3, max_tool_calls=20)

    assert "reached maximum turns (3) without completing" in str(exc_info.value)


# ─── 2. No-Progress & Repeated-Call Detection ─────────────────────────────


def test_identical_tool_call_repetition_halts_loop(tmp_path: Path):
    """Calling the exact same tool with identical arguments 3 times halts the loop."""
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": "loop.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(5)
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    (ws.root / "loop.py").write_text("loop", encoding="utf-8")

    with pytest.raises(NoProgressLoopError) as exc_info:
        gateway.run_loop([Message.user("go")], max_turns=10)

    assert "Pathological loop" in str(exc_info.value)
    assert "repeated 3 times without progress" in str(exc_info.value)


def test_ping_pong_tool_call_cycle_halts_loop(tmp_path: Path):
    """Alternating tool calls (A -> B -> A -> B -> A -> B) are detected and halted."""
    cycle_calls = [
        ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"}),
        ToolCallRequest(id="c2", name="read_file", arguments={"path": "b.py"}),
    ]
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[cycle_calls[i % 2]],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(8)
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    (ws.root / "a.py").write_text("a", encoding="utf-8")
    (ws.root / "b.py").write_text("b", encoding="utf-8")

    with pytest.raises(NoProgressLoopError) as exc_info:
        gateway.run_loop([Message.user("go")], max_turns=10)

    assert "Pathological tool call cycle detected" in str(exc_info.value)


def test_detect_tool_cycle_unit():
    """Unit test for detect_tool_cycle helper."""
    # Cycle length 2 repeated 3 times
    sigs_2 = [
        ("read", '{"path": "a"}'), ("write", '{"path": "b"}'),
        ("read", '{"path": "a"}'), ("write", '{"path": "b"}'),
        ("read", '{"path": "a"}'), ("write", '{"path": "b"}'),
    ]
    assert detect_tool_cycle(sigs_2) is not None
    assert "read -> write" in detect_tool_cycle(sigs_2)

    # Non-repeating sequence
    sigs_clean = [
        ("read", '{"path": "a"}'), ("read", '{"path": "b"}'),
        ("write", '{"path": "c"}'),
    ]
    assert detect_tool_cycle(sigs_clean) is None


# ─── 3. Consecutive Failure Detection ─────────────────────────────────────


def test_consecutive_tool_failures_halt_loop(tmp_path: Path):
    """Loop halts when 3 consecutive tool calls fail."""
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"missing_{i}.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(5)
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, _ = _setup_mock_gateway(tmp_path, adapter)

    with pytest.raises(ConsecutiveToolFailureError) as exc_info:
        gateway.run_loop([Message.user("go")], max_turns=10)

    assert "Multiple consecutive tool failures" in str(exc_info.value)
    assert exc_info.value.failures == 3


# ─── 4. Bounded Context & Truncation ───────────────────────────────────────


def test_tool_output_truncation():
    """Outputs larger than max_chars are safely truncated with head, tail, and diagnostic marker."""
    large_text = "A" * 20_000
    truncated = truncate_tool_output(large_text, max_chars=1_000)
    assert len(truncated) < 20_000
    assert "[... TRUNCATED:" in truncated
    assert "characters omitted" in truncated
    assert truncated.startswith("A" * 500)
    assert truncated.endswith("A" * 500)

    # Short output is untouched
    assert truncate_tool_output("short", max_chars=100) == "short"


# ─── 5. Cancellation and Timeout Handling ─────────────────────────────────


def test_cancellation_stops_tool_loop_immediately(tmp_path: Path):
    """If cancellation token is set, loop raises OperationCancelledError without executing turn."""
    adapter = MockSequenceAdapter([])
    gateway, _ = _setup_mock_gateway(tmp_path, adapter)

    cancel_event = Event()
    cancel_event.set()

    with pytest.raises(OperationCancelledError):
        gateway.run_loop([Message.user("go")], cancellation_token=cancel_event)


def test_timeout_stops_tool_loop(tmp_path: Path):
    """If overall loop timeout expires, TimeoutError is raised."""
    class SlowAdapter(ProviderAdapter):
        def __init__(self):
            super().__init__(provider_name="slow", model_name="slow-model")

        def complete(self, messages: list[Message], **kwargs) -> ProviderResponse:
            time.sleep(0.05)
            return ProviderResponse(
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})],
                ),
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

        def stream(self, messages: list[Message], **kwargs):
            raise NotImplementedError()

    adapter = SlowAdapter()
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    (ws.root / "a.py").write_text("a", encoding="utf-8")

    with pytest.raises(TimeoutError) as exc_info:
        gateway.run_loop([Message.user("go")], max_turns=10, timeout=0.03)

    assert "timed out after" in str(exc_info.value) or "timed out" in str(exc_info.value)


# ─── 6. Structured Tool Failure Diagnostics ───────────────────────────────


def test_tool_call_missing_required_arguments_returns_recovery_guidance(tmp_path: Path):
    """Calling read_file without path returns structured diagnostic guidance."""
    adapter = MockSequenceAdapter([])
    gateway, _ = _setup_mock_gateway(tmp_path, adapter)

    # read_file with empty args
    tc = ToolCallRequest(id="1", name="read_file", arguments={})
    res = gateway.execute_tool_call(tc)
    assert "Error: 'read_file' missing required argument 'path'" in res
    assert "Example:" in res

    # write_file without content
    tc2 = ToolCallRequest(id="2", name="write_file", arguments={"path": "foo.py"})
    res2 = gateway.execute_tool_call(tc2)
    assert "Error: 'write_file' missing required argument" in res2


# ─── 7. Deterministic Termination in AgentRunner ──────────────────────────


def test_runner_returns_blocked_when_tool_loop_limit_exceeded(tmp_path: Path):
    """When tool loop limit is reached, AgentRunner returns blocked status, not failure crash."""
    project = tmp_path / "project"
    init(project, name="TestLoopLimit", no_git=True)

    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-001"]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    (project / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001", "type": "FEATURE", "title": "Loop test",
            "status": "ACTIVE", "priority": "P2", "assigned_agents": ["codex"],
            "dependencies": [], "description": "Loop test",
            "deliverable": {"type": "code", "path": "src/app.py", "description": "Test app"},
        }, sort_keys=False), encoding="utf-8",
    )
    (project / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "status": "ACTIVE", "assigned_agents": ["codex"]}]}, sort_keys=False),
        encoding="utf-8",
    )
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "codex", "work_order": "WO-001",
            "scope": {"allow": [{"module": "*"}], "deny": [], "write": "read-write"},
            "budget": {"max_tokens": 10000, "max_files_touched": 10},
        }, sort_keys=False), encoding="utf-8",
    )

    # Responses that endlessly emit tool calls
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(10)
    ]
    adapter = MockSequenceAdapter(responses)
    runner = AgentRunner(project, "codex", provider_adapter=adapter, max_tool_calls=2)
    result = runner.run_once()

    assert result.status == "blocked"
    assert result.persisted is False
    assert "ToolLimitExceededError" in result.reason or "tool call limit" in result.reason.lower()


def test_runner_returns_cancelled_when_cancellation_requested(tmp_path: Path):
    """When cancellation event is set, runner returns cancelled result."""
    project = tmp_path / "project"
    init(project, name="TestCancel", no_git=True)

    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-001"]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    (project / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001", "type": "FEATURE", "title": "Cancel test",
            "status": "ACTIVE", "priority": "P2", "assigned_agents": ["codex"],
            "dependencies": [], "description": "Cancel test",
            "deliverable": {"type": "code", "path": "src/app.py", "description": "Test app"},
        }, sort_keys=False), encoding="utf-8",
    )
    (project / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({"orders": [{"id": "WO-001", "status": "ACTIVE", "assigned_agents": ["codex"]}]}, sort_keys=False),
        encoding="utf-8",
    )
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "codex", "work_order": "WO-001",
            "scope": {"allow": [{"module": "*"}], "deny": [], "write": "read-write"},
            "budget": {"max_tokens": 10000, "max_files_touched": 10},
        }, sort_keys=False), encoding="utf-8",
    )

    cancel_event = Event()
    cancel_event.set()

    adapter = MockSequenceAdapter([])
    runner = AgentRunner(project, "codex", provider_adapter=adapter)
    result = runner.run_once(cancel_event, "op-cancel-test")

    assert result.status == "cancelled"
    assert result.persisted is False
    assert result.reason == "operation cancelled"


# ─── 8. Normal Agent Completion Preservation ──────────────────────────────


def test_normal_agent_tool_loop_completion_succeeds(tmp_path: Path):
    """Normal turn that calls a tool and then produces a decision completes successfully."""
    responses = [
        # Turn 1: call read_file
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "input.txt"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Turn 2: write_file and finish
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Task done",
                    "report_markdown": "Done",
                    "modified_files": ["output.txt"],
                }),
                tool_calls=[ToolCallRequest(id="c2", name="write_file", arguments={"path": "output.txt", "content": "result"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Turn 3: final answer
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Task fully completed",
                    "report_markdown": "All files written and verified",
                    "modified_files": ["output.txt"],
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    gateway, ws = _setup_mock_gateway(tmp_path, adapter)
    (ws.root / "input.txt").write_text("hello", encoding="utf-8")

    history = gateway.run_loop([Message.user("please process input.txt")])
    assert len(history) > 1
    assert gateway.total_tool_calls == 2
    assert "output.txt" in gateway.written_files
    assert (ws.root / "output.txt").read_text(encoding="utf-8") == "result"


# ─── 9. Replay Fixtures & Guard Finalization Tests ─────────────────────────

AUTH_PY_VALID = '''# Valid Auth implementation
from flask import Flask, request, jsonify
from werkzeug.security import check_password_hash

app = Flask(__name__)
users = {'user1': 'pbkdf2:sha256:150000$abcdefgh$1234567890abcdef'}

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    u = data.get('username')
    p = data.get('password')
    if u in users and check_password_hash(users[u], p):
        return jsonify({'message': 'Login successful'}), 200
    return jsonify({'message': 'Invalid credentials'}), 401
'''


def _setup_runner_wo001(tmp_path: Path, adapter: Any, max_tool_calls: int | None = None) -> tuple[AgentRunner, Path]:
    project = tmp_path / "project"
    init(project, name="ReplayProject", no_git=True)

    # 1. Create pyproject.toml with flask and werkzeug dependencies
    (project / "pyproject.toml").write_text(
        '[project]\nname = "replay-pkg"\nversion = "0.1.0"\ndependencies = [\n    "flask",\n    "werkzeug",\n]\n',
        encoding="utf-8",
    )

    # 2. Companion test file for D024 Gate
    test_file = project / "tests" / "test_auth.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_dummy(): pass\n", encoding="utf-8")

    # 3. Configure TREE.yaml
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-001"]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")

    # 4. Work order WO-001
    (project / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-001", "type": "FEATURE", "title": "Implement Login API Endpoint",
            "status": "ACTIVE", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": [], "description": "Implement login API",
            "deliverable": {"type": "code", "path": "src/api/auth.py", "description": "Login API Endpoint"},
        }, sort_keys=False), encoding="utf-8",
    )
    (project / ".sync" / "work-orders" / "INDEX.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "next_id": 2,
            "last_updated": "2026-09-28T00:00:00+00:00",
            "updated_by": "test",
            "orders": [{
                "id": "WO-001",
                "type": "FEATURE",
                "title": "Implement Login API Endpoint",
                "status": "ACTIVE",
                "priority": "P1",
                "assigned_agents": ["codex"],
                "dependencies": [],
                "created": "2026-09-28T00:00:00+00:00",
                "updated": "2026-09-28T00:00:00+00:00",
                "file": "work-orders/ACTIVE/WO-001.yaml",
            }],
        }, sort_keys=False),
        encoding="utf-8",
    )

    # 5. Contract WO-001
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-001.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "codex", "work_order": "WO-001",
            "scope": {"allow": [{"module": "src/**"}, {"module": "tests/**"}], "deny": [], "write": "read-write"},
            "budget": {"max_tokens": 10000, "max_files_touched": 10},
        }, sort_keys=False), encoding="utf-8",
    )

    runner = AgentRunner(project, "codex", provider_adapter=adapter, max_tool_calls=max_tool_calls)
    return runner, project


def test_replay_original_step1(tmp_path: Path):
    """(a) Original Step 1: read(fail) -> run_command(denied) -> write -> read -> query_graph -> complete.

    Must complete normally with no guard trip.
    """
    responses = [
        # Call 1: read_file (fails)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "src/api/auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 2: run_command (denied)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c2", name="run_command", arguments={"command": ["touch", "src/api/auth.py"]})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 3: write_file (succeeds)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c3", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 4: read_file (succeeds)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c4", name="read_file", arguments={"path": "src/api/auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 5: query_graph (succeeds)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c5", name="query_graph", arguments={"query": "flask login api"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Final Decision
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Implemented login endpoint",
                    "report_markdown": "Successfully implemented login endpoint with hash verification",
                    "modified_files": ["src/api/auth.py"],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert (project / "src" / "api" / "auth.py").is_file() is True
    assert result.meta.get("guard_trip") is None
    assert result.meta.get("decision_source") == "model"


def test_replay_run1_consecutive_command_denials_finalizes(tmp_path: Path):
    """(b) Run 1: read(fail) -> query_graph -> write -> run_command x3 denied.

    Must end in finalization with auth.py going through the gates, not an abort-and-discard.
    """
    responses = [
        # Call 1: read_file (fail)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "src/api/auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 2: query_graph (success)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c2", name="query_graph", arguments={"query": "auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 3: write_file (success)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c3", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 4: run_command 1 (denied)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c4", name="run_command", arguments={"command": ["python", "src/api/auth.py"]})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 5: run_command 2 (denied)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c5", name="run_command", arguments={"command": ["python", "-m", "http.server", "8000"]})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 6: run_command 3 (denied -> trips ConsecutiveToolFailureError)
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c6", name="run_command", arguments={"command": ["python", "src/api/auth.py"]})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Turn 7 (Finalization turn): Model emits final decision JSON
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Implemented login endpoint despite command restrictions",
                    "report_markdown": "Deliverables finalized via tool-less turn",
                    "modified_files": ["src/api/auth.py"],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert (project / "src" / "api" / "auth.py").is_file() is True
    assert result.meta.get("guard_trip") == "ConsecutiveToolFailureError"
    assert result.meta.get("decision_source") == "model"


def test_replay_run2_interleaved_cycle_caught_and_finalizes(tmp_path: Path):
    """(c) Run 2: the 10-call write/command/read cycle.

    Must be caught by E (total occurrence threshold) or end in finalization, with auth.py going through the gates.
    """
    responses = [
        # Call 1: read_file
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "src/api/auth.py"})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 2: query_graph
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c2", name="query_graph", arguments={"query": "auth.py"})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 3: write_file 1
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c3", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 4: run_command 1
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c4", name="run_command", arguments={"command": ["python", "src/api/auth.py"]})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 5: read_file
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c5", name="read_file", arguments={"path": "src/api/auth.py"})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 6: write_file 2
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c6", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 7: run_command 2
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c7", name="run_command", arguments={"command": ["python", "src/api/auth.py"]})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 8: query_graph
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c8", name="query_graph", arguments={"query": "auth.py"})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Call 9: write_file 3 -> trips Item E (total occurrence = 3)
        ProviderResponse(
            message=Message(role="assistant", content=None, tool_calls=[ToolCallRequest(id="c9", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})]),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        # Finalization turn response:
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Completed after loop guard",
                    "report_markdown": "Done",
                    "modified_files": ["src/api/auth.py"],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert (project / "src" / "api" / "auth.py").is_file() is True
    assert result.meta.get("guard_trip") in ("NoProgressLoopError", "ToolLoopExhaustedError")


def test_pathological_zero_writes_hard_blocks(tmp_path: Path):
    """A genuinely pathological run (infinite loop with no writes) still ends deterministically as blocked."""
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": f"nonexistent_{i}.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        for i in range(10)
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "blocked"
    assert result.persisted is False
    assert not (project / "src" / "api" / "auth.py").exists()


def test_cancellation_and_timeout_remain_immediate_without_finalization(tmp_path: Path):
    """Cancellation and wall-clock timeout remain immediate and do not trigger finalization."""
    # 1. Test Cancellation
    cancel_event = Event()
    cancel_event.set()
    adapter = MockSequenceAdapter([])
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once(cancellation=cancel_event)
    assert result.status == "cancelled"
    assert result.persisted is False

    # 2. Test Timeout
    class TimeoutAdapter(ProviderAdapter):
        def __init__(self):
            super().__init__(provider_name="timeout-mock", model_name="m")
        def complete(self, messages, **kwargs):
            raise TimeoutError("Execution timed out after 0.1s")
        def stream(self, messages, **kwargs):
            raise TimeoutError("Execution timed out after 0.1s")

    t_runner, t_project = _setup_runner_wo001(tmp_path / "timeout", TimeoutAdapter())
    t_result = t_runner.run_once()
    assert t_result.status == "blocked"
    assert t_result.persisted is False
    assert "timed out" in t_result.reason.lower()


# ─── Part A: No-Op Deliverable Prevention Tests ──────────────────────────────


def test_replay_f3_f5_query_graph_only_blocks_without_write(tmp_path: Path):
    """Replay post-fix runs F3/F5: query_graph only, no write_file.

    Must block on outcome_verified with explicit blocker diagnostic and NOT persist
    or commit any bookkeeping writes.
    """
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="query_graph", arguments={"query": "auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c2", name="query_graph", arguments={"query": "login endpoint"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Completed research on auth endpoint",
                    "report_markdown": "Researched graph without writing deliverables.",
                    "modified_files": [],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "blocked"
    assert result.persisted is False
    assert "outcome_verified" in result.reason
    assert "declared deliverable 'src/api/auth.py' was not added or modified in this turn" in result.reason
    assert not (project / "src" / "api" / "auth.py").exists()

    # Confirm harness-owned bookkeeping writes were NOT applied to live tree
    inbox_reviews = list((project / ".sync" / "inbox" / "gemma").glob("*review.md"))
    assert len(inbox_reviews) == 0, f"Unexpected review notice written: {inbox_reviews}"
    claude_notices = list((project / ".sync" / "inbox" / "claude").glob("*complete.md"))
    assert len(claude_notices) == 0, f"Unexpected complete notice written: {claude_notices}"


def test_real_write_of_deliverable_completes(tmp_path: Path):
    """Writing the declared deliverable actually satisfies outcome_verified."""
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "src/api/auth.py", "content": AUTH_PY_VALID})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Implemented login endpoint",
                    "report_markdown": "Deliverable written.",
                    "modified_files": ["src/api/auth.py"],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    adapter = MockSequenceAdapter(responses)
    runner, project = _setup_runner_wo001(tmp_path, adapter)
    result = runner.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert (project / "src" / "api" / "auth.py").is_file() is True


def test_retry_modifying_existing_deliverable_completes(tmp_path: Path):
    """When deliverable already exists, modifying it in this turn satisfies outcome_verified."""
    # Pre-create the deliverable in authoritative root before runner executes
    adapter = MockSequenceAdapter([])
    _, project = _setup_runner_wo001(tmp_path, adapter)
    auth_file = project / "src" / "api" / "auth.py"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(AUTH_PY_VALID, encoding="utf-8")

    # Now runner executes a retry that modifies the existing file
    modified_content = AUTH_PY_VALID + "\n# Modified in retry turn\n"
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "src/api/auth.py", "content": modified_content})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Modified existing auth endpoint",
                    "report_markdown": "Updated deliverable.",
                    "modified_files": ["src/api/auth.py"],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    runner2 = AgentRunner(project, "codex", provider_adapter=MockSequenceAdapter(responses))
    result = runner2.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert "Modified in retry turn" in auth_file.read_text(encoding="utf-8")


def test_work_order_without_declared_deliverable_passes_with_task_changes(tmp_path: Path):
    """Work order without deliverable.path (e.g. audit/research) passes when changes occur."""
    adapter = MockSequenceAdapter([])
    _, project = _setup_runner_wo001(tmp_path, adapter)

    # Change WO-001 deliverable to have no path (audit task)
    wo_file = project / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
    wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
    wo_data["deliverable"] = {"type": "code", "description": "Audit report in log"}
    wo_file.write_text(yaml.safe_dump(wo_data, sort_keys=False), encoding="utf-8")

    notes_content = "# Audit notes\n"
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "tests/audit_notes.txt", "content": notes_content})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Completed audit",
                    "report_markdown": "Audit notes saved.",
                    "modified_files": ["tests/audit_notes.txt"],
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    runner2 = AgentRunner(project, "codex", provider_adapter=MockSequenceAdapter(responses))
    result = runner2.run_once()

    assert result.status == "completed"
    assert result.persisted is True
    assert (project / "tests" / "audit_notes.txt").is_file() is True


def test_preexisting_deliverable_noop_turn_blocked_even_with_bookkeeping(tmp_path: Path):
    """Pre-existing deliverable untouched in turn is blocked even when harness stages bookkeeping.

    When deliverable exists before turn and the agent turn only executes query_graph
    or makes no deliverable modifications, outcome_verified must fail closed and
    return status='blocked', persisted=False, even if harness bookkeeping is staged.
    """
    adapter = MockSequenceAdapter([])
    _, project = _setup_runner_wo001(tmp_path, adapter)
    auth_file = project / "src" / "api" / "auth.py"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(AUTH_PY_VALID, encoding="utf-8")

    # Turn only queries graph; does not modify src/api/auth.py
    responses = [
        ProviderResponse(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="query_graph", arguments={"query": "auth.py"})],
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
        ProviderResponse(
            message=Message(
                role="assistant",
                content=json.dumps({
                    "status": "completed",
                    "summary": "Looked up auth endpoint",
                    "report_markdown": "Did not modify deliverable.",
                    "modified_files": [],
                    "release_target": "src/api/auth.py",
                }),
            ),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        ),
    ]
    runner = AgentRunner(project, "codex", provider_adapter=MockSequenceAdapter(responses))
    result = runner.run_once()

    assert result.status == "blocked"
    assert result.persisted is False
    assert "outcome_verified" in result.reason
    assert "declared deliverable 'src/api/auth.py' was not added or modified in this turn" in result.reason

    # Ensure live deliverable content was untouched
    assert auth_file.read_text(encoding="utf-8") == AUTH_PY_VALID

    # Ensure harness bookkeeping was NOT persisted to live tree
    inbox_reviews = list((project / ".sync" / "inbox" / "gemma").glob("*review.md"))
    assert len(inbox_reviews) == 0


