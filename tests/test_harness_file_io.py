"""Phase A integration coverage for governed model-originated file I/O."""

from __future__ import annotations

import json

import yaml

from cli.init import init
from validators.harness.runner import AgentRunner
from validators.kernel.providers import OpenAICompatibleAdapter


def _project_with_work_order(tmp_path):
    project = tmp_path / "project"
    init(project, name="PhaseA", no_git=True)
    tree_path = project / ".sync" / "runtime" / "TREE.yaml"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    tree["agents"]["codex"]["assigned_work_orders"] = ["WO-026"]
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    (project / ".sync" / "work-orders" / "ACTIVE" / "WO-026.yaml").write_text(
        yaml.safe_dump({
            "id": "WO-026", "type": "FEATURE", "title": "Governed file I/O",
            "status": "ACTIVE", "priority": "P0", "assigned_agents": ["codex"],
            "dependencies": [], "description": "Edit the fixture through tools.",
            "deliverable": {"type": "module", "path": "validators/harness/phase_a_fixture.py", "description": "Phase A fixture module"},
        }, sort_keys=False), encoding="utf-8",
    )
    index_path = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    index_data["orders"].append({
        "id": "WO-026", "type": "FEATURE", "title": "Governed file I/O",
        "status": "ACTIVE", "priority": "P0", "assigned_agents": ["codex"],
        "dependencies": [],
        "deliverable": {"type": "module", "path": "validators/harness/phase_a_fixture.py", "description": "Phase A fixture module"},
    })
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "WO-026.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "agent_id": "codex", "work_order": "WO-026",
            "scope": {"allow": [{"module": "validators.harness", "depth": 2}], "deny": [], "write": "read-write"},
            "budget": {"max_files_touched": 2, "max_tokens": 10000},
        }, sort_keys=False), encoding="utf-8",
    )
    fixture = project / "validators" / "harness" / "phase_a_fixture.py"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("VALUE = 'before'\n", encoding="utf-8")
    return project, fixture


def test_runner_promotes_native_tool_write_only_after_verification(tmp_path):
    project, fixture = _project_with_work_order(tmp_path)
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        messages = payload["messages"]
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "read", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "validators/harness/phase_a_fixture.py"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        if calls == 2:
            assert any(m.get("role") == "tool" and "before" in m.get("content", "") for m in messages)
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "write", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "validators/harness/phase_a_fixture.py", "content": "VALUE = 'after'\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "completed", "summary": "Fixture updated", "report_markdown": "done",
            "blockers": [], "modified_files": ["validators/harness/phase_a_fixture.py"],
            "release_target": "v3.1.0", "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    runner = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    assert result.status == "completed", result.reason
    assert fixture.read_text(encoding="utf-8") == "VALUE = 'after'\n"


def test_native_tool_write_is_denied_outside_contract(tmp_path):
    project, fixture = _project_with_work_order(tmp_path)
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "escape", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "outside.py", "content": "blocked = True\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        assert any("PermissionError" in m.get("content", "") for m in payload["messages"] if m.get("role") == "tool")
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "blocked", "summary": "Denied", "report_markdown": "denied", "blockers": ["scope"],
            "modified_files": [], "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    result = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport)).run_once()
    assert result.status == "blocked"
    assert fixture.read_text(encoding="utf-8") == "VALUE = 'before'\n"
    assert not (project / "outside.py").exists()


def test_native_tool_write_denied_when_contract_read_only(tmp_path):
    project, fixture = _project_with_work_order(tmp_path)
    # Set contract to read-only
    contract_file = project / ".sync" / "contracts" / "WO-026.yaml"
    contract_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
    contract_data["scope"]["write"] = "read-only"
    contract_file.write_text(yaml.safe_dump(contract_data, sort_keys=False), encoding="utf-8")

    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "readonly_write", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "validators/harness/phase_a_fixture.py", "content": "VALUE = 'hacked'\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        assert any("contract is read-only" in m.get("content", "") for m in payload["messages"] if m.get("role") == "tool")
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "blocked", "summary": "Read-only blocked", "report_markdown": "denied",
            "blockers": ["read-only"], "modified_files": [], "retrieval_queries": [],
            "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    result = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport)).run_once()
    assert result.status == "blocked"
    assert fixture.read_text(encoding="utf-8") == "VALUE = 'before'\n"


def test_native_tool_path_traversal_denied(tmp_path):
    project, fixture = _project_with_work_order(tmp_path)
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "traversal", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "../escape.py", "content": "escaped = True\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        assert any(
            ("WorkspaceEscapeError" in m.get("content", "") or "traversal is forbidden" in m.get("content", "") or "PermissionError" in m.get("content", ""))
            for m in payload["messages"] if m.get("role") == "tool"
        )
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "blocked", "summary": "Traversal blocked", "report_markdown": "denied",
            "blockers": ["traversal"], "modified_files": [], "retrieval_queries": [],
            "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    result = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport)).run_once()
    assert result.status == "blocked"
    assert not (tmp_path / "escape.py").exists()


