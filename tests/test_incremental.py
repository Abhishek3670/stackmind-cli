from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import (
    collect_git_python_changes,
    incremental_update,
)
from validators.knowledge.compiler.rename import AppearedSymbol, HistoricalSymbol, detect_renames
from validators.knowledge.compiler.watcher import PollingWatcher
from validators.knowledge.storage import latest_revision_id
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


def seed_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')


def test_content_hash_skip_produces_zero_new_revisions(fresh_project):
    put(fresh_project, 'app.py', 'def main():\n    return 1\n')
    seed_graph(fresh_project)
    before = latest_revision_id(fresh_project)

    result = incremental_update(fresh_project)

    assert not result.changed
    assert result.revision_id is None
    assert latest_revision_id(fresh_project) == before


def test_incremental_update_rewrites_only_changed_file_nodes(fresh_project):
    put(
        fresh_project,
        'app.py',
        'def one():\n    return 1\n\n\ndef two():\n    return 2\n',
    )
    seed_graph(fresh_project)

    put(
        fresh_project,
        'app.py',
        'def one():\n    return 10\n\n\ndef two():\n    return 2\n',
    )
    result = incremental_update(fresh_project)
    node_writes = [path for path in result.written_paths if 'nodes' in str(path)]

    assert result.changed
    assert result.revision_id is not None
    assert len([path for path in node_writes if '/function/' in path.as_posix().lower()]) == 1


def test_graph_update_cli_works_without_git(runner, fresh_project):
    put(fresh_project, 'app.py', 'def main():\n    return 1\n')
    seed_graph(fresh_project)
    put(fresh_project, 'app.py', 'def main():\n    return 2\n')

    result = runner.invoke(cli, ['graph', 'update', '--project', str(fresh_project)])

    assert result.exit_code == 0
    assert 'Incremental update complete' in result.output


def test_collect_git_python_changes_handles_rename(tmp_path):
    project = tmp_path / 'git-project'
    init(project, name='Project', no_git=False)
    put(project, 'old_name.py', 'def main():\n    return 1\n')
    os.replace(project / 'old_name.py', project / 'new_name.py')

    changed, deleted = collect_git_python_changes(project)

    assert 'new_name.py' in changed or 'old_name.py' in changed


def test_watcher_excludes_sync_and_debounces(fresh_project):
    put(fresh_project, 'app.py', 'def main():\n    return 1\n')
    seed_graph(fresh_project)
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def runner_callback(project_path, *, agent, changed_paths, deleted_paths):
        calls.append((changed_paths, deleted_paths))
        return incremental_update(
            project_path,
            agent=agent,
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
        )

    watcher = PollingWatcher(
        fresh_project,
        debounce_seconds=1.0,
        poll_interval=0.0,
        runner=runner_callback,
    )
    sync_python = fresh_project / '.sync' / 'knowledge' / 'cache' / 'ignored.py'
    sync_python.parent.mkdir(parents=True, exist_ok=True)
    sync_python.write_text('print(1)\n', encoding='utf-8')
    watcher.step(now=0.0)
    assert not calls

    app = fresh_project / 'app.py'
    app.write_text('def main():\n    return 2\n', encoding='utf-8')
    watcher.step(now=0.1)
    app.write_text('def main():\n    return 3\n', encoding='utf-8')
    watcher.step(now=0.2)
    watcher.step(now=1.3)

    assert len(calls) == 1
    assert calls[0][0] == ('app.py',)


def test_detect_renames_matches_unique_continuity_candidate():
    matches, unmatched = detect_renames(
        [
            HistoricalSymbol(
                node_id='FUNC-1234567890abcdef',
                kind='Function',
                path='app.py',
                qualified_name='old_name',
                signature='def old_name()',
                content_hash='hash',
                owner_qualified_name=None,
            )
        ],
        [
            AppearedSymbol(
                kind='Function',
                path='app.py',
                qualified_name='new_name',
                signature='def old_name()',
                content_hash='hash',
                owner_qualified_name=None,
            )
        ],
    )

    assert len(matches) == 1
    assert matches[0].node_id == 'FUNC-1234567890abcdef'
    assert not unmatched


