"""End-to-End Integration tests verifying the Phase 2 & 3 compiler toolchains."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_end_to_end_phase_2_and_3_compilation(tmp_path):
    # Initialize mock project
    project = tmp_path / "e2e-project"
    init(project, name="E2E Compiler Integration Project", no_git=True)

    # 1. Write codebase files with circular imports
    _write(project, "mod_a.py", "import mod_b\ndef func_a():\n    return mod_b.func_b()\n")
    _write(project, "mod_b.py", "import mod_a\ndef func_b():\n    return 2\n")

    # 2. Write dead/unreferenced module
    _write(project, "mod_dead.py", "def unreachable_code():\n    return 42\n")

    # 3. Write configured files (WO-021 Docs & WO-022 Config & WO-023 CI/CD)
    _write(project, "README.md", "# E2E Integration\nDocumenting func_a and cycles.\n")
    _write(project, "pyproject.toml", '[tool.poetry]\nname = "e2e-proj"\nversion = "0.1.0"\n')
    _write(project, "requirements.txt", "fastapi>=0.100.0\npydantic>=2.0\npytest\n")
    _write(project, ".env", "DB_URL=sqlite:///:memory:\n")
    _write(project, ".github/workflows/ci.yml", "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n")

    # 4. Write unit tests (WO-024 Test Compiler)
    _write(project, "tests/test_mod_a.py", "from mod_a import func_a\nclass TestModA:\n    def test_func_a(self):\n        assert func_a() == 2\n")

    # Run global compiler
    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # --- VERIFY PHASE 2 (Repository Intelligence) ---
    
    # Verify Documentation Compiler (WO-021)
    assert any(s["kind"] == "DocFile" and "README.md" in s["qualified_name"] for s in symbols)
    
    # Verify Configuration Compiler (WO-022)
    assert any(s["kind"] == "Dependency" and "pytest" in s["qualified_name"] for s in symbols)
    
    # Verify CI/CD Compiler (WO-023)
    assert any(s["kind"] == "Pipeline" and "ci.yml" in s["qualified_name"] for s in symbols)
    
    # Verify Test Compiler (WO-024)
    assert any(s["kind"] == "TestSuite" and "test_mod_a" in s["qualified_name"] for s in symbols)

    # --- VERIFY PHASE 3 (Engineering Intelligence) ---

    # Verify Circular Import Compiler (WO-025)
    assert any(s["kind"] == "ImportCycle" for s in symbols)
    assert any(e["relation"] == "CIRCULAR_DEPENDENCY" for e in edges)

    # Verify Dead Code & Unused Modules Inspector (WO-026)
    assert any(s["kind"] == "DeadCode" and "unreachable_code" in s["qualified_name"] for s in symbols)

    # Verify Complexity & Health Analyzer (WO-027)
    assert any(s["kind"] == "HealthMetrics" for s in symbols)

    # Verify Refactoring Impact Analyzer (WO-028)
    assert any(e["relation"] == "REFACTORING_AFFECTS" for e in edges)
