"""Tests for WO-023 CI/CD Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_cicd_compiler_parses_github_actions_workflow(tmp_path):
    project = tmp_path / "cicd-project"
    init(project, name="CICD Project", no_git=True)

    wf_content = (
        "name: CI Pipeline\n"
        "on:\n"
        "  push:\n"
        "    branches: [ main ]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Checkout code\n"
        "        uses: actions/checkout@v3\n"
        "      - name: Run Pytest\n"
        "        run: pytest tests/\n"
        "  deploy:\n"
        "    needs: test\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Deploy\n"
        "        run: echo Deploying\n"
    )
    _write(project, ".github/workflows/ci.yml", wf_content)

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check Pipeline, Job, and Step symbols
    assert any(s["kind"] == "Pipeline" for s in symbols)
    assert any(s["kind"] == "PipelineJob" and "test" in s["qualified_name"] for s in symbols)
    assert any(s["kind"] == "PipelineJob" and "deploy" in s["qualified_name"] for s in symbols)
    assert any(s["kind"] == "PipelineStep" for s in symbols)

    # Check job dependency and pipeline containment edges
    assert any(e["relation"] == "PIPELINE_CONTAINS_JOB" for e in edges)
    assert any(e["relation"] == "JOB_DEPENDS_ON_JOB" for e in edges)
    assert any(e["relation"] == "STEP_RUNS_COMMAND" and e["target_name"] == "pytest" for e in edges)
