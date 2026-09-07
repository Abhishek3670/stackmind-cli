"""Tests for WO-022 Configuration Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_config_compiler_parses_requirements_and_env(tmp_path):
    project = tmp_path / "cfg-project"
    init(project, name="Config Project", no_git=True)

    _write(
        project,
        "requirements.txt",
        "fastapi>=0.100.0\n"
        "pydantic>=2.0\n"
        "pytest\n",
    )
    _write(
        project,
        ".env.example",
        "DATABASE_URL=sqlite:///app.db\n"
        "SECRET_KEY=devsecret\n",
    )

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check dependency symbols
    dep_symbols = [s for s in symbols if s["kind"] == "Dependency"]
    assert any(s["qualified_name"] == "dep:fastapi" for s in dep_symbols)
    assert any(s["qualified_name"] == "dep:pydantic" for s in dep_symbols)

    # Check EnvVar symbols
    env_symbols = [s for s in symbols if s["kind"] == "EnvVar"]
    assert any(s["qualified_name"] == "env:DATABASE_URL" for s in env_symbols)
    assert any(s["qualified_name"] == "env:SECRET_KEY" for s in env_symbols)

    # Check edges
    assert any(e["relation"] == "CONFIG_REQUIRES_DEP" for e in edges)
    assert any(e["relation"] == "CONFIG_DEFINES_ENV" for e in edges)


def test_config_compiler_parses_dockerfile_and_compose(tmp_path):
    project = tmp_path / "docker-project"
    init(project, name="Docker Project", no_git=True)

    _write(
        project,
        "Dockerfile",
        "FROM python:3.12-slim\n"
        "ENV APP_ENV=production\n"
        "EXPOSE 8000\n",
    )
    _write(
        project,
        "docker-compose.yml",
        "version: '3.8'\n"
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "  db:\n"
        "    image: postgres:15\n",
    )

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]

    docker_services = [s for s in symbols if s["kind"] == "DockerService"]
    assert any("web" in s["qualified_name"] for s in docker_services)
    assert any("db" in s["qualified_name"] for s in docker_services)
