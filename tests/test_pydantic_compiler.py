from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.projections import build_projections
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


def build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)


def test_compile_project_extracts_pydantic_symbols_and_edges(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from pydantic import BaseModel, ConfigDict, Field, field_validator, validator\n\n'
            'class BaseSchema(BaseModel):\n'
            '    id: int\n\n'
            'class User(BaseSchema):\n'
            '    model_config = ConfigDict(populate_by_name=True)\n'
            '    name: str = Field(..., alias="full_name")\n'
            '    age: int = 0\n\n'
            '    @validator("name")\n'
            '    def normalize_name(cls, value):\n'
            '        return value.strip()\n\n'
            'class Account(BaseModel):\n'
            '    slug: str\n\n'
            '    @field_validator("slug", mode="before")\n'
            '    @classmethod\n'
            '    def normalize_slug(cls, value):\n'
            '        return value.lower()\n'
        ),
    )

    data = json.loads(compile_project(fresh_project).to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']

    assert 'BaseSchema.__field__.id' in symbols
    assert 'User.__field__.name' in symbols
    assert 'User.__field__.age' in symbols
    assert 'User.__validator__.normalize_name' in symbols
    assert 'Account.__validator__.normalize_slug' in symbols
    assert 'User.__config__.model_config' in symbols
    assert 'required=true' in symbols['User.__field__.name']['signature']
    assert "alias='full_name'" in symbols['User.__field__.name']['signature']
    assert 'required=false' in symbols['User.__field__.age']['signature']

    assert any(
        edge['relation'] == 'INHERITS'
        and edge['source_id'] == symbols['BaseSchema']['node_id']
        and edge['resolution'] == 'EXTERNAL'
        and edge['target_name'] == 'pydantic.BaseModel'
        for edge in edges
    )
    assert any(
        edge['relation'] == 'INHERITS'
        and edge['source_id'] == symbols['User']['node_id']
        and edge['target_id'] == symbols['BaseSchema']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'VALIDATES'
        and edge['source_id'] == symbols['User.__validator__.normalize_name']['node_id']
        and edge['target_id'] == symbols['User.__field__.name']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'VALIDATES'
        and edge['source_id'] == symbols['Account.__validator__.normalize_slug']['node_id']
        and edge['target_id'] == symbols['Account.__field__.slug']['node_id']
        for edge in edges
    )


def test_graph_models_and_model_cli_report_compiled_pydantic_detail(runner, fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from pydantic import BaseModel, Field, validator\n\n'
            'class User(BaseModel):\n'
            '    name: str = Field(..., alias="full_name")\n\n'
            '    @validator("name")\n'
            '    def normalize_name(cls, value):\n'
            '        return value.strip()\n'
        ),
    )
    build_graph(fresh_project)

    models = runner.invoke(
        cli,
        ['graph', 'models', '--project', str(fresh_project), '--json-output'],
    )
    detail = runner.invoke(
        cli,
        ['graph', 'model', 'User', '--project', str(fresh_project), '--json-output'],
    )

    assert models.exit_code == 0
    assert detail.exit_code == 0
    assert json.loads(models.output)['models'][0]['qualified_name'] == 'User'
    payload = json.loads(detail.output)
    assert payload['qualified_name'] == 'User'
    assert payload['fields'][0]['name'] == 'name'
    assert payload['validators'][0]['name'] == 'normalize_name'
    assert payload['validators'][0]['fields'] == ['name']


def test_incremental_update_preserves_pydantic_artifacts(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from pydantic import BaseModel\n\n'
            'class User(BaseModel):\n'
            '    name: str\n'
        ),
    )
    build_graph(fresh_project)

    put(
        fresh_project,
        'app.py',
        (
            'from pydantic import BaseModel\n\n'
            'class User(BaseModel):\n'
            '    name: str\n'
            '    age: int = 0\n'
        ),
    )
    result = incremental_update(fresh_project)
    ir = read_ir(fresh_project)

    assert result.changed
    assert any(symbol.qualified_name == 'User.__field__.age' for symbol in ir.symbols)
