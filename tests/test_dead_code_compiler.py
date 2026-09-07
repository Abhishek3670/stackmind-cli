"""Tests for WO-026 Dead Code & Unused Module Inspector Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_dead_code_compiler_detects_unreachable_functions(tmp_path):
    project = tmp_path / "dead-project"
    init(project, name="Dead Project", no_git=True)

    _write(
        project,
        "app.py",
        "def main():\n"
        "    return active_helper()\n\n"
        "def active_helper():\n"
        "    return 42\n\n"
        "def unused_abandoned_function():\n"
        "    return 999\n",
    )

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check DeadCode symbol for unused_abandoned_function
    dead_symbols = [s for s in symbols if s["kind"] == "DeadCode"]
    assert any("unused_abandoned_function" in s["qualified_name"] for s in dead_symbols)
    assert any(e["relation"] == "IS_DEAD_CODE" for e in edges)
