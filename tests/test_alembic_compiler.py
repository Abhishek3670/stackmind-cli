from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.projections import build_projections
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


def build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)


def test_compile_alembic_migration_flow_and_reconstruction(fresh_project):
    put(
        fresh_project,
        'migrations/versions/1a2b3c_create_users.py',
        (
            'revision = "1a2b3c"\n'
            'down_revision = None\n\n'
            'def upgrade():\n'
            '    op.create_table(\n'
            '        "users",\n'
            '        sa.Column("id", sa.Integer(), primary_key=True),\n'
            '        sa.Column("name", sa.String())\n'
            '    )\n'
        ),
    )
    put(
        fresh_project,
        'migrations/versions/4d5e6f_add_email.py',
        (
            'revision = "4d5e6f"\n'
            'down_revision = "1a2b3c"\n\n'
            'def upgrade():\n'
            '    op.add_column("users", sa.Column("email", sa.String()))\n'
            '    op.create_table("logs", sa.Column("id", sa.Integer()))\n'
        ),
    )
    put(
        fresh_project,
        'migrations/versions/7g8h9i_remove_email.py',
        (
            'revision = "7g8h9i"\n'
            'down_revision = "4d5e6f"\n\n'
            'def upgrade():\n'
            '    op.drop_column("users", "email")\n'
            '    op.drop_table("logs")\n'
            '    op.alter_column("users", "name", nullable=True)\n'
        ),
    )

    ir = compile_project(fresh_project)
    data = json.loads(ir.to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']

    # Check migration symbols are registered
    assert 'alembic.migration.1a2b3c' in symbols
    assert 'alembic.migration.4d5e6f' in symbols
    assert 'alembic.migration.7g8h9i' in symbols

    # Check dependencies (DEPENDS_ON_MIGRATION)
    assert any(
        e['relation'] == 'DEPENDS_ON_MIGRATION'
        and e['source_id'] == symbols['alembic.migration.4d5e6f']['node_id']
        and e['target_id'] == symbols['alembic.migration.1a2b3c']['node_id']
        for e in edges
    )
    assert any(
        e['relation'] == 'DEPENDS_ON_MIGRATION'
        and e['source_id'] == symbols['alembic.migration.7g8h9i']['node_id']
        and e['target_id'] == symbols['alembic.migration.4d5e6f']['node_id']
        for e in edges
    )


def test_alembic_cli_commands(runner, fresh_project):
    put(
        fresh_project,
        'migrations/versions/1a2b3c_create_users.py',
        (
            'revision = "1a2b3c"\n'
            'down_revision = None\n\n'
            'def upgrade():\n'
            '    op.create_table(\n'
            '        "users",\n'
            '        sa.Column("id", sa.Integer(), primary_key=True),\n'
            '        sa.Column("name", sa.String())\n'
            '    )\n'
        ),
    )
    put(
        fresh_project,
        'migrations/versions/4d5e6f_add_email.py',
        (
            'revision = "4d5e6f"\n'
            'down_revision = "1a2b3c"\n\n'
            'def upgrade():\n'
            '    op.add_column("users", sa.Column("email", sa.String()))\n'
            '    op.create_table("logs", sa.Column("id", sa.Integer()))\n'
        ),
    )
    build_graph(fresh_project)

    # 1. Test migrations list
    res_migs = runner.invoke(cli, ['graph', 'migrations', '--project', str(fresh_project), '--json-output'])
    assert res_migs.exit_code == 0
    data_migs = json.loads(res_migs.output)
    assert len(data_migs['migrations']) == 2
    assert data_migs['migrations'][0]['revision'] == '1a2b3c'
    assert data_migs['migrations'][1]['revision'] == '4d5e6f'

    # Test migrations text output
    res_migs_text = runner.invoke(cli, ['graph', 'migrations', '--project', str(fresh_project)])
    assert res_migs_text.exit_code == 0
    assert 'migrations: 2' in res_migs_text.output
    assert '1a2b3c (base)' in res_migs_text.output
    assert '4d5e6f (<- 1a2b3c)' in res_migs_text.output

    # 2. Test schema at specific revision
    # At 1a2b3c: only users table with columns id, name
    res_schema_1 = runner.invoke(cli, ['graph', 'schema', '--project', str(fresh_project), '--at', '1a2b3c', '--json-output'])
    assert res_schema_1.exit_code == 0
    data_schema_1 = json.loads(res_schema_1.output)
    assert data_schema_1['revision'] == '1a2b3c'
    assert data_schema_1['schema']['users'] == ['id', 'name']
    assert 'logs' not in data_schema_1['schema']

    # At 4d5e6f: users (id, name, email) and logs (id)
    res_schema_2 = runner.invoke(cli, ['graph', 'schema', '--project', str(fresh_project), '--at', '4d5e6f', '--json-output'])
    assert res_schema_2.exit_code == 0
    data_schema_2 = json.loads(res_schema_2.output)
    assert data_schema_2['revision'] == '4d5e6f'
    assert data_schema_2['schema']['users'] == ['id', 'name', 'email']
    assert data_schema_2['schema']['logs'] == ['id']

    # Test schema text output
    res_schema_text = runner.invoke(cli, ['graph', 'schema', '--project', str(fresh_project), '--at', '4d5e6f'])
    assert res_schema_text.exit_code == 0
    assert 'schema at revision 4d5e6f:' in res_schema_text.output
    assert '- users: id, name, email' in res_schema_text.output
    assert '- logs: id' in res_schema_text.output

    # Test invalid revision
    res_schema_invalid = runner.invoke(cli, ['graph', 'schema', '--project', str(fresh_project), '--at', '999999'])
    assert res_schema_invalid.exit_code != 0
    assert 'Revision \'999999\' not found' in res_schema_invalid.output
