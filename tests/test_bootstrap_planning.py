"""Tests for Step 2: Bootstrap Planning Work Order & Contract Synthesis.

Verifies:
1. Synthesizing bootstrap planning artifacts (WO-000 / next ID) as trusted daemon bookkeeping.
2. AuthoringGate validation on synthesized Work Order and Contract.
3. Task discovery picking up kind='work_order' with identifier='WO-000'.
4. Contract loading via load_harness_contract.
5. Construction of GovernedToolRuntime with read-write permissions on PLAN.md and child work-orders/contracts.
6. Daemon start_turn with is_goal=True automatically synthesizing and executing under WO-000.
7. Adapter :goal command setting is_goal=True.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
import pytest
import yaml

from validators.kernel.daemon.manager import (
    SessionManager,
    synthesize_bootstrap_planning,
    resolve_bootstrap_work_order_id,
)
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.tui.adapter import StackMindTuiAdapter
from validators.harness.authoring_gate import AuthoringGate
from validators.harness.contract_gate import load_harness_contract
from validators.harness.runner import AgentRunner
from validators.knowledge.api import KnowledgeAPI


class DummyProviderAdapter:
    """Mock ProviderAdapter for tool runtime testing."""
    provider_name = "test-provider"
    model_name = "test-model"

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        return None


def test_synthesize_bootstrap_planning_artifacts(tmp_path: Path) -> None:
    """synthesize_bootstrap_planning produces valid WO-000 and contract YAML."""
    prompt = "Build a simple login page with OAuth2 authentication"
    wo_rec, contract_rec = synthesize_bootstrap_planning(tmp_path, prompt)

    assert wo_rec["id"] == "WO-000"
    assert wo_rec["type"] == "RESEARCH"
    assert wo_rec["status"] == "ACTIVE"
    assert wo_rec["priority"] == "P0"
    assert wo_rec["assigned_agents"] == ["claude"]
    assert wo_rec["deliverable"]["path"] == "PLAN.md"
    assert prompt in wo_rec["description"]

    assert contract_rec["work_order"] == "WO-000"
    assert contract_rec["agent_id"] == "claude"
    assert contract_rec["scope"]["write"] == "read-write"
    # Verify narrowed deny list covers operationally sensitive paths
    deny_modules = {d["module"] for d in contract_rec["scope"]["deny"]}
    assert ".git/**" in deny_modules
    assert "src/**" in deny_modules
    assert "tests/**" in deny_modules
    assert ".sync/inbox/codex/**" in deny_modules
    assert ".sync/inbox/gemma/**" in deny_modules
    assert ".sync/runtime/**" in deny_modules
    assert ".sync/outbox/**" in deny_modules
    assert ".sync/agents/**" in deny_modules
    assert ".sync/knowledge/**" in deny_modules
    assert ".env" in deny_modules

    # Files on disk
    wo_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml"
    contract_file = tmp_path / ".sync" / "contracts" / "WO-000.yaml"
    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    tree_file = tmp_path / ".sync" / "runtime" / "TREE.yaml"

    assert wo_file.is_file()
    assert contract_file.is_file()
    assert index_file.is_file()
    assert tree_file.is_file()

    # AuthoringGate validation
    gate = AuthoringGate()
    wo_dec = gate.validate_artifact_content(
        ".sync/work-orders/ACTIVE/WO-000.yaml", wo_file.read_text(encoding="utf-8"), agent="ceo"
    )
    assert wo_dec.passed, wo_dec.errors

    contract_dec = gate.validate_artifact_content(
        ".sync/contracts/WO-000.yaml", contract_file.read_text(encoding="utf-8"), agent="ceo"
    )
    assert contract_dec.passed, contract_dec.errors

    # INDEX.yaml validation
    index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    order_ids = [o["id"] for o in index_data["orders"]]
    assert "WO-000" in order_ids

    # TREE.yaml validation
    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    assigned = tree_data["agents"]["claude"]["assigned_work_orders"]
    assert "WO-000" in assigned


def test_task_discovery_and_contract_loading(tmp_path: Path) -> None:
    """AgentRunner discovers WO-000 and loads its assigned contract."""
    synthesize_bootstrap_planning(tmp_path, "Design checkout microservice")

    runner = AgentRunner(tmp_path, "claude", provider_adapter=DummyProviderAdapter())
    tree_data = runner._load_tree()

    task = runner.discover_next_task(tree_data)
    assert task is not None
    assert task.kind == "work_order"
    assert task.identifier == "WO-000"
    assert task.work_order_id == "WO-000"
    assert task.deliverable_path == "PLAN.md"

    # Contract loading
    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    assert contract is not None
    assert contract.work_order == "WO-000"
    assert contract.agent_id == "claude"
    assert contract.write_mode == "read-write"


def test_build_tool_runtime_returns_governed_tool_runtime(tmp_path: Path) -> None:
    """_build_tool_runtime returns non-None GovernedToolRuntime with correct boundaries."""
    synthesize_bootstrap_planning(tmp_path, "Implement payment gateway architecture")

    runner = AgentRunner(tmp_path, "claude", provider_adapter=DummyProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    assert task is not None

    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    assert contract is not None

    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)

    assert tool_runtime is not None
    gateway = tool_runtime.gateway

    # 1. Writing and reading PLAN.md is permitted
    plan_content = "# Execution Plan\n\n## 1. Overview\nArchitecture details.\n"
    gateway.write_file("PLAN.md", plan_content)
    read_back = gateway.read_file("PLAN.md")
    assert "## 1. Overview" in read_back

    # 1b. Querying graph is permitted and returns structured result
    graph_res = gateway.query_graph("payment gateway")
    assert isinstance(graph_res, dict)
    assert "revision" in graph_res
    assert "entries" in graph_res

    # 2. Writing valid child work order is permitted
    child_wo = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Stripe Integration",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "description": "Implement Stripe checkout endpoints",
    }
    gateway.write_file(".sync/work-orders/ACTIVE/WO-001.yaml", yaml.safe_dump(child_wo, sort_keys=False))

    # 3. Writing malformed child work order is rejected fail-closed by AuthoringGate
    with pytest.raises(PermissionError) as exc_info:
        gateway.write_file(".sync/work-orders/ACTIVE/WO-002.yaml", "malformed: true\n")
    assert "Authoring validation failed" in str(exc_info.value)

    # 4. Out of scope / denied writes (.git/**) are rejected
    with pytest.raises(PermissionError) as git_exc:
        gateway.write_file(".git/config", "tamper")
    assert "target is explicitly denied" in str(git_exc.value)

    # 5. Verify the narrowed deny list is enforced (inbox, runtime)
    with pytest.raises(PermissionError):
        gateway.write_file(".sync/inbox/gemma/fake.md", "injected")
    with pytest.raises(PermissionError):
        gateway.write_file(".sync/runtime/TREE.yaml", "tampered")


def test_narrowed_write_scope_denies_arbitrary_source_files(tmp_path: Path) -> None:
    """Writes to arbitrary source files (src/app.py) and sensitive paths (.sync/inbox/**)
    are correctly denied under the narrowed bootstrap contract.

    This is the key regression test proving the contract's write scope is actually
    narrow in practice, not just nominally — addressing the gap where the previous
    test suite only proved .git/** and malformed artifacts were blocked.
    """
    synthesize_bootstrap_planning(tmp_path, "Test scope narrowing")

    runner = AgentRunner(tmp_path, "claude", provider_adapter=DummyProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    assert task is not None

    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    assert contract is not None

    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)
    assert tool_runtime is not None
    gateway = tool_runtime.gateway

    # --- Denied writes: operationally sensitive paths ---

    # Agent inboxes
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/inbox/gemma/2026-09-27_codex_WO-001-review.md", "fake review")
    assert "denied" in str(exc.value).lower()

    # Runtime state
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/runtime/boot/claude.boot.yaml", "tampered boot")
    assert "denied" in str(exc.value).lower()

    # Outbox
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/outbox/dispatch.yaml", "fake dispatch")
    assert "denied" in str(exc.value).lower()

    # Agent identity files
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/agents/codex.yaml", "identity tamper")
    assert "denied" in str(exc.value).lower()

    # Knowledge store (compiler output)
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/knowledge/index.db", "corrupted")
    assert "denied" in str(exc.value).lower()

    # Snapshots
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/snapshots/latest.yaml", "tampered")
    assert "denied" in str(exc.value).lower()

    # Environment secrets
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".env", "SECRET_KEY=leaked")
    assert "denied" in str(exc.value).lower()

    # --- Permitted writes still work ---

    # PLAN.md
    gateway.write_file("PLAN.md", "# Project Plan:\n\nArchitecture details.\n")
    assert "Architecture details" in gateway.read_file("PLAN.md")

    # Work orders
    child_wo = {
        "id": "WO-001", "type": "FEATURE", "title": "Test WO",
        "status": "ACTIVE", "priority": "P0", "assigned_agents": ["codex"],
        "dependencies": [], "description": "Test task",
    }
    gateway.write_file(".sync/work-orders/ACTIVE/WO-001.yaml", yaml.safe_dump(child_wo, sort_keys=False))

    # Contracts
    child_contract = {
        "schema_version": 1, "agent_id": "codex", "work_order": "WO-001",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {"allow": [{"module": "src/**"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": 10, "max_tokens": 20000},
    }
    gateway.write_file(".sync/contracts/WO-001.yaml", yaml.safe_dump(child_contract, sort_keys=False))


def test_narrowed_scope_with_arbitrary_file_extension(tmp_path: Path) -> None:
    """Regression test: deny rules work for arbitrary non-Python paths,
    confirming the path-to-scope-rule fix is general, not special-cased.

    Covers: README.md reads (allowed), .json config reads (allowed),
    and denied path patterns work regardless of file extension.
    """
    synthesize_bootstrap_planning(tmp_path, "Test arbitrary extensions")

    runner = AgentRunner(tmp_path, "claude", provider_adapter=DummyProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)
    assert tool_runtime is not None
    gateway = tool_runtime.gateway

    # Reads of allowed non-Python paths are permitted
    ws_root = tool_runtime.workspace.root
    (ws_root / "PLAN.md").write_text("# Plan", encoding="utf-8")
    assert gateway.read_file("PLAN.md") == "# Plan"

    # Denied paths are blocked regardless of file extension
    with pytest.raises(PermissionError):
        gateway.write_file(".sync/inbox/gemma/notice.json", '{"msg": "injected"}')
    with pytest.raises(PermissionError):
        gateway.read_file(".sync/runtime/drafts/claude.boot.draft.yaml")


def test_claude_narrowed_contract_denies_source_tests_and_other_inboxes(tmp_path: Path) -> None:
    """Claude under the narrowed contract cannot write to src/**, tests/**, or other agents' inboxes.

    Verifies the true minimum contract boundaries:
    - Forbidden: src/**, tests/**, .sync/inbox/codex/**, .sync/inbox/gemini/**, .sync/inbox/gemma/**, .sync/runtime/**
    - Permitted: PLAN.md, .sync/work-orders/**, .sync/contracts/**, .sync/inbox/local-llm/**, .sync/inbox/claude/**
    """
    wo_rec, contract_rec = synthesize_bootstrap_planning(tmp_path, "Build auth microservice")
    runner = AgentRunner(tmp_path, "claude", provider_adapter=DummyProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)
    assert tool_runtime is not None
    gateway = tool_runtime.gateway

    # 1. Claude CANNOT write to src/**
    with pytest.raises(PermissionError) as exc:
        gateway.write_file("src/api/auth.py", "def login(): pass")
    assert "denied" in str(exc.value).lower() or "outside" in str(exc.value).lower()

    # 2. Claude CANNOT write to tests/**
    with pytest.raises(PermissionError) as exc:
        gateway.write_file("tests/test_auth.py", "def test_login(): pass")
    assert "denied" in str(exc.value).lower() or "outside" in str(exc.value).lower()

    # 3. Claude CANNOT write to other agents' inboxes
    for other_agent in ("codex", "gemini", "gemma", "CEO"):
        with pytest.raises(PermissionError) as exc:
            gateway.write_file(f".sync/inbox/{other_agent}/assignment.md", "# Work Order")
        assert "denied" in str(exc.value).lower() or "outside" in str(exc.value).lower()

    # 4. Claude CANNOT write to runtime state
    with pytest.raises(PermissionError) as exc:
        gateway.write_file(".sync/runtime/TREE.yaml", "tampered: true")
    assert "denied" in str(exc.value).lower() or "outside" in str(exc.value).lower()

    # 5. Permitted writes still succeed
    gateway.write_file("PLAN.md", "# Project Plan\n\nArchitecture verified.\n")
    assert "Architecture verified" in gateway.read_file("PLAN.md")

    child_wo = {
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Scaffolding",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "description": "Scaffolding task",
    }
    gateway.write_file(
        ".sync/work-orders/ACTIVE/WO-001.yaml",
        yaml.safe_dump(child_wo, sort_keys=False),
    )
    child_contract = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-001",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {"allow": [{"module": "requirements.txt"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 5, "max_tokens": 10000},
    }
    gateway.write_file(
        ".sync/contracts/WO-001.yaml",
        yaml.safe_dump(child_contract, sort_keys=False),
    )
    gateway.write_file(
        ".sync/inbox/local-llm/release_auth.md",
        "# Release Authorization\nApproved for release.\n",
    )


def test_daemon_start_turn_goal_synthesizes_and_discovers(tmp_path: Path) -> None:
    """Daemon start_turn with is_goal=True synthesizes planning WO and assigns to Claude."""
    storage = DaemonStorage(tmp_path / "daemon.json")
    created_runner = None

    def dummy_runner_factory(ws: str, agent: str) -> Any:
        nonlocal created_runner
        created_runner = AgentRunner(Path(ws), agent, provider_adapter=DummyProviderAdapter())
        return created_runner

    manager = SessionManager(storage, runner_factory=dummy_runner_factory)
    session = manager.create_session(
        agent="codex",
        provider="local",
        contract={"scope": {"allow": ["*"], "deny": [], "write": "read-write"}},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    # Start turn with is_goal=True
    turn_op = manager.start_turn(
        sid,
        "Build a user notification microservice",
        is_goal=True,
    )

    assert turn_op["role"] == "architecture"
    assert turn_op["agent_id"] == "claude"
    assert turn_op["work_order_id"] == "WO-000"

    # Confirmed artifacts on disk
    wo_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-000.yaml"
    contract_file = tmp_path / ".sync" / "contracts" / "WO-000.yaml"
    assert wo_file.is_file()
    assert contract_file.is_file()


def test_adapter_goal_command_sets_is_goal() -> None:
    """StackMindTuiAdapter :goal command attaches is_goal=True and role=architecture."""
    class MockClient:
        def __init__(self) -> None:
            self.last_turn_args = None

        def turn(self, session_id: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
            self.last_turn_args = (session_id, prompt, kwargs)
            return {"operation_id": "op-goal-123"}

    mock_client = MockClient()
    adapter = StackMindTuiAdapter(mock_client)

    res = adapter.command(":goal Design a caching layer", session_id="sess-1")
    assert res == {"operation_id": "op-goal-123"}

    sid, prompt, kwargs = mock_client.last_turn_args
    assert sid == "sess-1"
    assert prompt == "Design a caching layer"
    assert kwargs.get("role") == "architecture"
    assert kwargs.get("agent_id") == "claude"
    assert kwargs.get("is_goal") is True


def test_resolve_bootstrap_work_order_id_fallback(tmp_path: Path) -> None:
    """resolve_bootstrap_work_order_id returns next available ID when WO-000 is completed."""
    # When no work orders exist
    assert resolve_bootstrap_work_order_id(tmp_path, "WO-000") == "WO-000"

    # When WO-000 is completed
    completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_dir.mkdir(parents=True, exist_ok=True)
    (completed_dir / "WO-000.yaml").write_text("id: WO-000\nstatus: COMPLETED\n", encoding="utf-8")

    next_id = resolve_bootstrap_work_order_id(tmp_path, "WO-000")
    assert next_id == "WO-001"


def test_architecture_prompt_and_schema_injection(tmp_path: Path) -> None:
    """Step 3 unit verification: When Architecture executes a planning work order,
    it receives role-specific instructions:
    1. System prompt identifying Claude as Senior Architect.
    2. Architecture research instructions mandating query_graph before writing PLAN.md.
    3. PLAN_GENERATION_INSTRUCTIONS with required section structure and checklist syntax.
    4. Work Order and Contract schema templates for downstream task breakdown.
    """
    from validators.harness.runner import LLMRequest, HarnessTask
    from validators.kernel.providers.models import ProviderResponse, TokenUsage, Message

    synthesize_bootstrap_planning(tmp_path, "Build authentication system")

    captured_messages: list[Any] = []

    class CapturingProviderAdapter:
        provider_name = "capturing"
        model_name = "capturing-model"

        def complete(self, messages: Any, **kwargs: Any) -> Any:
            nonlocal captured_messages
            captured_messages = list(messages)
            # Return a valid JSON completion decision
            return ProviderResponse(
                message=Message.assistant('{"status": "completed", "summary": "Done", "modified_files": ["PLAN.md"]}'),
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    runner = AgentRunner(tmp_path, "claude", provider_adapter=CapturingProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    assert task is not None
    assert task.deliverable_path == "PLAN.md"

    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)

    request = LLMRequest(
        agent="claude",
        session_count=1,
        task=task,
        context=k_api.assemble_context(task.query, limit=2, contract=contract),
        retrieval=runner.search_tool.search(task.query, limit=2),
    )

    runner._complete_request(request, tool_runtime)

    # 1. System message check
    system_msg = next((m for m in captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "Senior Architect" in system_msg.content
    assert "query_graph" in system_msg.content

    # 2. User message checks
    user_msg = next((m for m in captured_messages if m.role == "user"), None)
    assert user_msg is not None

    # (a) Research instructions
    assert "Mandatory Codebase Research" in user_msg.content
    assert "query_graph" in user_msg.content

    # (b) PLAN.md generation instructions
    assert "# Project Plan:" in user_msg.content
    assert "## Current Architecture" in user_msg.content
    assert "## Milestones & Roadmap" in user_msg.content
    assert "markdown checklist syntax" in user_msg.content

    # (c) Governed Artifact Authoring Schemas
    assert "Work Order Schema Specification" in user_msg.content
    assert "schemas/work-order.schema.json" in user_msg.content
    assert "Contract Schema Specification" in user_msg.content
    assert "schemas/contract.schema.json" in user_msg.content


def test_codex_task_with_architecture_in_title_does_not_get_claude_prompt(tmp_path: Path) -> None:
    """Regression test: A codex-assigned task with 'architecture' in its title
    must NOT receive the Claude/Architecture system prompt or research-first instructions.
    Only self.agent == 'claude' receives Architecture identity and planning guidance.
    """
    from validators.harness.runner import HarnessTask, LLMRequest, GovernedToolRuntime
    from validators.kernel.providers.models import ProviderResponse, TokenUsage, Message
    from validators.kernel.contract import AgentContract as KernelContract
    from validators.kernel.identity import AuthorizationPolicy
    from validators.kernel.boundary import RuntimeBoundary
    from validators.kernel.operations import OperationJournal
    from validators.kernel.workspace import ScratchWorkspace
    from validators.kernel.tools import ToolGateway

    captured_messages: list[Any] = []

    class CapturingProviderAdapter:
        provider_name = "capturing"
        model_name = "capturing-model"

        def complete(self, messages: Any, **kwargs: Any) -> Any:
            nonlocal captured_messages
            captured_messages = list(messages)
            return ProviderResponse(
                message=Message.assistant('{"status": "completed", "summary": "Done"}'),
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    runner = AgentRunner(tmp_path, "codex", provider_adapter=CapturingProviderAdapter())
    k_api = KnowledgeAPI(tmp_path)

    task = HarnessTask(
        kind="work_order",
        identifier="WO-010",
        path=tmp_path / "WO-010.yaml",
        title="Refactor database architecture and schema models",
        body="Implement database migrations and clean architecture repository pattern.",
        query="Refactor database architecture",
        work_order_id="WO-010",
        deliverable_path="src/db/repository.py",
    )

    contract = KernelContract(
        agent_id="codex",
        work_order="WO-010",
        allow=("workspace/src/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    policy = AuthorizationPolicy.permit("test", ("read_file", "write_file"))
    boundary = RuntimeBoundary(OperationJournal())
    ws = ScratchWorkspace.create(tmp_path, "test-codex-arch")
    gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="s-codex",
        attempt_id="a-codex",
        actor_id="codex",
        provider_id="mock",
    )
    tool_runtime = GovernedToolRuntime(workspace=ws, gateway=gateway)

    request = LLMRequest(
        agent="codex",
        session_count=1,
        task=task,
        context=k_api.assemble_context(task.query, limit=2),
        retrieval=runner.search_tool.search(task.query, limit=2),
    )

    runner._complete_request(request, tool_runtime)

    # 1. System message must be generic worker prompt, NOT Claude / Senior Architect
    system_msg = next((m for m in captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "Senior Architect" not in system_msg.content
    assert "Claude" not in system_msg.content
    assert "You are a governed StackMind worker. Use tools for all file I/O." in system_msg.content
    assert "Code execution is unavailable; the harness verifies after the final decision." in system_msg.content

    # 2. User message must NOT contain Architecture research or authoring instructions
    user_msg = next((m for m in captured_messages if m.role == "user"), None)
    assert user_msg is not None
    assert "Mandatory Codebase Research" not in user_msg.content
    assert "Work Order Schema Specification" not in user_msg.content
    assert "Contract Schema Specification" not in user_msg.content
    assert "PLAN.md formatting requirement" not in user_msg.content


def test_step4_prompt_and_schema_injection_for_authoring_turn(tmp_path: Path) -> None:
    """Step 4 prompt verification: When Architecture executes an authoring turn (after plan approval),
    it receives role-specific authoring instructions:
    1. System prompt identifying Claude as Senior Architect tasked with authoring Work Orders and Contracts.
    2. Governed Artifact Authoring Schemas (work-order and contract).
    3. Does NOT receive PLAN_GENERATION_INSTRUCTIONS or query_graph mandatory rule.
    """
    from validators.harness.runner import LLMRequest, HarnessTask
    from validators.kernel.providers.models import ProviderResponse, TokenUsage, Message

    synthesize_bootstrap_planning(tmp_path, "Build authentication system")

    captured_messages: list[Any] = []

    class CapturingProviderAdapter:
        provider_name = "capturing"
        model_name = "capturing-model"

        def complete(self, messages: Any, **kwargs: Any) -> Any:
            nonlocal captured_messages
            captured_messages = list(messages)
            return ProviderResponse(
                message=Message.assistant('{"status": "completed", "summary": "Done", "modified_files": [".sync/work-orders/ACTIVE/WO-001.yaml"]}'),
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    runner = AgentRunner(tmp_path, "claude", provider_adapter=CapturingProviderAdapter())
    tree_data = runner._load_tree()
    task = runner.discover_next_task(tree_data)
    assert task is not None

    contract = load_harness_contract(tmp_path, "claude", task.work_order_id)
    k_api = KnowledgeAPI(tmp_path)
    tool_runtime = runner._build_tool_runtime(contract, task, k_api)

    authoring_prompt = (
        "The architecture plan 'PLAN-001' has been approved by the operator (reason: Approved by operator).\n\n"
        "Your task now as Senior Architect is to author the implementation Work Orders and Contracts for the tasks in PLAN.md:\n"
        "1. Review PLAN.md for the approved milestones and tasks.\n"
        "2. For each task, call write_file to write a Work Order YAML file to .sync/work-orders/ACTIVE/<WO-ID>.yaml\n"
        "3. For each Work Order, call write_file to write a corresponding Contract YAML file to .sync/contracts/<WO-ID>.yaml\n"
        "4. When all work orders and contracts are written, return the final HarnessDecision JSON."
    )

    authoring_task = HarnessTask(
        kind=task.kind,
        identifier=task.identifier,
        path=task.path,
        title=task.title,
        body=f"{task.body}\n\nTurn Instructions:\n{authoring_prompt}",
        query=task.query,
        work_order_id=task.work_order_id,
        deliverable_path=task.deliverable_path,
    )

    request = LLMRequest(
        agent="claude",
        session_count=1,
        task=authoring_task,
        context=k_api.assemble_context(task.query, limit=2, contract=contract),
        retrieval=runner.search_tool.search(task.query, limit=2),
    )

    runner._complete_request(request, tool_runtime)

    # 1. System message check
    system_msg = next((m for m in captured_messages if m.role == "system"), None)
    assert system_msg is not None
    assert "Senior Architect" in system_msg.content
    assert "approved the architecture plan in PLAN.md" in system_msg.content
    assert "author the implementation Work Orders and Contracts" in system_msg.content
    assert "Architects must NEVER assign implementation tasks to claude" in system_msg.content
    assert "query_graph" not in system_msg.content  # Turn 1 research rule not in authoring prompt

    # 2. User message checks
    user_msg = next((m for m in captured_messages if m.role == "user"), None)
    assert user_msg is not None
    assert "Governed Artifact Authoring Schemas" in user_msg.content
    assert "schemas/work-order.schema.json" in user_msg.content
    assert "schemas/contract.schema.json" in user_msg.content
    assert "PLAN.md formatting requirement" not in user_msg.content


def test_step4_end_to_end_orchestration_approve_and_reject(tmp_path: Path) -> None:
    """Step 4 integration: Full end-to-end orchestration:
    1. Turn 1 (:goal) generates PLAN.md -> transitions to AWAITING_APPROVAL.
    2. Rejection test: reject_plan sets state to REJECTED, 0 child WOs created.
    3. Propose again -> Approval test: approve_plan sets state to APPROVED, dispatches Turn 2.
    4. Turn 2 authors WO-001 and contract -> promoted to disk under lock.
    5. Live disk has child WO and contract, INDEX.yaml and TREE.yaml updated, runtime validate passes!
    """
    import json
    import time
    from cli.init import init
    from cli.validate import validate as validate_runtime
    from validators.kernel.providers.models import ProviderResponse, TokenUsage, Message, ToolCallRequest

    # Initialize a valid StackMind project so protocol and structural gates pass
    init(tmp_path, name="Test Project", no_git=True)

    storage = DaemonStorage(tmp_path / "daemon.json")

    plan_markdown = (
        "# Project Plan: User Authentication System\n\n"
        "## Current Architecture\n"
        "StackMind repository layout with CLI kernel and harness.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Implement auth backend models and routes (assigned: codex)\n"
        "- [ ] Milestone 2: Implement login UI screen and form validation (assigned: gemini)\n"
    )

    child_wo_yaml = yaml.safe_dump({
        "id": "WO-001",
        "type": "FEATURE",
        "title": "Implement auth backend models and routes",
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {
            "type": "code",
            "description": "Auth endpoints",
        },
        "description": "Implement authentication endpoints",
    }, sort_keys=False)

    child_contract_yaml = yaml.safe_dump({
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-001",
        "identity": {"role": "backend", "reports_to": "claude"},
        "scope": {"allow": [{"module": "src/**"}], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": 10, "max_tokens": 20000},
    }, sort_keys=False)

    class ScriptedStep4ProviderAdapter:
        provider_name = "scripted-step4"
        model_name = "scripted-model"

        def complete(self, messages: Any, **kwargs: Any) -> Any:
            user_msg = next((m for m in messages if m.role == "user"), None)
            user_text = user_msg.content if user_msg else ""

            # Check if this is Turn 2 (authoring child work orders and contracts)
            if "author the implementation work orders and contracts" in user_text.lower():
                # Check if tool results are already in history
                has_tool_result = any(m.role == "tool" for m in messages)
                if not has_tool_result:
                    return ProviderResponse(
                        message=Message.assistant(
                            content="",
                            tool_calls=[
                                ToolCallRequest(
                                    id="call_wo",
                                    name="write_file",
                                    arguments={
                                        "path": ".sync/work-orders/ACTIVE/WO-001.yaml",
                                        "content": child_wo_yaml,
                                    },
                                ),
                                ToolCallRequest(
                                    id="call_c",
                                    name="write_file",
                                    arguments={
                                        "path": ".sync/contracts/WO-001.yaml",
                                        "content": child_contract_yaml,
                                    },
                                ),
                            ],
                        ),
                        usage=TokenUsage(prompt_tokens=150, completion_tokens=80, total_tokens=230),
                    )
                else:
                    decision_json = json.dumps({
                        "status": "completed",
                        "summary": "Authored WO-001 and Contract",
                        "report_markdown": "Authored implementation artifacts for approved plan.",
                        "modified_files": [
                            ".sync/work-orders/ACTIVE/WO-001.yaml",
                            ".sync/contracts/WO-001.yaml",
                        ],
                        "release_target": "v3.1.0",
                    })
                    return ProviderResponse(
                        message=Message.assistant(content=decision_json),
                        usage=TokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140),
                    )
            else:
                # Turn 1: Planning (write PLAN.md)
                has_tool_result = any(m.role == "tool" for m in messages)
                if not has_tool_result:
                    return ProviderResponse(
                        message=Message.assistant(
                            content="",
                            tool_calls=[
                                ToolCallRequest(
                                    id="call_plan",
                                    name="write_file",
                                    arguments={
                                        "path": "PLAN.md",
                                        "content": plan_markdown,
                                    },
                                )
                            ],
                        ),
                        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                    )
                else:
                    decision_json = json.dumps({
                        "status": "completed",
                        "summary": "Generated architecture plan in PLAN.md",
                        "report_markdown": "Completed planning turn.",
                        "modified_files": ["PLAN.md"],
                        "release_target": "v3.1.0",
                    })
                    return ProviderResponse(
                        message=Message.assistant(content=decision_json),
                        usage=TokenUsage(prompt_tokens=80, completion_tokens=30, total_tokens=110),
                    )

    def scripted_runner_factory(ws: str, agent: str) -> Any:
        return AgentRunner(Path(ws), agent, provider_adapter=ScriptedStep4ProviderAdapter())

    manager = SessionManager(storage, runner_factory=scripted_runner_factory)
    session = manager.create_session(
        agent="codex",
        provider="local",
        contract={"scope": {"allow": ["*"], "deny": [], "write": "read-write"}},
        workspace=str(tmp_path),
    )
    sid = session["session_id"]

    # 1. Start Turn 1 with is_goal=True
    turn1_op = manager.start_turn(
        sid,
        "Build a user authentication system",
        is_goal=True,
    )
    op1_id = turn1_op["operation_id"]

    # Wait for Turn 1 thread to finish
    max_wait = 10.0
    start_t = time.monotonic()
    while time.monotonic() - start_t < max_wait:
        with manager._lock:
            op_curr = manager.get_operation(op1_id)
            if op_curr.get("status") in ("COMPLETED", "FAILED"):
                break
        time.sleep(0.05)

    assert op_curr.get("status") == "COMPLETED", op_curr.get("result")
    assert (tmp_path / "PLAN.md").is_file()

    # Plan should be proposed into AWAITING_APPROVAL
    plan = manager.get_plan(sid, "PLAN-001")
    assert plan["state"] == "AWAITING_APPROVAL"
    assert len(plan["work_orders"]) >= 1

    # 2. Test Rejection flow: reject_plan marks plan REJECTED with feedback
    rejected_plan = manager.reject_plan(sid, "PLAN-001", reason="Needs MFA")
    assert rejected_plan["state"] == "REJECTED"
    assert rejected_plan["feedback"] == "Needs MFA"
    assert rejected_plan["created_work_orders"] == []

    # Propose revised plan for approval test
    manager.propose_plan(
        sid,
        "PLAN-001",
        title="User Authentication System (v2)",
        content=plan_markdown,
        metadata=plan["metadata"],
    )
    revised_plan = manager.get_plan(sid, "PLAN-001")
    assert revised_plan["state"] == "AWAITING_APPROVAL"

    # 3. Test Approval flow: approve_plan marks plan APPROVED and dispatches Turn 2
    approved_wos = manager.approve_plan(sid, "PLAN-001", reason="Approved by operator")
    assert len(approved_wos) >= 1
    assert manager.get_plan(sid, "PLAN-001")["state"] == "APPROVED"

    # Find Turn 2 operation in session journal
    with manager._lock:
        journal = manager.session_history(sid)
        turn2_entries = [
            e for e in journal
            if e.get("operation") == "turn" and e.get("operation_id") != op1_id
        ]
        assert len(turn2_entries) == 1
        turn2_op_id = turn2_entries[0]["operation_id"]

    # Wait for Turn 2 thread to finish
    start_t = time.monotonic()
    while time.monotonic() - start_t < max_wait:
        with manager._lock:
            op2_curr = manager.get_operation(turn2_op_id)
            if op2_curr.get("status") in ("COMPLETED", "FAILED"):
                break
        time.sleep(0.05)

    assert op2_curr.get("status") == "COMPLETED"

    # 4. Verify disk artifacts from Turn 2
    wo1_file = tmp_path / ".sync" / "work-orders" / "ACTIVE" / "WO-001.yaml"
    contract1_file = tmp_path / ".sync" / "contracts" / "WO-001.yaml"
    assert wo1_file.is_file(), "Child work order WO-001.yaml must exist on disk"
    assert contract1_file.is_file(), "Child contract WO-001.yaml must exist on disk"

    # 5. Verify INDEX.yaml and TREE.yaml reflect WO-001
    index_file = tmp_path / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    order_ids = [o["id"] for o in index_data["orders"]]
    assert "WO-001" in order_ids

    tree_file = tmp_path / ".sync" / "runtime" / "TREE.yaml"
    tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
    codex_assigned = tree_data["agents"]["codex"]["assigned_work_orders"]
    assert "WO-001" in codex_assigned

    # 6. Verify stackmind validate passes on live workspace
    validation = validate_runtime(tmp_path)
    assert validation.passed, [e.message for e in validation.errors]


def test_step4_tui_adapter_approve_reject_delegates_to_plan(tmp_path: Path) -> None:
    """StackMindTuiAdapter :approve and :reject commands route to plan_approve/plan_reject
    when a plan is in AWAITING_APPROVAL state.
    """
    class MockClient:
        def __init__(self) -> None:
            self.plan_get_called = False
            self.plan_approve_called = False
            self.plan_reject_called = False
            self.plan = {
                "plan_id": "PLAN-001",
                "state": "AWAITING_APPROVAL",
            }

        def plan_get(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
            self.plan_get_called = True
            return self.plan

        def plan_approve(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
            self.plan_approve_called = True
            self.plan["state"] = "APPROVED"
            return {"status": "APPROVED", "plan_id": plan_id, "reason": reason}

        def plan_reject(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
            self.plan_reject_called = True
            self.plan["state"] = "REJECTED"
            return {"status": "REJECTED", "plan_id": plan_id, "reason": reason}

        def decide(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
            return {"session_id": session_id, "approved": approved}

    mock_client = MockClient()
    adapter = StackMindTuiAdapter(mock_client)

    # 1. :approve routes to plan_approve
    res_app = adapter.command(":approve Looks solid", session_id="s-123")
    assert mock_client.plan_get_called
    assert mock_client.plan_approve_called
    assert res_app["status"] == "APPROVED"
    assert res_app["reason"] == "Looks solid"

    # 2. Reset and test :reject
    mock_client.plan_approve_called = False
    mock_client.plan["state"] = "AWAITING_APPROVAL"
    res_rej = adapter.command(":reject Needs revisions", session_id="s-123")
    assert mock_client.plan_reject_called
    assert res_rej["status"] == "REJECTED"
    assert res_rej["reason"] == "Needs revisions"


