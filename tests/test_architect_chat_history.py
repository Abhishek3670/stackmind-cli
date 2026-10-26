"""Tests for Architect conversational chat, multi-turn history, and :goal recommendation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from validators.harness.runner import (
    AgentRunner,
    HarnessTask,
    LLMRequest,
)
from validators.kernel.daemon.manager import SessionManager
from validators.kernel.daemon.storage import DaemonStorage
from validators.kernel.providers.models import Message


class RecordingLLMProvider:
    """Mock LLM Provider that records the exact messages received."""

    def __init__(self, response_text: str = "Architect response") -> None:
        self.response_text = response_text
        self.received_messages: list[Any] = []
        self.call_count = 0

    def complete(self, request: Any) -> Any:
        self.call_count += 1
        return {
            "status": "completed",
            "summary": self.response_text,
            "report_markdown": self.response_text,
            "blockers": [],
            "modified_files": [],
        }


class RecordingProviderAdapter:
    """Mock ProviderAdapter implementing complete() with message recording."""

    def __init__(self, response_text: str = "Architect response") -> None:
        self.response_text = response_text
        self.received_messages: list[Message] = []
        self.provider_name = "mock"
        self.model_name = "mock-model"

    def complete(self, messages: list[Message], cancellation_token: Any = None) -> Any:
        self.received_messages = list(messages)
        from dataclasses import dataclass

        @dataclass
        class _Msg:
            content: str

        @dataclass
        class _Resp:
            message: _Msg
            usage: Any = None

        return _Resp(message=_Msg(content=self.response_text))


def _init_dummy_project(path: Path) -> Path:
    sync_dir = path / ".sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    (sync_dir / "runtime").mkdir(parents=True, exist_ok=True)
    (sync_dir / "inbox" / "claude").mkdir(parents=True, exist_ok=True)
    (sync_dir / "work-orders" / "ACTIVE").mkdir(parents=True, exist_ok=True)

    tree = {
        "agents": {
            "claude": {"session_count": 1, "status": "idle", "assigned_work_orders": []},
            "codex": {"session_count": 0, "status": "idle", "assigned_work_orders": []},
        },
        "work_orders": {"total_active": 0},
    }
    (sync_dir / "runtime" / "TREE.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")
    return path


def test_llm_request_dataclass_holds_session_history():
    req = LLMRequest(
        agent="claude",
        session_count=1,
        task=HarnessTask("adhoc", "t1", Path("."), "Title", "Body", "Query"),
        context=None,
        retrieval=None,
        session_history=[{"role": "user", "content": "I want a landing page"}],
    )
    assert req.session_history is not None
    assert len(req.session_history) == 1
    assert req.session_history[0]["content"] == "I want a landing page"


def test_agent_runner_injects_session_history_into_messages(tmp_path: Path):
    project = _init_dummy_project(tmp_path)
    adapter = RecordingProviderAdapter("Got it, let's design your website.")
    runner = AgentRunner(project, "claude")
    runner.provider_adapter = adapter

    history = [
        {"role": "user", "content": "I want to build a landing for my business"},
        {"role": "assistant", "content": "What kind of business?"},
        {"role": "user", "content": "It's a genset renting business."},
        {"role": "assistant", "content": "A Genset rental landing page with fleet catalog."},
    ]

    result = runner.run_once(
        prompt="for now focus on local deployment",
        session_history=history,
    )

    assert result.status == "completed"
    assert len(adapter.received_messages) >= 6

    # Verify message sequence: system -> turn 1 -> turn 2 -> turn 3
    roles = [m.role for m in adapter.received_messages]
    assert roles[0] == "system"
    assert roles[1] == "user"
    assert adapter.received_messages[1].content == "I want to build a landing for my business"
    assert roles[2] == "assistant"
    assert roles[3] == "user"
    assert adapter.received_messages[3].content == "It's a genset renting business."
    assert roles[4] == "assistant"
    assert roles[5] == "user"
    assert adapter.received_messages[5].content == "for now focus on local deployment"


def test_architect_system_prompt_guidelines(tmp_path: Path):
    project = _init_dummy_project(tmp_path)
    runner = AgentRunner(project, "claude")
    sys_msg = runner._build_adhoc_system_message()

    # Verify key architectural and conversational guidelines
    assert "You are Claude, the Lead Architect" in sys_msg
    assert "powered by StackMind CLI" in sys_msg
    assert "understand their vision" in sys_msg.lower() or "explore their needs" in sys_msg.lower()
    assert ":goal" in sys_msg
    assert "delegate_task" in sys_msg or "trigger_goal" in sys_msg
    assert "Never assume or claim procedural skills (such as 'ponytail'" in sys_msg


def test_session_manager_extracts_and_supplies_chat_history(tmp_path: Path):
    storage_path = tmp_path / "daemon-storage.json"
    storage = DaemonStorage(storage_path)

    recorded_histories: list[Any] = []

    class MockRunner:
        def __init__(self, workspace: str, agent: str):
            self.workspace = workspace
            self.agent = agent

        def run_once(
            self,
            *,
            cancel_event=None,
            operation_id=None,
            prompt=None,
            work_order_id=None,
            is_authoring=False,
            session_history=None,
        ):
            recorded_histories.append(list(session_history) if session_history else None)
            from dataclasses import dataclass

            @dataclass
            class _RunRes:
                status: str = "completed"
                persisted: bool = False
                task_id: str = "task-1"
                report_path: Any = None
                reason: Any = None
                meta: dict = None

            reply = f"Response to: {prompt}"
            return _RunRes(meta={"summary": reply, "report_markdown": reply})

    manager = SessionManager(storage, runner_factory=lambda ws, agent: MockRunner(ws, agent))
    session = manager.create_session("claude", "test", {}, str(tmp_path))
    sid = session["session_id"]

    # Turn 1
    t1 = manager.start_turn(sid, "hey")
    thread1 = manager._turn_threads.get(t1["operation_id"])
    if thread1:
        thread1.join(timeout=2)

    assert recorded_histories[0] is None or len(recorded_histories[0]) == 0

    # Turn 2
    t2 = manager.start_turn(sid, "I want to build a landing for my business")
    thread2 = manager._turn_threads.get(t2["operation_id"])
    if thread2:
        thread2.join(timeout=2)

    assert recorded_histories[1] is not None
    assert len(recorded_histories[1]) == 2
    assert recorded_histories[1][0] == {"role": "user", "content": "hey"}
    assert recorded_histories[1][1] == {"role": "assistant", "content": "Response to: hey"}

    # Turn 3
    t3 = manager.start_turn(sid, "it's a genset renting business")
    thread3 = manager._turn_threads.get(t3["operation_id"])
    if thread3:
        thread3.join(timeout=2)

    assert recorded_histories[2] is not None
    assert len(recorded_histories[2]) == 4
    assert recorded_histories[2][2]["content"] == "I want to build a landing for my business"
    assert recorded_histories[2][3]["content"] == (
        "Response to: I want to build a landing for my business"
    )


def test_adhoc_mode_does_not_leak_context_into_user_message(tmp_path: Path, monkeypatch):
    """Verify skills and context are not prepended/appended into Message.user in adhoc mode."""
    project = _init_dummy_project(tmp_path)
    adapter = RecordingProviderAdapter("Hello! How can I help you today?")
    runner = AgentRunner(project, "claude")
    runner.provider_adapter = adapter

    from types import SimpleNamespace
    from validators.knowledge.api import KnowledgeAPI

    mock_bundle = SimpleNamespace(
        text="#### Skill Summary: git-guardrails-claude-code\n#### Skill Summary: wizard",
        symbols={},
        files=[],
    )
    monkeypatch.setattr(KnowledgeAPI, "assemble_context", lambda self, *args, **kwargs: mock_bundle)

    # Run an adhoc turn
    result = runner.run_once(prompt="hey")

    assert result.status == "completed"
    assert len(adapter.received_messages) >= 2

    # User message must be strictly the user prompt
    user_msg = adapter.received_messages[-1]
    assert user_msg.role == "user"
    assert user_msg.content == "hey"
    assert "git-guardrails-claude-code" not in user_msg.content
    assert "Context:" not in user_msg.content

    # System message contains the background context
    sys_msg = adapter.received_messages[0]
    assert sys_msg.role == "system"
    assert "git-guardrails-claude-code" in sys_msg.content
    assert "Background Reference Only" in sys_msg.content


def test_adhoc_mode_filters_out_colon_commands_from_history(tmp_path: Path):
    """Verify internal TUI colon commands are never fed into the model as user messages."""
    project = _init_dummy_project(tmp_path)
    adapter = RecordingProviderAdapter("Understood.")
    runner = AgentRunner(project, "claude")
    runner.provider_adapter = adapter

    history = [
        {"role": "user", "content": ":provider"},
        {"role": "assistant", "content": "Available providers: ollama, anthropic"},
        {"role": "user", "content": ":roles"},
        {"role": "assistant", "content": "Roles configured"},
        {"role": "user", "content": "hello world"},
    ]

    result = runner.run_once(
        prompt="let's start",
        session_history=history,
    )

    assert result.status == "completed"
    received_user_texts = [m.content for m in adapter.received_messages if m.role == "user"]

    assert ":provider" not in received_user_texts
    assert ":roles" not in received_user_texts
    assert "hello world" in received_user_texts
    assert "let's start" in received_user_texts
