from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.projections import build_projections
from validators.knowledge.projections.reverse_index import lookup_reverse_edges
from validators.knowledge.projections.search import search_symbols
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


@pytest.fixture
def runner():
    return CliRunner()


def put(project: Path, name: str, text: str) -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def test_projection_rebuildability_from_t1(fresh_project):
    put(
        fresh_project,
        'app.py',
        'def later():\n    return 1\n\n\ndef main():\n    return later()\n',
    )
    write_knowledge(fresh_project, compile_project(fresh_project), built_at='fixed')

    build_projections(fresh_project)
    root = fresh_project / '.sync' / 'knowledge' / 'cache'
    first = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob('*.json'))
    }

    shutil.rmtree(root)
    build_projections(fresh_project)
    second = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob('*.json'))
    }

    assert second == first


def test_reverse_index_lookup_uses_direct_shard(fresh_project, monkeypatch):
    put(
        fresh_project,
        'app.py',
        'def later():\n    return 1\n\n\ndef main():\n    return later()\n',
    )
    write_knowledge(fresh_project, compile_project(fresh_project), built_at='fixed')
    build_projections(fresh_project)

    ir = read_ir(fresh_project)
    target = next(symbol for symbol in ir.symbols if symbol.qualified_name == 'later')
    source = next(symbol for symbol in ir.symbols if symbol.qualified_name == 'main')

    def fail_glob(*args, **kwargs):
        raise AssertionError('reverse lookup must not scan all nodes')

    monkeypatch.setattr(Path, 'glob', fail_glob)
    callers = lookup_reverse_edges(fresh_project, target.node_id, relation='CALLS')

    assert callers
    assert callers[0]['source_id'] == source.node_id
    assert callers[0]['relation'] == 'CALLS'


def test_search_projection_indexes_symbol_names(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'class Greeter:\n'
            '    def hello_world(self, name: str) -> str:\n'
            '        return name\n'
        ),
    )
    write_knowledge(fresh_project, compile_project(fresh_project), built_at='fixed')
    build_projections(fresh_project)

    results = search_symbols(fresh_project, 'greeter hello world')

    assert results
    assert any(item['qualified_name'] == 'Greeter.hello_world' for item in results)


def test_graph_build_cli_materializes_complete_store(runner, fresh_project):
    put(
        fresh_project,
        'app.py',
        'def later():\n    return 1\n\n\ndef main():\n    return later()\n',
    )

    result = runner.invoke(cli, ['graph', 'build', '--project', str(fresh_project)])

    assert result.exit_code == 0
    assert (fresh_project / '.sync' / 'knowledge' / 'nodes').exists()
    assert (fresh_project / '.sync' / 'knowledge' / 'cache' / 'reverse_index').exists()
    assert (fresh_project / '.sync' / 'knowledge' / 'cache' / 'search').exists()
    assert (fresh_project / '.sync' / 'knowledge' / 'cache' / 'metrics').exists()
