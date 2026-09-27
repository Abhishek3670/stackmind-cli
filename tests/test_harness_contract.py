from __future__ import annotations
import shutil
from pathlib import Path
import pytest
import yaml
from cli.init import init
from validators.harness import AgentRunner
from validators.harness.runner import CompletionRecord
from tests.test_harness import StaticLLMProvider, _fixed_now, _init_project, _write_yaml

def test_harness_pre_execution_gate_expired_contract(tmp_path):
    project = _init_project(tmp_path)
    
    # 1. Setup work order in TREE
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-101']
    _write_yaml(tree_path, tree)
    
    # 2. Save active work order file
    work_order = {
        'id': 'WO-101',
        'type': 'FEATURE',
        'title': 'Harness contract smoke task',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'module',
            'path': 'app.py',
            'description': 'Main app file',
        },
        'description': 'Test contract pre-execution gate.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-101.yaml', work_order)
    
    # 3. Create expired contract file
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-101",
        "scope": {
            "allow": [],
            "deny": []
        },
        "budget": {
            "expires_at": "2020-07-22T18:00:00Z" # Past date
        }
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-101.yaml", contract_data)
    
    # Copy schemas
    src_schemas = Path(__file__).parent.parent / "schemas"
    shutil.copytree(src_schemas, project / "schemas")
    
    provider = StaticLLMProvider({
        'status': 'completed',
        'summary': 'Task completed',
        'report_markdown': 'Validated successfully.',
        'blockers': [],
        'modified_files': [],
        'retrieval_queries': [],
        'uncertainty': [],
    })
    
    runner = AgentRunner(project, 'codex', llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()
    
    assert result.status == 'blocked'
    assert not result.persisted
    assert 'expired' in result.reason.lower()

def test_harness_post_execution_gate_readonly_violation(tmp_path):
    project = _init_project(tmp_path)
    
    # 1. Setup work order in TREE
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-101']
    _write_yaml(tree_path, tree)
    
    # 2. Save active work order file
    work_order = {
        'id': 'WO-101',
        'type': 'FEATURE',
        'title': 'Harness contract smoke task',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'module',
            'path': 'app.py',
            'description': 'Main app file',
        },
        'description': 'Test contract post-execution gate.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-101.yaml', work_order)
    
    # 3. Create read-only contract file
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-101",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": [],
            "write": "read-only" # Read-only!
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z"
        }
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-101.yaml", contract_data)
    
    # Copy schemas
    src_schemas = Path(__file__).parent.parent / "schemas"
    shutil.copytree(src_schemas, project / "schemas")
    
    # Mock LLM provider returns modified_files even though contract is read-only
    provider = StaticLLMProvider({
        'status': 'completed',
        'summary': 'Task completed',
        'report_markdown': 'Validated successfully.',
        'blockers': [],
        'modified_files': ['app.py'],
        'release_target': 'v2.0.0',
        'retrieval_queries': [],
        'uncertainty': [],
    })
    
    runner = AgentRunner(project, 'codex', llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()
    
    assert result.status == 'blocked'
    assert not result.persisted
    assert 'read-only' in result.reason.lower()

def test_harness_post_execution_gate_scope_violation(tmp_path):
    project = _init_project(tmp_path)
    
    # 1. Setup work order in TREE
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-101']
    _write_yaml(tree_path, tree)
    
    # 2. Save active work order file
    work_order = {
        'id': 'WO-101',
        'type': 'FEATURE',
        'title': 'Harness contract smoke task',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'module',
            'path': 'app.py',
            'description': 'Main app file',
        },
        'description': 'Test contract scope violation.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-101.yaml', work_order)
    
    # 3. Create scoped contract file
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-101",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": [],
            "write": "read-write"
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z"
        }
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-101.yaml", contract_data)
    
    # Copy schemas
    src_schemas = Path(__file__).parent.parent / "schemas"
    shutil.copytree(src_schemas, project / "schemas")
    
    # Mock LLM provider returns modified_files outside of allowed scope
    provider = StaticLLMProvider({
        'status': 'completed',
        'summary': 'Task completed',
        'report_markdown': 'Validated successfully.',
        'blockers': [],
        'modified_files': ['unauthorized.py'], # NOT IN ALLOW SCOPE!
        'release_target': 'v2.0.0',
        'retrieval_queries': [],
        'uncertainty': [],
    })
    
    runner = AgentRunner(project, 'codex', llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()
    
    assert result.status == 'blocked'
    assert not result.persisted
    assert 'outside allowed contract scope' in result.reason.lower() or 'denied' in result.reason.lower()

def test_harness_post_execution_gate_d025_violation(tmp_path):
    project = _init_project(tmp_path)
    
    # 1. Setup work order in TREE
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-101']
    _write_yaml(tree_path, tree)
    
    # 2. Save active work order file
    work_order = {
        'id': 'WO-101',
        'type': 'FEATURE',
        'title': 'Harness contract smoke task',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'module',
            'path': 'app.py',
            'description': 'Main app file',
        },
        'description': 'Test D025 violation.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-101.yaml', work_order)
    
    # 3. Create scoped contract file
    contract_data = {
        "agent_id": "codex",
        "work_order": "WO-101",
        "scope": {
            "allow": [
                {"module": "app", "depth": 0}
            ],
            "deny": [],
            "write": "read-write"
        },
        "budget": {
            "expires_at": "2036-07-22T18:00:00Z"
        }
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-101.yaml", contract_data)
    
    # Copy schemas
    src_schemas = Path(__file__).parent.parent / "schemas"
    shutil.copytree(src_schemas, project / "schemas")
    
    # Mock LLM provider returns a destructive command
    provider = StaticLLMProvider({
        'status': 'completed',
        'summary': 'Task completed',
        'report_markdown': 'Validated successfully.',
        'blockers': [],
        'modified_files': [],
        'release_target': 'v2.0.0',
        'retrieval_queries': [],
        'uncertainty': [],
        'commands': ['git reset --hard HEAD~1'] # DESTRUCTIVE COMMAND
    })
    
    runner = AgentRunner(project, 'codex', llm_provider=provider, now_fn=_fixed_now)
    result = runner.run_once()
    
    assert result.status == 'blocked'
    assert not result.persisted
    assert 'd025' in result.reason.lower() or 'destructive' in result.reason.lower()


def test_arbitrary_non_python_scope_rule_matching(tmp_path):
    """Confirm scope rule matching works generally for arbitrary non-Python file extensions and globs."""
    project = _init_project(tmp_path)
    
    # 1. Setup work order
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-201']
    _write_yaml(tree_path, tree)
    
    work_order = {
        'id': 'WO-201',
        'type': 'FEATURE',
        'title': 'Arbitrary non-python config and doc updates',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'doc',
            'path': 'README.md',
            'description': 'Docs update',
        },
        'description': 'Update README and JSON config.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-201.yaml', work_order)
    
    index_path = project / '.sync' / 'work-orders' / 'INDEX.yaml'
    index_data = yaml.safe_load(index_path.read_text(encoding='utf-8'))
    index_data['orders'].append({
        'id': 'WO-201',
        'type': 'FEATURE',
        'title': work_order['title'],
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'doc',
            'path': 'README.md',
            'description': 'Docs update',
        },
    })
    _write_yaml(index_path, index_data)

    # 2. Setup contract scoping arbitrary non-Python files: README.md, config/*.json, docs/*.txt
    # and explicitly denying secrets.json
    contract_data = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-201",
        "scope": {
            "allow": [
                {"module": "README.md"},
                {"module": "config/*.json"},
                {"module": "docs/**"},
            ],
            "deny": [
                {"module": "secrets.json"},
                {"module": "private/**"},
            ],
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": 10,
            "max_tokens": 10000,
        },
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-201.yaml", contract_data)

    src_schemas = Path(__file__).parent.parent / "schemas"
    if not (project / "schemas").exists():
        shutil.copytree(src_schemas, project / "schemas")

    # 3. Create target files in project
    (project / "README.md").write_text("# Project Docs\n", encoding="utf-8")
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "config" / "settings.json").write_text('{"env": "test"}\n', encoding="utf-8")
    (project / "docs").mkdir(parents=True, exist_ok=True)
    (project / "docs" / "notes.txt").write_text("Notes\n", encoding="utf-8")

    from validators.kernel.providers import OpenAICompatibleAdapter
    import json

    # Allowed turn: modifying README.md and config/settings.json via write_file tool
    calls = 0
    def allowed_transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "w1", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "README.md", "content": "# Updated Docs\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        if calls == 2:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "w2", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "config/settings.json", "content": '{"env": "prod"}\n'}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "completed", "summary": "Updated README and config", "report_markdown": "Done.",
            "blockers": [], "modified_files": ["README.md", "config/settings.json"],
            "release_target": "v1.0.0", "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    runner = AgentRunner(project, 'codex', provider_adapter=OpenAICompatibleAdapter(transport=allowed_transport), now_fn=_fixed_now)
    result = runner.run_once()
    assert result.status == 'completed', result.reason
    assert result.persisted is True
    assert (project / "README.md").read_text(encoding="utf-8") == "# Updated Docs\n"
    assert (project / "config" / "settings.json").read_text(encoding="utf-8") == '{"env": "prod"}\n'

    # Denied turn: attempting to touch denied secrets.json
    denied_calls = 0
    def denied_transport(payload, stream, timeout):
        nonlocal denied_calls
        denied_calls += 1
        if denied_calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "w_deny", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "secrets.json", "content": '{"key": "leak"}\n'}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "blocked", "summary": "Denied", "report_markdown": "denied",
            "blockers": ["scope"], "modified_files": [], "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    denied_runner = AgentRunner(project, 'codex', provider_adapter=OpenAICompatibleAdapter(transport=denied_transport), now_fn=_fixed_now)
    denied_result = denied_runner.run_once()
    assert denied_result.status == 'blocked'


