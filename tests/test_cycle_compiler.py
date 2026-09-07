"""Tests for WO-025 Circular Import & Cycle Detection Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_cycle_compiler_detects_circular_imports(tmp_path):
    project = tmp_path / "cycle-project"
    init(project, name="Cycle Project", no_git=True)

    _write(project, "mod_a.py", "import mod_b\ndef func_a(): return 1\n")
    _write(project, "mod_b.py", "import mod_a\ndef func_b(): return 2\n")

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check ImportCycle symbols and CIRCULAR_DEPENDENCY relations
    assert any(s["kind"] == "ImportCycle" for s in symbols)
    assert any(e["relation"] == "CIRCULAR_DEPENDENCY" for e in edges)
    assert any(e["relation"] == "PART_OF_CYCLE" for e in edges)
