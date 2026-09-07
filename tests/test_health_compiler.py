"""Tests for WO-027 Structural Complexity & Health Analyzer Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_health_compiler_computes_complexity_and_metrics(tmp_path):
    project = tmp_path / "health-project"
    init(project, name="Health Project", no_git=True)

    _write(
        project,
        "complex_module.py",
        "def evaluate(val):\n"
        "    if val > 10:\n"
        "        for i in range(val):\n"
        "            if i % 2 == 0:\n"
        "                print(i)\n"
        "    elif val < 0:\n"
        "        while val < 0:\n"
        "            val += 1\n"
        "    return val\n",
    )

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check HealthMetrics symbols and HAS_HEALTH_METRICS relations
    health_symbols = [s for s in symbols if s["kind"] == "HealthMetrics"]
    assert health_symbols
    assert "complexity=" in health_symbols[0]["signature"]
    assert any(e["relation"] == "HAS_HEALTH_METRICS" for e in edges)
