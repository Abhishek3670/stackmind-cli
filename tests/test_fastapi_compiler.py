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


def test_compile_project_extracts_fastapi_routes_dependencies_and_models(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from fastapi import APIRouter, Depends, FastAPI\n'
            'from fastapi.middleware.cors import CORSMiddleware\n'
            'from fastapi.security import OAuth2PasswordBearer\n'
            'from pydantic import BaseModel\n\n'
            'app = FastAPI()\n'
            'router = APIRouter()\n'
            'oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")\n'
            'app.add_middleware(CORSMiddleware)\n\n'
            'class UserIn(BaseModel):\n'
            '    name: str\n\n'
            'class UserOut(BaseModel):\n'
            '    id: int\n'
            '    name: str\n\n'
            'def current_user(token: str = Depends(oauth2_scheme)):\n'
            '    return token\n\n'
            '@router.post("/users", response_model=UserOut, status_code=201)\n'
            'def create_user(payload: UserIn, user=Depends(current_user)):\n'
            '    return {"id": 1, "name": payload.name}\n\n'
            'app.include_router(router, prefix="/api")\n'
        ),
    )

    data = json.loads(compile_project(fresh_project).to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']
    route = next(item for item in data['symbols'] if item['kind'] == 'FastAPIRoute')

    assert 'POST /api/users' in route['signature']
    assert any(item['kind'] == 'FastAPIDependency' for item in data['symbols'])
    assert any(item['kind'] == 'FastAPIAuth' for item in data['symbols'])
    assert any(item['kind'] == 'FastAPIMiddleware' for item in data['symbols'])
    assert any(
        edge['relation'] == 'HANDLES'
        and edge['source_id'] == route['node_id']
        and edge['target_id'] == symbols['create_user']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'USES_REQUEST_MODEL'
        and edge['source_id'] == route['node_id']
        and edge['target_id'] == symbols['UserIn']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'USES_RESPONSE_MODEL'
        and edge['source_id'] == route['node_id']
        and edge['target_id'] == symbols['UserOut']['node_id']
        for edge in edges
    )
    assert any(edge['relation'] == 'ROUTE_DEPENDS_ON' for edge in edges)


def test_graph_fastapi_cli_reports_routes_endpoint_auth_and_middleware(runner, fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from fastapi import Depends, FastAPI\n'
            'from fastapi.security import OAuth2PasswordBearer\n'
            'from pydantic import BaseModel\n\n'
            'app = FastAPI()\n'
            'oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")\n\n'
            'class Payload(BaseModel):\n'
            '    name: str\n\n'
            'class Result(BaseModel):\n'
            '    name: str\n\n'
            'def current_user(token: str = Depends(oauth2_scheme)):\n'
            '    return token\n\n'
            '@app.get("/items", response_model=Result)\n'
            'def list_items(payload: Payload, user=Depends(current_user)):\n'
            '    return payload\n'
        ),
    )
    build_graph(fresh_project)

    routes = runner.invoke(cli, ['graph', 'routes', '--project', str(fresh_project), '--json-output'])
    endpoint = runner.invoke(cli, ['graph', 'endpoint', '/items', '--project', str(fresh_project), '--json-output'])
    auth = runner.invoke(cli, ['graph', 'auth', '--project', str(fresh_project), '--json-output'])
    middleware = runner.invoke(cli, ['graph', 'middleware', '--project', str(fresh_project), '--json-output'])

    assert routes.exit_code == 0
    assert endpoint.exit_code == 0
    assert auth.exit_code == 0
    assert middleware.exit_code == 0
    assert json.loads(routes.output)['routes'][0]['path'] == '/items'
    detail = json.loads(endpoint.output)
    assert detail['endpoint'] == 'list_items'
    assert detail['response_model'] == 'Result'
    assert detail['request_models'] == ['Payload']
    assert detail['dependencies'][0]['target'] == 'current_user'
    assert detail['auth'] == ['oauth2_scheme']
    assert json.loads(auth.output)['auth'][0]['name'] == 'oauth2_scheme'
    assert json.loads(middleware.output)['middleware'] == []


def test_incremental_update_preserves_fastapi_artifacts(fresh_project):
    put(
        fresh_project,
        'app.py',
        (
            'from fastapi import FastAPI\n\n'
            'app = FastAPI()\n\n'
            '@app.get("/items")\n'
            'def list_items():\n'
            '    return []\n'
        ),
    )
    build_graph(fresh_project)

    put(
        fresh_project,
        'app.py',
        (
            'from fastapi import FastAPI\n\n'
            'app = FastAPI()\n\n'
            '@app.get("/items")\n'
            'def list_items():\n'
            '    return []\n\n'
            '@app.post("/items")\n'
            'def create_item():\n'
            '    return {}\n'
        ),
    )
    result = incremental_update(fresh_project)
    ir = read_ir(fresh_project)

    assert result.changed
    assert len([symbol for symbol in ir.symbols if symbol.kind == 'FastAPIRoute']) == 2
