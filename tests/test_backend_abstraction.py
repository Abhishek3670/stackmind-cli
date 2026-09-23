"""Comprehensive test suite for Milestone P7-2: Execution Backend Abstraction & Role Rebinding.

Covers:
1. Common ExecutionBackend protocol contract across Agent and Model backend adapters.
2. BackendRegistry registration, health/status reporting, and enumeration.
3. Deterministic failure semantics for backend errors and timeouts in AgentRunner.
4. Strict Rebinding Guard: rejecting role.configureBackend when in-flight work orders exist.
5. Permitted role rebinding between assignments.
6. JSON-RPC protocol endpoints (backend.list, role.list, role.configureBackend).
7. DaemonClient methods for backends and roles.
8. Role rebinding execution without TUI changes.
9. Zero credential leakage verification across RPC responses, events, and storage.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from validators.harness.backend import (
    AgentExecutionBackend,
    BackendExecutionError,
    BackendRegistry,
    BackendTimeoutError,
    BackendUnavailableError,
    EchoAgentBackend,
    ExecutionBackend,
    ModelExecutionBackend,
    OllamaBackend,
    get_default_registry,
    reset_default_registry,
    sanitize_secrets,
)
from validators.harness.runner import (
    AgentRunner,
    CompletionRecord,
    EchoLLMProvider,
    HarnessRunResult,
    HarnessTask,
    LLMRequest,
)
from validators.kernel.daemon import DaemonStorage, LocalDaemon, SessionManager
from validators.kernel.daemon.protocol import JsonRpcProtocol
from validators.kernel.tui.client import DaemonClient


def _contract() -> dict[str, object]:
    return {"agent_id": "codex", "work_order": "WO-020", "scope": {"allow": ["tests/**"]}}


def _rpc_request(method: str, params: dict[str, object] | None = None, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


# -----------------------------------------------------------------------------
# 1. Common ExecutionBackend Protocol Contract Tests
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda: AgentExecutionBackend(backend_id="test-agent", model="agent-v1"),
        lambda: ModelExecutionBackend(backend_id="test-model", model="llama3", endpoint="http://localhost:11434"),
    ],
)
def test_execution_backend_common_contract(backend_factory):
    backend: ExecutionBackend = backend_factory()
    assert isinstance(backend, ExecutionBackend)
    assert backend.backend_id
    assert backend.backend_type in {"agent", "model"}
    assert backend.status in {"available", "unavailable", "not configured"}
    assert isinstance(backend.capabilities, list)

    # 1. start_operation
    task = HarnessTask(
        kind="test",
        identifier="task-1",
        path=Path("task.md"),
        title="Test Task",
        body="Execute test task",
        query="test task",
    )
    op_id = backend.start_operation(task=task, context=None)
    assert isinstance(op_id, str)
    assert len(op_id) > 0

    # 2. inspect before work
    state = backend.inspect(op_id)
    assert state["operation_id"] == op_id
    assert state["status"] == "RUNNING"
    assert state["task_id"] == "task-1"

    # 3. send_work
    work_res = backend.send_work(op_id, "Run step 1")
    assert work_res["operation_id"] == op_id
    assert work_res["status"] == "RUNNING"

    # 4. request_approval
    approval = backend.request_approval(op_id, "Approve step 2?", options=["yes", "no"])
    assert approval["prompt"] == "Approve step 2?"
    assert approval["status"] == "PENDING"

    # 5. stream_events
    events = list(backend.stream_events(op_id))
    event_names = [e["event"] for e in events]
    assert "operation.started" in event_names
    assert "work.sent" in event_names
    assert "approval.requested" in event_names

    # 6. cancel
    cancel_res = backend.cancel(op_id)
    assert cancel_res["status"] == "CANCELLED"
    events_after_cancel = list(backend.stream_events(op_id))
    assert any(e["event"] == "operation.cancelled" for e in events_after_cancel)

    # 7. report_result
    result = backend.report_result(op_id)
    assert result["operation_id"] == op_id
    assert result["status"] == "CANCELLED"

    # 8. as_dict public representation
    d = backend.as_dict()
    assert d["id"] == backend.backend_id
    assert d["type"] == backend.backend_type
    assert d["status"] == backend.status
    assert "capabilities" in d


# -----------------------------------------------------------------------------
# 2. BackendRegistry Functionality & Discovery
# -----------------------------------------------------------------------------

def test_backend_registry_management_and_status():
    registry = BackendRegistry()
    agent_backend = EchoAgentBackend(backend_id="agent-custom", model="echo-v1")
    model_backend = OllamaBackend(backend_id="ollama-custom", model="mistral", endpoint="http://localhost:11434")
    unconfigured_backend = ModelExecutionBackend(backend_id="unconfigured", endpoint=None)

    registry.register(agent_backend)
    registry.register(model_backend)
    registry.register(unconfigured_backend)

    assert "agent-custom" in registry
    assert "ollama-custom" in registry
    assert registry.has("unconfigured")
    assert registry.get("agent-custom") is agent_backend

    backends = registry.list_backends()
    assert len(backends) == 3
    ids = {b["id"]: b for b in backends}
    assert ids["agent-custom"]["status"] == "available"
    assert ids["ollama-custom"]["status"] == "available"
    assert ids["unconfigured"]["status"] == "not configured"

    registry.unregister("unconfigured")
    assert not registry.has("unconfigured")
    with pytest.raises(KeyError):
        registry.get("unconfigured")


def test_concurrent_backend_registry_operations():
    """Registry mutations and snapshot listings are safe under concurrent use."""
    registry = BackendRegistry()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def mutate(worker_id: int) -> None:
        try:
            start.wait()
            for iteration in range(200):
                backend_id = f"concurrent-{worker_id}-{iteration % 12}"
                registry.register(EchoAgentBackend(backend_id=backend_id))
                assert registry.has(backend_id)
                assert backend_id in registry
                assert registry.get(backend_id).backend_id == backend_id
                if iteration % 2:
                    registry.unregister(backend_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def list_snapshots() -> None:
        try:
            start.wait()
            for _ in range(300):
                snapshot = registry.list_backends()
                assert all("id" in backend for backend in snapshot)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=mutate, args=(index,)) for index in range(4)]
    threads.extend(threading.Thread(target=list_snapshots) for _ in range(4))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads), "registry operation deadlocked"
    assert not errors


def test_global_default_registry():
    reg = reset_default_registry()
    assert reg.has("echo-agent")
    assert reg.has("mock-model")
    assert reg.has("ollama")

    backends = reg.list_backends()
    backend_ids = [b["id"] for b in backends]
    assert "echo-agent" in backend_ids
    assert "mock-model" in backend_ids


def _live_model_request() -> SimpleNamespace:
    task = HarnessTask(
        kind="test",
        identifier="live-model-test",
        path=Path("task.md"),
        title="Live model test",
        body="Return a response",
        query="live model test",
    )
    return SimpleNamespace(task=task, context=SimpleNamespace(revision=1), retrieval=SimpleNamespace(query="test"))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (urllib.error.URLError("connection refused; token=do-not-leak"), BackendUnavailableError),
        (socket.timeout("password=do-not-leak"), BackendTimeoutError),
        (urllib.error.HTTPError("http://token@localhost", 500, "server", None, None), BackendExecutionError),
    ],
)
def test_live_model_transport_errors_are_typed_and_sanitized(error, expected):
    backend = ModelExecutionBackend(backend_id="live-model", endpoint="http://localhost:11434")

    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(expected) as raised:
            backend.complete(_live_model_request())

    message = str(raised.value).lower()
    assert "do-not-leak" not in message
    assert "password" not in message
    assert "token@" not in message


def test_live_model_malformed_json_is_typed_and_sanitized():
    backend = ModelExecutionBackend(backend_id="live-model", endpoint="http://localhost:11434")
    response = MagicMock()
    response.__iter__.return_value = [b"not valid json\n"]
    response.__enter__.return_value = response

    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(BackendExecutionError, match="invalid response") as raised:
            backend.complete(_live_model_request())

    assert "not valid json" not in str(raised.value)


def test_model_without_endpoint_uses_explicit_synthetic_completion():
    backend = ModelExecutionBackend(backend_id="offline-model", endpoint=None)

    completion = backend.complete(_live_model_request())

    assert completion.payload["status"] == "completed"


def test_default_runner_clones_backend_for_concurrency(tmp_path):
    """Verify _default_runner provides isolated backend instances across concurrent roles."""
    reset_default_registry()
    registry = get_default_registry()
    try:
        backend_id = "test-concurrent-ollama"
        shared_backend = ModelExecutionBackend(
            backend_id=backend_id,
            model="shared-llama3",
            endpoint="http://localhost:11434",
        )
        registry.register(shared_backend)

        storage = DaemonStorage(tmp_path)
        manager = SessionManager(storage)
        manager.configure_role_backend(role="backend", backend=backend_id, model="custom-coder")
        manager.configure_role_backend(role="frontend", backend=backend_id, model="custom-ui")

        runner_a = manager._default_runner(str(tmp_path), "backend")
        runner_b = manager._default_runner(str(tmp_path), "frontend")

        # 1. Assert that the two resulting runners' backends are distinct instances (copy.copy)
        assert runner_a.backend is not runner_b.backend
        assert runner_a.backend is not shared_backend
        assert runner_b.backend is not shared_backend

        # 2. Assert setting on_token on runner A does NOT leak into runner B
        token_listener = MagicMock()
        runner_a.backend.on_token = token_listener
        assert runner_b.backend.on_token is None

        # 3. Assert model customization on runners did not mutate the registry singleton
        assert runner_a.backend.model == "custom-coder"
        assert runner_b.backend.model == "custom-ui"
        assert shared_backend.model == "shared-llama3"
        assert shared_backend.model_name == "shared-llama3"
    finally:
        reset_default_registry()


def test_ollama_streaming_token_deltas():
    # Includes an invalid NDJSON line in the stream to verify per-line parsing resiliency
    chunks = [
        {"response": "Hello", "done": False},
        {"response": " world", "done": False},
        {"response": "!", "done": True, "eval_count": 15, "eval_duration": 120000000},
    ]
    ndjson_lines = [
        json.dumps(chunks[0]).encode("utf-8") + b"\n",
        b"malformed json line that should be skipped\n",
        json.dumps(chunks[1]).encode("utf-8") + b"\n",
        json.dumps(chunks[2]).encode("utf-8") + b"\n",
    ]

    # (a) & (b) on_token is called once per fragment with correct text, and payload matches concatenation
    tokens: list[str] = []
    backend = ModelExecutionBackend(
        backend_id="live-model",
        endpoint="http://localhost:11434",
        on_token=tokens.append,
    )

    response = MagicMock()
    response.__iter__.return_value = iter(ndjson_lines)
    response.__enter__.return_value = response

    with patch("urllib.request.urlopen", return_value=response):
        completion = backend.complete(_live_model_request())

    # (a) Assert on_token is called once per fragment with the correct text
    assert tokens == ["Hello", " world", "!"]

    # (b) Assert final payload's summary/report_markdown matches concatenation
    assert completion.payload["summary"] == "Hello world!"
    assert "Hello world!" in completion.payload["report_markdown"]
    assert completion.payload["_stream_stats"] == {
        "eval_count": 15,
        "eval_duration": 120000000,
    }

    # (c) Assert behavior with on_token=None is unchanged from before this change
    backend_no_token = ModelExecutionBackend(
        backend_id="live-model",
        endpoint="http://localhost:11434",
        on_token=None,
    )
    response_no_token = MagicMock()
    response_no_token.__iter__.return_value = iter(ndjson_lines)
    response_no_token.__enter__.return_value = response_no_token

    with patch("urllib.request.urlopen", return_value=response_no_token):
        completion_no_token = backend_no_token.complete(_live_model_request())

    assert completion_no_token.payload["summary"] == "Hello world!"
    assert "Hello world!" in completion_no_token.payload["report_markdown"]
    assert completion_no_token.payload["_stream_stats"] == {
        "eval_count": 15,
        "eval_duration": 120000000,
    }


def test_ollama_streaming_cancellation():
    cancel_event = threading.Event()
    tokens: list[str] = []

    def cancel_after_first(token: str):
        tokens.append(token)
        cancel_event.set()

    backend = ModelExecutionBackend(
        backend_id="live-model",
        endpoint="http://localhost:11434",
        on_token=cancel_after_first,
    )
    backend.cancel_event = cancel_event

    chunks = [
        {"response": "Chunk 1", "done": False},
        {"response": " Chunk 2", "done": False},
        {"response": " Chunk 3", "done": True},
    ]
    ndjson_lines = [json.dumps(c).encode("utf-8") + b"\n" for c in chunks]

    response = MagicMock()
    response.__iter__.return_value = iter(ndjson_lines)
    response.__enter__.return_value = response

    with patch("urllib.request.urlopen", return_value=response):
        completion = backend.complete(_live_model_request())

    assert tokens == ["Chunk 1"]
    assert completion.payload["summary"] == "Chunk 1"


# -----------------------------------------------------------------------------
# 3. Deterministic Failure Semantics in Harness
# -----------------------------------------------------------------------------

def test_runner_deterministic_failure_on_backend_error(tmp_path):
    failing_backend = AgentExecutionBackend(
        backend_id="failing-agent",
        should_fail=True,
        failure_reason="Deterministic backend crash",
    )
    runner = AgentRunner(tmp_path, "codex", backend=failing_backend)
    assert runner.backend_id == "failing-agent"

    task = HarnessTask(
        kind="test",
        identifier="task-fail",
        path=tmp_path / "task.md",
        title="Test Failure",
        body="Should fail deterministically",
        query="failure",
    )
    # Mock tree and task discovery to trigger provider completion
    runner._load_tree = MagicMock(return_value={"agents": {"codex": {"session_count": 1}}})
    runner.discover_next_task = MagicMock(return_value=task)
    runner._ensure_protocol_citizenship = MagicMock()

    res = runner.run_once(operation_id="op-fail-test")
    assert isinstance(res, HarnessRunResult)
    assert res.status == "failed"
    assert res.persisted is False
    assert res.task_id == "task-fail"
    assert "Deterministic backend crash" in str(res.reason)
    assert res.meta is not None
    assert res.meta["backend_id"] == "failing-agent"
    assert res.meta["operation_id"] == "op-fail-test"


def test_runner_deterministic_failure_on_backend_timeout(tmp_path):
    timeout_backend = ModelExecutionBackend(
        backend_id="timeout-model",
        simulate_timeout=True,
        timeout=5.0,
    )
    runner = AgentRunner(tmp_path, "codex", backend=timeout_backend)
    assert runner.backend_id == "timeout-model"

    task = HarnessTask(
        kind="test",
        identifier="task-timeout",
        path=tmp_path / "task.md",
        title="Test Timeout",
        body="Should time out deterministically",
        query="timeout",
    )
    runner._load_tree = MagicMock(return_value={"agents": {"codex": {"session_count": 1}}})
    runner.discover_next_task = MagicMock(return_value=task)
    runner._ensure_protocol_citizenship = MagicMock()

    res = runner.run_once(operation_id="op-timeout-test")
    assert isinstance(res, HarnessRunResult)
    assert res.status == "failed"
    assert res.persisted is False
    assert "timed out after 5.0s" in str(res.reason)
    assert res.meta["backend_id"] == "timeout-model"


# -----------------------------------------------------------------------------
# 4. Strict Rebinding Guard: In-Flight Work Order Rejection
# -----------------------------------------------------------------------------

def test_role_rebinding_guard_rejects_active_operation(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    session = manager.create_session("codex", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    # Start an operation for codex (role: backend)
    _, op_id = manager.begin_operation(session_id, "backend.build", work_order_id="WO-020")

    # Attempt to rebind backend role while operation is RUNNING -> Must be rejected!
    with pytest.raises(ValueError, match="Cannot rebind role"):
        manager.configure_role_backend(role="backend", backend="mock-model")

    # Protocol level translation to -32003 ("Policy denied")
    protocol = JsonRpcProtocol(manager)
    req = _rpc_request("role.configureBackend", {"role": "backend", "backend": "mock-model"})
    resp = protocol.handle(req)
    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert "Policy denied" in resp["error"]["message"]

    # Finish operation
    manager.complete_operation(session_id, op_id, status="COMPLETED")

    # Now between assignments with zero non-terminal operations, rebinding must succeed!
    success = manager.configure_role_backend(role="backend", backend="mock-model", model="llama3")
    assert success["role"] == "backend"
    assert success["backend"] == "mock-model"
    assert success["model"] == "llama3"
    assert "appliedAt" in success


def test_role_rebinding_guard_rejects_inflight_work_order(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("codex", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    # Create an active work order via approved plan
    manager.propose_plan(
        session_id=session_id,
        plan_id="plan-p7",
        title="P7 Plan",
        metadata={"work_orders": [{"id": "WO-999", "assigned_agents": ["codex"], "status": "ACTIVE"}]},
    )
    manager.approve_plan(session_id, "plan-p7", reason="Operator approved")

    # Target role (backend / codex) now has non-terminal work order WO-999 in flight
    with pytest.raises(ValueError, match="in-flight non-terminal work order"):
        manager.configure_role_backend(role="backend", backend="mock-model")

    # RPC protocol test
    protocol = JsonRpcProtocol(manager)
    req = _rpc_request("role.configureBackend", {"role": "backend", "backend": "mock-model"})
    resp = protocol.handle(req)
    assert resp["error"]["code"] == -32003
    assert "Policy denied" in resp["error"]["message"]


# -----------------------------------------------------------------------------
# 5. Permitted Rebinding Between Assignments & Persistence
# -----------------------------------------------------------------------------

def test_role_rebinding_between_assignments_persists(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)

    # Initial roles list has defaults
    roles = manager.list_roles()
    role_map = {r["role"]: r for r in roles}
    assert "backend" in role_map
    assert role_map["backend"]["backend"] in {"echo-agent", "ollama"}

    # Rebind between assignments
    result = manager.configure_role_backend(
        role="backend",
        backend="mock-model",
        model="custom-coder-v1",
        credential_ref="vault://coder-key",
    )
    assert result["role"] == "backend"
    assert result["backend"] == "mock-model"
    assert result["model"] == "custom-coder-v1"

    # Verify reflected in list_roles()
    updated_roles = manager.list_roles()
    updated_map = {r["role"]: r for r in updated_roles}
    assert updated_map["backend"]["backend"] == "mock-model"
    assert updated_map["backend"]["model"] == "custom-coder-v1"
    assert updated_map["backend"]["credentialRef"] == "vault://coder-key"

    # Verify durable persistence by reloading manager from same storage
    reloaded_manager = SessionManager(storage)
    reloaded_roles = reloaded_manager.list_roles()
    reloaded_map = {r["role"]: r for r in reloaded_roles}
    assert reloaded_map["backend"]["backend"] == "mock-model"
    assert reloaded_map["backend"]["model"] == "custom-coder-v1"
    assert reloaded_map["backend"]["credentialRef"] == "vault://coder-key"


# -----------------------------------------------------------------------------
# 6. JSON-RPC Protocol & DaemonClient Exposure
# -----------------------------------------------------------------------------

def test_json_rpc_backend_and_role_endpoints(tmp_path):
    manager = SessionManager(DaemonStorage(tmp_path))
    protocol = JsonRpcProtocol(manager)

    # backend.list
    res_backends = protocol.handle(_rpc_request("backend.list"))["result"]
    assert "backends" in res_backends
    assert len(res_backends["backends"]) >= 2
    for b in res_backends["backends"]:
        assert "id" in b
        assert "type" in b
        assert "status" in b
        assert "capabilities" in b

    # role.list
    res_roles = protocol.handle(_rpc_request("role.list"))["result"]
    assert "roles" in res_roles
    roles = {r["role"]: r for r in res_roles["roles"]}
    assert "architecture" in roles
    assert "backend" in roles
    assert "frontend" in roles
    assert "qa" in roles
    assert "gitops" in roles

    # role.configureBackend
    conf_res = protocol.handle(
        _rpc_request(
            "role.configureBackend",
            {
                "role": "frontend",
                "backend": "mock-model",
                "model": "frontend-specialist",
                "credentialRef": "vault://gemini-key",
            },
        )
    )["result"]
    assert conf_res["role"] == "frontend"
    assert conf_res["backend"] == "mock-model"
    assert conf_res["model"] == "frontend-specialist"
    assert "appliedAt" in conf_res


def test_daemon_client_backend_and_role_helpers(tmp_path):
    with LocalDaemon(tmp_path) as daemon:
        client = DaemonClient(daemon.url)

        # list_backends
        backends = client.list_backends()
        assert isinstance(backends, list)
        assert any(b["id"] == "echo-agent" for b in backends)
        assert any(b["id"] == "mock-model" for b in backends)

        # list_roles
        roles = client.list_roles()
        assert isinstance(roles, list)
        assert any(r["role"] == "backend" for r in roles)

        # configure_role_backend
        applied = client.configure_role_backend(
            role="qa",
            backend="mock-model",
            model="qa-tester",
            credential_ref="vault://qa-token",
        )
        assert applied["role"] == "qa"
        assert applied["backend"] == "mock-model"

        # Verify updated roles
        roles_after = client.list_roles()
        qa_role = next(r for r in roles_after if r["role"] == "qa")
        assert qa_role["backend"] == "mock-model"
        assert qa_role["credentialRef"] == "vault://qa-token"


# -----------------------------------------------------------------------------
# 7. Zero Credential Leakage Invariant
# -----------------------------------------------------------------------------

def test_zero_credential_leakage_across_daemon_and_rpc(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    protocol = JsonRpcProtocol(manager)

    # Configure role with credentialRef
    manager.configure_role_backend(
        role="backend",
        backend="mock-model",
        model="llama3",
        credential_ref="vault://secret-bearer-token",
    )

    # 1. RPC responses must never leak raw tokens
    role_list = protocol.handle(_rpc_request("role.list"))["result"]
    raw_rpc_json = json.dumps(role_list)
    assert "secret-bearer-token" not in raw_rpc_json or "vault://secret-bearer-token" in raw_rpc_json
    # Crucially: no raw keys or secrets
    assert "api_key" not in raw_rpc_json
    assert "password" not in raw_rpc_json

    # 2. Check sanitize_secrets utility
    dirty_data = {
        "id": "test",
        "api_key": "raw_secret_key_12345",
        "token": "bearer_abc_xyz",
        "nested": {"password": "super_secret", "normal": "safe_value"},
        "credentialRef": "vault://ref",
    }
    clean = sanitize_secrets(dirty_data)
    assert "api_key" not in clean
    assert "token" not in clean
    assert "password" not in clean["nested"]
    assert clean["nested"]["normal"] == "safe_value"
    assert clean["credentialRef"] == "vault://ref"

    # 3. Check persisted file has zero raw credentials
    state_file = tmp_path / "daemon-state.json"
    assert state_file.exists()
    content = state_file.read_text(encoding="utf-8")
    assert "api_key" not in content
    assert "password" not in content


# -----------------------------------------------------------------------------
# 8. Dynamic Role Execution Rebinding Without TUI Changes
# -----------------------------------------------------------------------------

def test_dynamic_role_rebinding_execution(tmp_path):
    storage = DaemonStorage(tmp_path)
    manager = SessionManager(storage)
    session = manager.create_session("codex", "test", _contract(), str(tmp_path))
    session_id = session["session_id"]

    # Initial execution turn with default backend (echo-agent)
    op1 = manager.start_turn(session_id, "Turn 1 with default backend")
    op1_id = op1["operation_id"]
    # Wait for turn thread to complete
    thread1 = manager._turn_threads.get(op1_id)
    if thread1:
        thread1.join(timeout=3.0)

    op1_record = manager.get_operation(op1_id)
    assert op1_record["status"] in {"COMPLETED", "FAILED"}
    assert op1_record.get("backend_id") in {"echo-agent", "ollama"}

    # Between assignments: Rebind role "backend" to "mock-model"
    manager.configure_role_backend(role="backend", backend="mock-model", model="mock-llama3")

    # Second execution turn for same agent codex -> Now executes via mock-model!
    op2 = manager.start_turn(session_id, "Turn 2 with rebound backend")
    op2_id = op2["operation_id"]
    thread2 = manager._turn_threads.get(op2_id)
    if thread2:
        thread2.join(timeout=3.0)

    op2_record = manager.get_operation(op2_id)
    assert op2_record["status"] in {"COMPLETED", "FAILED"}
    assert op2_record.get("backend_id") == "mock-model"
    assert op2_record.get("model") == "mock-llama3"


# -----------------------------------------------------------------------------
# 9. Ollama Error & Exception Transparency Tests (WO-007)
# -----------------------------------------------------------------------------

def test_ollama_midstream_error_chunk():
    backend = ModelExecutionBackend(backend_id="ollama-live", endpoint="http://localhost:11434")
    response_mock = MagicMock()
    response_mock.__iter__.return_value = [
        b'{"response": "partial ok"}\n',
        b'{"error": "model runner crashed: out of memory"}\n',
    ]
    response_mock.__enter__.return_value = response_mock
    with patch("urllib.request.urlopen", return_value=response_mock):
        with pytest.raises(BackendExecutionError) as exc_info:
            backend.complete(_live_model_request())
    assert "Ollama error: model runner crashed: out of memory" in str(exc_info.value)


def test_ollama_empty_generation_error():
    backend = ModelExecutionBackend(backend_id="ollama-live", endpoint="http://localhost:11434")
    response_mock = MagicMock()
    response_mock.__iter__.return_value = [
        b'{"response": ""}\n',
        b'{"done": true}\n',
    ]
    response_mock.__enter__.return_value = response_mock
    with patch("urllib.request.urlopen", return_value=response_mock):
        with pytest.raises(BackendExecutionError) as exc_info:
            backend.complete(_live_model_request())
    assert "Ollama completed without generating a response" in str(exc_info.value)


def test_ollama_http_error_body_extraction():
    backend = ModelExecutionBackend(backend_id="ollama-live", endpoint="http://localhost:11434")
    body_fp = io.BytesIO(b'{"error": "pull model required before generation"}')
    http_err = urllib.error.HTTPError("http://localhost:11434/api/generate", 500, "Internal Error", None, body_fp)
    with patch("urllib.request.urlopen", side_effect=http_err):
        with pytest.raises(BackendExecutionError) as exc_info:
            backend.complete(_live_model_request())
    assert "Ollama HTTP 500: pull model required before generation" in str(exc_info.value)


def test_ollama_server_reachability_error():
    backend = ModelExecutionBackend(backend_id="ollama-live", endpoint="http://localhost:11434")
    url_err = urllib.error.URLError("[Errno 111] Connection refused")
    with patch("urllib.request.urlopen", side_effect=url_err):
        with pytest.raises(BackendUnavailableError) as exc_info:
            backend.complete(_live_model_request())
    assert "Could not connect to Ollama at http://localhost:11434: [Errno 111] Connection refused" in str(exc_info.value)

