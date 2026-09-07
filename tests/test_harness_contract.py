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
