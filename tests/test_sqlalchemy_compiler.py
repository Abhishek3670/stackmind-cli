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


def test_compile_project_extracts_sqlalchemy_schema_symbols_and_edges(fresh_project):
    put(
        fresh_project,
        'models.py',
        (
            'from sqlalchemy import Column, ForeignKey, Integer, String, Table\n'
            'from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker\n\n'
            'class Base(DeclarativeBase):\n'
            '    pass\n\n'
            'user_roles = Table(\n'
            '    "user_roles",\n'
            '    Base.metadata,\n'
            '    Column("user_id", ForeignKey("users.id"), primary_key=True),\n'
            '    Column("role_id", ForeignKey("roles.id"), primary_key=True),\n'
            ')\n\n'
            'SessionLocal = sessionmaker()\n\n'
            'class User(Base):\n'
            '    __tablename__ = "users"\n'
            '    id: Mapped[int] = mapped_column(primary_key=True)\n'
            '    name = Column(String, unique=True, nullable=False)\n'
            '    orders: Mapped[list["Order"]] = relationship("Order", back_populates="user")\n\n'
            'class Order(Base):\n'
            '    __tablename__ = "orders"\n'
            '    id = Column(Integer, primary_key=True)\n'
            '    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)\n'
            '    user = relationship("User", back_populates="orders")\n\n'
            'class UserRepository:\n'
            '    def __init__(self, session):\n'
            '        self.session = session\n\n'
            '    def get(self, user_id):\n'
            '        return self.session.get(User, user_id)\n'
        ),
    )

    data = json.loads(compile_project(fresh_project).to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']

    assert 'User.__column__.id' in symbols
    assert 'User.__column__.name' in symbols
    assert 'Order.__column__.user_id' in symbols
    assert 'User.__relationship__.orders' in symbols
    assert 'Order.__relationship__.user' in symbols
    assert 'models.__association_table__.user_roles' in symbols
    assert 'models.__session__.SessionLocal' in symbols
    assert 'UserRepository.__repository__' in symbols
    assert 'primary_key=true' in symbols['User.__column__.id']['signature']
    assert 'unique=true' in symbols['User.__column__.name']['signature']
    assert 'nullable=false' in symbols['Order.__column__.user_id']['signature']
    assert any(
        edge['relation'] == 'DECLARES_COLUMN'
        and edge['source_id'] == symbols['User']['node_id']
        and edge['target_id'] == symbols['User.__column__.id']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'FOREIGN_KEY'
        and edge['source_id'] == symbols['Order.__column__.user_id']['node_id']
        and edge['target_name'] == 'users.id'
        for edge in edges
    )
    assert any(
        edge['relation'] == 'RELATES_TO'
        and edge['source_id'] == symbols['User.__relationship__.orders']['node_id']
        and edge['target_id'] == symbols['Order']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'MANAGES_MODEL'
        and edge['source_id'] == symbols['UserRepository.__repository__']['node_id']
        and edge['target_id'] == symbols['User']['node_id']
        for edge in edges
    )


def test_graph_sqlalchemy_cli_reports_model_relations_and_schema(runner, fresh_project):
    put(
        fresh_project,
        'models.py',
        (
            'from sqlalchemy import Column, ForeignKey, Integer, String\n'
            'from sqlalchemy.orm import declarative_base, relationship, sessionmaker\n\n'
            'Base = declarative_base()\n'
            'SessionLocal = sessionmaker()\n\n'
            'class User(Base):\n'
            '    __tablename__ = "users"\n'
            '    id = Column(Integer, primary_key=True)\n'
            '    name = Column(String, nullable=False)\n'
            '    orders = relationship("Order", back_populates="user")\n\n'
            'class Order(Base):\n'
            '    __tablename__ = "orders"\n'
            '    id = Column(Integer, primary_key=True)\n'
            '    user_id = Column(Integer, ForeignKey("users.id"))\n'
            '    user = relationship("User", back_populates="orders")\n'
        ),
    )
    build_graph(fresh_project)

    model = runner.invoke(cli, ['graph', 'model', 'User', '--project', str(fresh_project), '--json-output'])
    relations = runner.invoke(cli, ['graph', 'relations', 'User', '--project', str(fresh_project), '--json-output'])
    schema = runner.invoke(cli, ['graph', 'schema', '--project', str(fresh_project), '--json-output'])

    assert model.exit_code == 0
    assert relations.exit_code == 0
    assert schema.exit_code == 0
    payload = json.loads(model.output)
    assert payload['qualified_name'] == 'User'
    assert payload['framework'] == 'sqlalchemy'
    assert [column['name'] for column in payload['columns']] == ['id', 'name']
    assert payload['relationships'][0]['target'] == 'Order'
    rel_payload = json.loads(relations.output)
    assert rel_payload['relationships'][0]['name'] == 'orders'
    schema_payload = json.loads(schema.output)
    assert [item['qualified_name'] for item in schema_payload['models']] == ['Order', 'User']
    assert schema_payload['sessions'][0]['name'] == 'SessionLocal'


def test_incremental_update_preserves_sqlalchemy_artifacts(fresh_project):
    put(
        fresh_project,
        'models.py',
        (
            'from sqlalchemy import Column, Integer\n'
            'from sqlalchemy.orm import declarative_base\n\n'
            'Base = declarative_base()\n\n'
            'class User(Base):\n'
            '    __tablename__ = "users"\n'
            '    id = Column(Integer, primary_key=True)\n'
        ),
    )
    build_graph(fresh_project)

    put(
        fresh_project,
        'models.py',
        (
            'from sqlalchemy import Column, Integer, String\n'
            'from sqlalchemy.orm import declarative_base\n\n'
            'Base = declarative_base()\n\n'
            'class User(Base):\n'
            '    __tablename__ = "users"\n'
            '    id = Column(Integer, primary_key=True)\n'
            '    name = Column(String, nullable=False)\n'
        ),
    )
    result = incremental_update(fresh_project)
    ir = read_ir(fresh_project)

    assert result.changed
    assert any(symbol.qualified_name == 'User.__column__.name' for symbol in ir.symbols)
