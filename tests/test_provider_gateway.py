"""Unit and integration tests for Phase P2 Provider Gateway and Adapters (WO-003)."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from validators.kernel import (
    AgentSession,
    AuthenticationError,
    AuthorizationPolicy,
    BudgetExceededError,
    ContextLengthExceededError,
    ContractNormalizer,
    Message,
    OpenAICompatibleAdapter,
    OperationJournal,
    ProviderGateway,
    RateLimitError,
    RuntimeBoundary,
    ScratchWorkspace,
    TimeoutError,
    ToolGateway,
)


def _setup_test_gateway(tmp_path, max_tokens: int = 10000, deny_paths: list[str] | None = None):
    authoritative = tmp_path / "authoritative"
    authoritative.mkdir(parents=True, exist_ok=True)
    (authoritative / "tracked.txt").write_text("live baseline", encoding="utf-8")

    workspace = ScratchWorkspace.create(authoritative, "attempt-p2")
    denied = ["authoritative/**"]
    if deny_paths:
        denied.extend(deny_paths)

    contract = ContractNormalizer.normalize({
        "agent": "codex",
        "wo": "WO-003",
        "scope": {
            "allow": ["workspace/**", "graph/**"],
            "deny": denied,
            "write": "read-write",
        },
        "budget": {
            "max_tokens": max_tokens,
            "max_files_touched": 12,
        },
    })
    policy = AuthorizationPolicy.permit("human-approved", [
        "read_file", "write_file", "run_command", "query_graph",
    ])
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    session = AgentSession("codex", "mock-provider", str(authoritative), session_id="session-p2")
    attempt = session.create_attempt(contract, attempt_id="attempt-p2")

    tools = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="session-p2",
        attempt_id="attempt-p2",
        actor_id="codex",
        provider_id="mock-provider",
        graph_query=lambda query: {"result": f"knowledge for {query}"},
    )
    return authoritative, workspace, tools, journal, attempt, contract


def test_provider_gateway_end_to_end_loop_with_tool_execution(tmp_path):
    authoritative, workspace, tool_gw, journal, attempt, contract = _setup_test_gateway(tmp_path)

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        messages = payload.get("messages", [])

        if turn_count == 1:
            # Model returns tool call to write_file
            return {
                "id": "resp-1",
                "model": "test-gpt",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "I will write the release notes.",
                        "tool_calls": [{
                            "id": "call-write-1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "RELEASE.md", "content": "# Release v3.1.0\nPhase P2 complete."}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            }
        elif turn_count == 2:
            # Check that tool output was included in messages
            assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call-write-1" for m in messages)
            return {
                "id": "resp-2",
                "model": "test-gpt",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Release notes have been written successfully and the task is complete.",
                    },
                }],
                "usage": {"prompt_tokens": 160, "completion_tokens": 20, "total_tokens": 180},
            }
        raise AssertionError("Unexpected turn in test")

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com",
        model="test-gpt",
        transport=mock_transport,
    )
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    messages = [
        Message.system("You are a governed backend worker."),
        Message.user("Please write the release notes to RELEASE.md."),
    ]

    history = gateway.run_loop(messages, max_turns=5)

    assert len(history) == 5  # system, user, assistant(tool_call), tool(result), assistant(final)
    assert history[-1].role == "assistant"
    assert "successfully" in history[-1].content

    # Verify scratch workspace contains the file
    assert (workspace.root / "RELEASE.md").exists()
    assert "Phase P2 complete." in (workspace.root / "RELEASE.md").read_text(encoding="utf-8")

    # Authoritative repository remains untouched
    assert not (authoritative / "RELEASE.md").exists()
    assert (authoritative / "tracked.txt").read_text(encoding="utf-8") == "live baseline"

    # Verify journal recorded the write operation
    write_records = [r for r in journal.records if r.request.operation_type.value == "write_file"]
    assert len(write_records) == 1
    assert write_records[0].authorized is True
    assert write_records[0].completed_at is not None
    assert write_records[0].request.session_id == "session-p2"
    assert write_records[0].request.attempt_id == "attempt-p2"

    # Verify token usage tracking
    assert gateway.total_usage.prompt_tokens == 280
    assert gateway.total_usage.completion_tokens == 50
    assert gateway.total_usage.total_tokens == 330
    assert attempt.usage.get("total_tokens") == 330


def test_provider_gateway_handles_scope_denial(tmp_path):
    authoritative, workspace, tool_gw, journal, attempt, contract = _setup_test_gateway(
        tmp_path, deny_paths=["workspace/restricted/**"]
    )

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            return {
                "id": "resp-1",
                "model": "test-gpt",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Attempting to access restricted path.",
                        "tool_calls": [{
                            "id": "call-deny-1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "restricted/secret.txt", "content": "data"}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }
        else:
            return {
                "id": "resp-2",
                "model": "test-gpt",
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Operation failed as target is restricted.",
                    },
                }],
                "usage": {"prompt_tokens": 130, "completion_tokens": 15, "total_tokens": 145},
            }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    messages = [Message.user("Write restricted file")]
    history = gateway.run_loop(messages, max_turns=3)

    tool_msg = [m for m in history if m.role == "tool"][0]
    assert "PermissionError" in tool_msg.content or "target is explicitly denied" in tool_msg.content

    # Journal records denial
    denied_records = [r for r in journal.records if not r.authorized]
    assert len(denied_records) == 1
    assert denied_records[0].reason == "target is explicitly denied"
    assert not (workspace.root / "restricted" / "secret.txt").exists()


def test_provider_gateway_budget_enforcement(tmp_path):
    authoritative, workspace, tool_gw, journal, attempt, contract = _setup_test_gateway(
        tmp_path, max_tokens=200
    )

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        return {
            "id": "resp-budget",
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Response"},
            }],
            "usage": {"prompt_tokens": 150, "completion_tokens": 80, "total_tokens": 230},
        }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    with pytest.raises(BudgetExceededError) as exc_info:
        gateway.execute_turn([Message.user("Hello")])

    assert exc_info.value.tokens_used == 230
    assert exc_info.value.max_tokens == 200
    assert "Contract token budget exceeded" in str(exc_info.value)


def test_provider_error_taxonomy_mapping():
    adapter = OpenAICompatibleAdapter(provider_name="test-provider")

    auth_err = adapter._map_http_error(401, '{"error": {"message": "Invalid API key"}}')
    assert isinstance(auth_err, AuthenticationError)
    assert auth_err.status_code == 401
    assert auth_err.retryable is False

    rate_err = adapter._map_http_error(429, '{"error": "Too Many Requests"}')
    assert isinstance(rate_err, RateLimitError)
    assert rate_err.status_code == 429
    assert rate_err.retryable is True

    timeout_err = adapter._map_http_error(504, '{"error": "Gateway Timeout"}')
    assert isinstance(timeout_err, TimeoutError)
    assert timeout_err.retryable is True

    ctx_err = adapter._map_http_error(400, '{"error": {"message": "This model\'s maximum context length is 8192 tokens."}}')
    assert isinstance(ctx_err, ContextLengthExceededError)
    assert ctx_err.status_code == 400
    assert ctx_err.retryable is False


def test_provider_adapter_streaming_and_cancellation():
    sse_events = [
        {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": " world!"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]

    adapter = OpenAICompatibleAdapter(transport=lambda payload, stream, timeout: sse_events)

    chunks = list(adapter.stream([Message.user("Hi")]))
    assert len(chunks) == 2
    assert chunks[0].delta_content == "Hello"
    assert chunks[1].delta_content == " world!"
    assert chunks[1].finish_reason == "stop"
    assert chunks[1].usage.total_tokens == 15

    # Test cancellation token
    with pytest.raises(TimeoutError) as exc:
        list(adapter.stream([Message.user("Hi")], cancellation_token=lambda: True))
    assert "cancelled" in str(exc.value)


def test_provider_gateway_run_command_and_query_graph(tmp_path):
    authoritative, workspace, tool_gw, journal, attempt, contract = _setup_test_gateway(tmp_path)
    # Interpreter denylist (Phase 2): code-string flags are denied at the
    # agent boundary, so the run_command vector executes a script file
    # (pre-written into the scratch workspace) instead of python -c.
    (workspace.root / "hello_task.py").write_text(
        "print('hello-from-sandbox')", encoding="utf-8"
    )

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            return {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "tc-cmd",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": json.dumps({"command": [sys.executable, "hello_task.py"]}),
                                },
                            },
                            {
                                "id": "tc-graph",
                                "type": "function",
                                "function": {
                                    "name": "query_graph",
                                    "arguments": json.dumps({"query": "AuthService"}),
                                },
                            },
                        ],
                    },
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            }
        else:
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Commands executed."},
                }],
                "usage": {"prompt_tokens": 80, "completion_tokens": 10, "total_tokens": 90},
            }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract)

    messages = [Message.user("Run command and query")]
    history = gateway.run_loop(messages, max_turns=3)

    tool_outputs = [m.content for m in history if m.role == "tool"]
    assert len(tool_outputs) == 2
    assert "hello-from-sandbox" in tool_outputs[0]
    assert "knowledge for AuthService" in tool_outputs[1]

    # Verify journal has recorded run_command and query_graph
    cmd_record = [r for r in journal.records if r.request.operation_type.value == "run_command"][0]
    graph_record = [r for r in journal.records if r.request.operation_type.value == "query_graph"][0]
    assert cmd_record.authorized is True
    assert graph_record.authorized is True


def test_get_curated_tools_for_phase():
    from validators.kernel.providers.gateway import (
        get_curated_tools_for_phase,
        PLANNING_CORE_TOOL_NAMES,
        AUTHORING_CORE_TOOL_NAMES,
        ARCHITECTURE_INVESTIGATION_TOOL_NAMES,
    )

    # Planning phase for architect
    planning_tools = get_curated_tools_for_phase("claude", phase="planning")
    tool_names = {t.name for t in planning_tools}
    for name in PLANNING_CORE_TOOL_NAMES:
        assert name in tool_names
    assert "request_tools" in tool_names
    # Should be lean: 5 tools max (4 core + request_tools)
    assert len(planning_tools) <= 5

    # Authoring phase for architect
    authoring_tools = get_curated_tools_for_phase("claude", phase="authoring")
    authoring_names = {t.name for t in authoring_tools}
    for name in AUTHORING_CORE_TOOL_NAMES:
        assert name in authoring_names
    assert "request_tools" in authoring_names
    assert len(authoring_tools) <= 4

    # Investigation / general architecture phase
    arch_tools = get_curated_tools_for_phase("claude", phase=None)
    arch_names = {t.name for t in arch_tools}
    for name in ARCHITECTURE_INVESTIGATION_TOOL_NAMES:
        assert name in arch_names
    assert "request_tools" in arch_names

    # Local Ollama backend trims diagnostic bloat
    ollama_tools = get_curated_tools_for_phase("codex", backend_id="ollama")
    ollama_names = {t.name for t in ollama_tools}
    assert "checkpoint" not in ollama_names
    assert "skill_mine" not in ollama_names
    assert "request_tools" in ollama_names


def test_request_tools_dynamic_activation(tmp_path):
    authoritative, workspace, tool_gw, journal, attempt, contract = _setup_test_gateway(tmp_path)

    turn_count = 0

    def mock_transport(payload: dict[str, Any], stream: bool, timeout: float | None) -> dict[str, Any]:
        nonlocal turn_count
        turn_count += 1
        tools_sent = payload.get("tools", [])
        tool_names = [t["function"]["name"] for t in tools_sent]

        if turn_count == 1:
            # Initially only read_file, write_file, request_tools are active
            assert "query_graph" not in tool_names
            return {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "tc-req",
                            "type": "function",
                            "function": {
                                "name": "request_tools",
                                "arguments": json.dumps({"query": "knowledge"}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
            }
        elif turn_count == 2:
            # After request_tools('knowledge'), query_graph should be dynamically added to active tools!
            assert "query_graph" in tool_names
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Tools activated and used."},
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            }

    adapter = OpenAICompatibleAdapter(transport=mock_transport)
    from validators.kernel.providers.gateway import STANDARD_KERNEL_TOOLS
    initial_tools = [t for t in STANDARD_KERNEL_TOOLS if t.name in ("read_file", "write_file", "request_tools")]
    gateway = ProviderGateway(adapter, tool_gw, attempt=attempt, contract=contract, tools=initial_tools)

    messages = [Message.user("Need graph tools")]
    history = gateway.run_loop(messages, max_turns=3)

    tool_outputs = [m.content for m in history if m.role == "tool"]
    assert any("Successfully activated" in out for out in tool_outputs)
    assert any(t.name == "query_graph" for t in gateway.active_tools_list)


def test_extract_plan_from_text_fallbacks():
    from validators.harness.plan import extract_plan_from_text, validate_plan_structure

    # 1. Fenced markdown block
    fenced_input = (
        "Here is the plan:\n"
        "```markdown\n"
        "# Project Plan: Weather App\n\n"
        "## Current Architecture\n"
        "Python FastAPI backend with standard library http.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Core API (Agent: codex)\n"
        "  - [ ] Task 1.1: Build endpoints\n"
        "```\n"
        "Hope this helps!"
    )
    p1 = extract_plan_from_text(fenced_input, default_title="Fallback")
    assert p1 is not None
    assert "# Project Plan: Weather App" in p1
    val, _ = validate_plan_structure(p1)
    assert val is True

    # 2. Raw text starting with # Project Plan
    raw_input = (
        "Understood. Starting planning.\n\n"
        "# Project Plan: Task Tracker\n\n"
        "## Current Architecture\n"
        "CLI based tool using SQLite database.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Data Model (Agent: codex)\n"
        "  - [ ] Task 1.1: Create migrations\n\n"
        "```json\n"
        "{\"status\": \"completed\", \"summary\": \"done\"}\n"
        "```"
    )
    p2 = extract_plan_from_text(raw_input, default_title="Fallback")
    assert p2 is not None
    assert "# Project Plan: Task Tracker" in p2
    assert "```json" not in p2
    val, _ = validate_plan_structure(p2)
    assert val is True

    # 3. Text missing top-level heading but having ## Architecture and ## Milestones
    partial_input = (
        "## Architecture\n"
        "Microservices communicating via REST.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Gateway Setup (Agent: codex)\n"
        "  - [ ] Task 1.1: Configure routes\n"
    )
    p3 = extract_plan_from_text(partial_input, default_title="Auth Service")
    assert p3 is not None
    assert "# Project Plan: Auth Service" in p3
    assert "## Architecture" in p3
    val, _ = validate_plan_structure(p3)
    assert val is True


def test_adapter_timeout_handling_and_defaults(monkeypatch):
    import builtins
    import socket
    import urllib.request
    from validators.kernel.providers.adapter import OllamaAdapter, OpenAICompatibleAdapter
    from validators.kernel.providers.errors import TimeoutError as KernelTimeoutError

    # Test defaults
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    monkeypatch.delenv("PROVIDER_TIMEOUT", raising=False)
    ollama = OllamaAdapter()
    assert ollama.default_timeout == 900.0

    openai = OpenAICompatibleAdapter()
    assert openai.default_timeout == 900.0

    # Test env overrides
    monkeypatch.setenv("OLLAMA_TIMEOUT", "450.0")
    monkeypatch.setenv("PROVIDER_TIMEOUT", "180.0")
    assert OllamaAdapter().default_timeout == 450.0
    assert OpenAICompatibleAdapter().default_timeout == 180.0

    # Test explicit timeout argument
    assert OllamaAdapter(default_timeout=60.0).default_timeout == 60.0

    # Test builtins.TimeoutError mapping in urlopen
    def mock_urlopen_builtin_timeout(req, timeout=None):
        raise builtins.TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_builtin_timeout)

    ollama_mock = OllamaAdapter()
    with pytest.raises(KernelTimeoutError) as exc_info:
        ollama_mock.complete([Message.user("Hello")])
    assert "Connection or timeout error" in str(exc_info.value)
    assert exc_info.value.provider == "ollama"

    openai_mock = OpenAICompatibleAdapter()
    with pytest.raises(KernelTimeoutError) as exc_info:
        openai_mock.complete([Message.user("Hello")])
    assert "Connection or timeout error" in str(exc_info.value)
    assert exc_info.value.provider == "openai-compatible"

    # Test socket.timeout mapping in urlopen
    def mock_urlopen_socket_timeout(req, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_socket_timeout)
    with pytest.raises(KernelTimeoutError):
        ollama_mock.complete([Message.user("Hello")])
    with pytest.raises(KernelTimeoutError):
        openai_mock.complete([Message.user("Hello")])