def test_narrower_ignore_list_no_false_positive_declaration_matches_for_ordinary_backend_turn(tmp_path):
    """Confirm that the narrower snapshot ignore list does not cause false-positive declaration_matches

    failures on ordinary backend code turns where harness writes internal bookkeeping under .sync/.
    """
    project = _init_project(tmp_path)
    
    # Setup work order in TREE
    tree_path = project / '.sync' / 'runtime' / 'TREE.yaml'
    tree = yaml.safe_load(tree_path.read_text(encoding='utf-8'))
    tree['agents']['codex']['assigned_work_orders'] = ['WO-301']
    _write_yaml(tree_path, tree)
    
    # Save active work order file
    work_order = {
        'id': 'WO-301',
        'type': 'FEATURE',
        'title': 'Implement user authentication service',
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'code',
            'path': 'src/auth.py',
            'description': 'Auth service module',
        },
        'description': 'Implement auth service without touching .sync paths.',
    }
    _write_yaml(project / '.sync' / 'work-orders' / 'ACTIVE' / 'WO-301.yaml', work_order)
    
    index_path = project / '.sync' / 'work-orders' / 'INDEX.yaml'
    index_data = yaml.safe_load(index_path.read_text(encoding='utf-8'))
    index_data['orders'].append({
        'id': 'WO-301',
        'type': 'FEATURE',
        'title': work_order['title'],
        'status': 'ACTIVE',
        'priority': 'P2',
        'assigned_agents': ['codex'],
        'dependencies': [],
        'deliverable': {
            'type': 'code',
            'path': 'src/auth.py',
            'description': 'Auth service module',
        },
    })
    _write_yaml(index_path, index_data)

    # Setup standard scoped contract allowing src/**
    contract_data = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": "WO-301",
        "scope": {
            "allow": [
                {"module": "src/**"},
            ],
            "deny": [
                {"module": ".sync/**"},
            ],
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": 5,
            "max_tokens": 10000,
        },
    }
    contracts_dir = project / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(contracts_dir / "WO-301.yaml", contract_data)

    src_schemas = Path(__file__).parent.parent / "schemas"
    if not (project / "schemas").exists():
        shutil.copytree(src_schemas, project / "schemas")

    from validators.kernel.providers import OpenAICompatibleAdapter
    import json

    calls = 0
    def transport(payload, stream, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "w_auth", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": "src/auth.py", "content": "def authenticate(): return True\n"}),
                },
            }]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "status": "completed", "summary": "Implemented auth service", "report_markdown": "Auth service done.",
            "blockers": [], "modified_files": ["src/auth.py"], "release_target": "v1.1.0",
            "retrieval_queries": [], "uncertainty": [], "commands": [],
        })}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}

    runner = AgentRunner(project, 'codex', provider_adapter=OpenAICompatibleAdapter(transport=transport), now_fn=_fixed_now)
    result = runner.run_once()
    
    # Verification must pass completely: declaration_matches is True, scope_verified is True,
    # and harness-owned bookkeeping (.sync/work-orders/ACTIVE/WO-301.yaml log, .sync/inbox review notice)
    # does NOT cause a false-positive declaration mismatch.
    assert result.status == 'completed', f"Turn failed: {result.reason}"
    assert result.persisted is True
    assert (project / "src" / "auth.py").exists()
    assert (project / "src" / "auth.py").read_text(encoding="utf-8") == "def authenticate(): return True\n"
