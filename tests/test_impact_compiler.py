"""Tests for WO-028 Refactoring Impact Mapper Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_impact_compiler_traces_downstream_impacts(tmp_path):
    project = tmp_path / "impact-project"
    init(project, name="Impact Project", no_git=True)

    _write(project, "core.py", "class CoreEngine:\n    def execute(self):\n        return 1\n")
    _write(
        project,
        "app.py",
        "from core import CoreEngine\n\n"
        "def main_route():\n"
        "    engine = CoreEngine()\n"
        "    return engine.execute()\n",
    )

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check ImpactPrediction symbols and REFACTORING_AFFECTS relations
    assert any(s["kind"] == "ImpactPrediction" for s in symbols)
    assert any(e["relation"] == "REFACTORING_AFFECTS" for e in edges)