def test_authoritative_project_unmodified_before_verification(tmp_path):
    project, fixture = _project_with_work_order(tmp_path)
    live_content_during_turn = None
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls, live_content_during_turn
        calls += 1
        messages = payload["messages"]
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "write", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "validators/harness/phase_a_fixture.py", "content": "VALUE = 'after'\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        if calls == 2:
            # During the turn, after write_file tool has executed in scratch workspace,
            # verify that authoritative live project has NOT been modified yet!
            live_content_during_turn = fixture.read_text(encoding="utf-8")
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
                "status": "completed", "summary": "Updated", "report_markdown": "done",
                "blockers": [], "modified_files": ["validators/harness/phase_a_fixture.py"],
                "release_target": "v3.1.0", "retrieval_queries": [], "uncertainty": [], "commands": [],
            })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    runner = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    assert result.status == "completed", result.reason
    assert live_content_during_turn == "VALUE = 'before'\n"
    assert fixture.read_text(encoding="utf-8") == "VALUE = 'after'\n"


def test_native_tool_creates_new_nested_file(tmp_path):
    project, _ = _project_with_work_order(tmp_path)
    new_relative = "validators/harness/nested/new_module.py"
    # Update work order and index deliverable to match new file
    wo_file = project / ".sync" / "work-orders" / "ACTIVE" / "WO-026.yaml"
    wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
    wo_data["deliverable"]["path"] = new_relative
    wo_file.write_text(yaml.safe_dump(wo_data, sort_keys=False), encoding="utf-8")

    index_file = project / ".sync" / "work-orders" / "INDEX.yaml"
    index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
    for order in index_data["orders"]:
        if order["id"] == "WO-026":
            order["deliverable"]["path"] = new_relative
    index_file.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "write_nested", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": new_relative, "content": "NESTED = True\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "completed", "summary": "Created nested", "report_markdown": "done",
            "blockers": [], "modified_files": [new_relative],
            "release_target": "v3.1.0", "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    runner = AgentRunner(project, "codex", provider_adapter=OpenAICompatibleAdapter(transport=transport))
    result = runner.run_once()

    assert result.status == "completed", result.reason
    live_new_file = project / "validators" / "harness" / "nested" / "new_module.py"
    assert live_new_file.exists()
    assert live_new_file.read_text(encoding="utf-8") == "NESTED = True\n"


def test_ollama_adapter_tool_call_extraction():
    from validators.kernel.providers.adapter import OllamaAdapter
    from validators.kernel.providers.models import Message

    # 1. Native tool_calls in message
    def transport_native(payload, stream, timeout):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                }],
            },
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

    adapter = OllamaAdapter(transport=transport_native)
    resp = adapter.complete([Message.user("hello")])
    assert len(resp.message.tool_calls) == 1
    assert resp.message.tool_calls[0].name == "read_file"
    assert resp.message.tool_calls[0].arguments == {"path": "README.md"}

    # 2. Content with raw JSON tool calls
    def transport_content_json(payload, stream, timeout):
        return {
            "message": {
                "role": "assistant",
                "content": '{"name": "read_file", "arguments": {"path": "foo.txt"}}\n{"name": "write_file", "arguments": {"path": "bar.txt", "content": "hi"}}',
            },
            "done_reason": "stop",
        }

    adapter2 = OllamaAdapter(transport=transport_content_json)
    resp2 = adapter2.complete([Message.user("hello")])
    assert len(resp2.message.tool_calls) == 2
    assert resp2.message.tool_calls[0].name == "read_file"
    assert resp2.message.tool_calls[1].name == "write_file"
    assert resp2.message.tool_calls[1].arguments == {"path": "bar.txt", "content": "hi"}

    # 3. Content with <tool_call> tags
    def transport_tags(payload, stream, timeout):
        return {
            "message": {
                "role": "assistant",
                "content": '<tool_call>{"name": "write_file", "arguments": {"path": "tagged.txt", "content": "data"}}</tool_call>',
            },
            "done_reason": "stop",
        }

    adapter3 = OllamaAdapter(transport=transport_tags)
    resp3 = adapter3.complete([Message.user("hello")])
    assert len(resp3.message.tool_calls) == 1
    assert resp3.message.tool_calls[0].name == "write_file"
    assert resp3.message.tool_calls[0].arguments == {"path": "tagged.txt", "content": "data"}


def test_daemon_default_runner_wires_ollama_adapter(tmp_path):
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.providers.adapter import OllamaAdapter

    storage = DaemonStorage(tmp_path / "daemon.json")
    manager = SessionManager(storage)
    manager.configure_role_backend("backend", "ollama", "qwen2.5-coder:7b")

    runner = manager._default_runner(str(tmp_path), "codex")
    assert runner.provider_adapter is not None
    assert isinstance(runner.provider_adapter, OllamaAdapter)
    assert runner.provider_adapter.model_name == "qwen2.5-coder:7b"


def test_runner_with_ollama_adapter_promotes_write_after_verification(tmp_path):
    from validators.kernel.providers.adapter import OllamaAdapter

    project, fixture = _project_with_work_order(tmp_path)
    calls = 0

    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": '{"name": "write_file", "arguments": {"path": "validators/harness/phase_a_fixture.py", "content": "VALUE = \'ollama_after\'\\n"}}',
                },
                "done_reason": "stop",
            }
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "name": "HarnessDecision",
                    "arguments": {
                        "status": "completed",
                        "summary": "Ollama fixture updated",
                        "report_markdown": "done",
                        "modified_files": ["validators/harness/phase_a_fixture.py"],
                        "blockers": [],
                    },
                }),
            },
            "done_reason": "stop",
        }

    adapter = OllamaAdapter(transport=transport, model="qwen2.5-coder:7b")
    runner = AgentRunner(project, "codex", provider_adapter=adapter)
    result = runner.run_once()

    assert result.status == "completed", result.reason
    assert fixture.read_text(encoding="utf-8") == "VALUE = 'ollama_after'\n"


