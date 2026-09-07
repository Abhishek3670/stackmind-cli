from __future__ import annotations

import os

import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.projections.reverse_index import lookup_reverse_edges
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


def put(project, name, text):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_rename_preserves_node_id_and_records_alias(fresh_project):
    put(
        fresh_project,
        'app.py',
        'def target():\n    return 1\n\n\ndef caller():\n    return target()\n',
    )
    write_knowledge(fresh_project, compile_project(fresh_project), built_at='fixed')
    from validators.knowledge.projections import build_projections

    build_projections(fresh_project)
    before = read_ir(fresh_project)
    old_target = next(symbol for symbol in before.symbols if symbol.qualified_name == 'target')
    caller = next(symbol for symbol in before.symbols if symbol.qualified_name == 'caller')

    put(
        fresh_project,
        'app.py',
        'def renamed_target():\n    return 1\n\n\ndef caller():\n    return renamed_target()\n',
    )
    result = incremental_update(fresh_project)
    after = read_ir(fresh_project)
    new_target = next(
        symbol for symbol in after.symbols if symbol.qualified_name == 'renamed_target'
    )
    registry = SymbolRegistry(fresh_project, agent='codex')
    record = registry.load(new_target.node_id)

    assert result.changed
    assert new_target.node_id == old_target.node_id
    assert record is not None
    assert 'app.py:target' in record['aliases']
    assert record['current']['qualified_name'] == 'renamed_target'
    inbound = lookup_reverse_edges(fresh_project, new_target.node_id, relation='CALLS')
    assert inbound
    assert inbound[0]['source_id'] == caller.node_id


def test_move_preserves_node_ids_and_updates_paths(fresh_project):
    put(fresh_project, 'pkg/tool.py', 'def utility():\n    return 1\n')
    write_knowledge(fresh_project, compile_project(fresh_project), built_at='fixed')
    before = read_ir(fresh_project)
    old_utility = next(symbol for symbol in before.symbols if symbol.qualified_name == 'utility')

    os.replace(fresh_project / 'pkg' / 'tool.py', fresh_project / 'pkg' / 'moved.py')
    result = incremental_update(fresh_project)
    after = read_ir(fresh_project)
    moved_utility = next(symbol for symbol in after.symbols if symbol.qualified_name == 'utility')

    assert result.changed
    assert moved_utility.node_id == old_utility.node_id
    assert moved_utility.path == 'pkg/moved.py'


