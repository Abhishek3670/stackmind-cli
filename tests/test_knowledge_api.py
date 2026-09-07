from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.projections import build_projections
from validators.knowledge.storage import latest_revision_id, read_ir
from validators.knowledge.writer import write_knowledge


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


@pytest.fixture
def git_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=False)
    return project


@pytest.fixture
def runner():
    return CliRunner()


def put(project: Path, name: str, text: str) -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)


def commit_all(project: Path, message: str) -> None:
    subprocess.run(
        ['git', 'config', 'user.name', 'StackMind Tests'],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'tests@example.com'],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ['git', 'add', '.'],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', message],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    )


def test_lookup_is_alias_aware_after_rename(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)
    before = read_ir(fresh_project)
    old = next(symbol for symbol in before.symbols if symbol.qualified_name == 'target')

    put(fresh_project, 'app.py', 'def renamed_target():\n    return 1\n')
    incremental_update(fresh_project)

    api = KnowledgeAPI(fresh_project)
    result = api.lookup('target').results[0]

    assert result.alias_matched
    assert result.node_id == old.node_id
    assert result.qualified_name == 'renamed_target'


def test_filter_and_text_search_are_revision_stamped(fresh_project):
    put(
        fresh_project,
        'pkg/app.py',
        'class Greeter:\n    def hello_world(self, name: str) -> str:\n        return name\n',
    )
    build_graph(fresh_project)
    api = KnowledgeAPI(fresh_project)

    filtered = api.filter(kind='Method', path_contains='pkg', qualified_name_contains='hello')
    searched = api.search('hello world greeter')

    assert filtered.revision == 1
    assert filtered.git_commit is None
    assert filtered.results
    assert filtered.results[0].qualified_name == 'Greeter.hello_world'
    assert not searched.semantic
    assert searched.results
    assert any(item.qualified_name == 'Greeter.hello_world' for item in searched.results)


def test_callers_and_impact_use_knowledge_without_repo_file_reads(fresh_project, monkeypatch):
    put(
        fresh_project,
        'app.py',
        (
            'def target():\n'
            '    return 1\n\n'
            'def caller():\n'
            '    return target()\n\n'
            'def entrypoint():\n'
            '    return caller()\n'
        ),
    )
    build_graph(fresh_project)
    api = KnowledgeAPI(fresh_project)

    original = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self.suffix == '.py' and '.sync' not in self.parts:
            raise AssertionError('Knowledge API must not read source files on the read path')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read_text)
    callers = api.callers('target')
    impact = api.impact('target', depth=2)

    assert [item.qualified_name for item in callers.results] == ['caller']
    assert [item.qualified_name for item in impact.results] == ['caller', 'entrypoint']


def test_assemble_context_reports_truncation(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'def helper_one(value):\n'
            '    return value + 1\n\n'
            'def helper_two(value):\n'
            '    return helper_one(value)\n\n'
            'def helper_three(value):\n'
            '    return helper_two(value)\n'
        ),
    )
    build_graph(fresh_project)
    api = KnowledgeAPI(fresh_project)

    bundle = api.assemble_context('helper', token_budget=20, limit=5)

    assert bundle.truncated
    assert bundle.truncation_reason is not None
    assert bundle.estimated_tokens <= bundle.token_budget


def test_stale_results_are_flagged_without_recompile(git_project):
    put(git_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(git_project)
    commit_all(git_project, 'baseline knowledge build')
    api = KnowledgeAPI(git_project)

    fresh = api.lookup('target')
    put(git_project, 'app.py', 'def target():\n    return 2\n')
    stale = api.lookup('target')

    assert not fresh.stale
    assert stale.stale


def test_api_queries_are_read_only(fresh_project):
    put(
        fresh_project,
        'app.py',
        'def target():\n    return 1\n\n\ndef caller():\n    return target()\n',
    )
    build_graph(fresh_project)
    api = KnowledgeAPI(fresh_project)
    before_revision = latest_revision_id(fresh_project)

    api.lookup('target')
    api.filter(kind='Function')
    api.callers('target')
    api.search('target caller')
    api.assemble_context('target', token_budget=80)

    assert latest_revision_id(fresh_project) == before_revision


def test_graph_cli_query_callers_impact_explain_and_context(runner, fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'def target():\n'
            '    return 1\n\n'
            'def caller():\n'
            '    return target()\n'
        ),
    )
    build_graph(fresh_project)

    query = runner.invoke(
        cli,
        ['graph', 'query', 'target', '--project', str(fresh_project), '--json-output'],
    )
    callers = runner.invoke(
        cli,
        ['graph', 'callers', 'target', '--project', str(fresh_project)],
    )
    impact = runner.invoke(
        cli,
        ['graph', 'impact', 'target', '--project', str(fresh_project), '--json-output'],
    )
    explain = runner.invoke(
        cli,
        ['graph', 'explain', 'target', '--project', str(fresh_project)],
    )
    context = runner.invoke(
        cli,
        [
            'graph',
            'context',
            'target',
            '--project',
            str(fresh_project),
            '--token-budget',
            '80',
        ],
    )

    assert query.exit_code == 0
    assert json.loads(query.output)['results'][0]['qualified_name'] == 'target'
    assert callers.exit_code == 0
    assert 'caller' in callers.output
    assert impact.exit_code == 0
    assert json.loads(impact.output)['results'][0]['qualified_name'] == 'caller'
    assert explain.exit_code == 0
    assert 'callers: 1' in explain.output
    assert context.exit_code == 0
    assert 'token_budget: 80' in context.output
