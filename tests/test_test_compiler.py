"""Tests for WO-024 Test Compiler frontend."""

from __future__ import annotations

import json
import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_test_compiler_parses_suites_cases_fixtures(tmp_path):
    project = tmp_path / "test-comp-project"
    init(project, name="Test Comp Project", no_git=True)

    test_content = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def sample_fixture():\n"
        "    return 42\n\n"
        "class TestUserFlow:\n"
        "    def test_login(self, sample_fixture):\n"
        "        assert sample_fixture == 42\n\n"
        "def test_standalone():\n"
        "    assert True\n"
    )
    _write(project, "tests/test_user.py", test_content)

    ir = compile_project(project)
    data = json.loads(ir.to_json())
    symbols = data["symbols"]
    edges = data["edges"]

    # Check TestSuite, TestCase, and TestFixture symbols
    assert any(s["kind"] == "TestSuite" and "TestUserFlow" in s["qualified_name"] for s in symbols)
    assert any(s["kind"] == "TestCase" and "test_login" in s["qualified_name"] for s in symbols)
    assert any(s["kind"] == "TestCase" and "test_standalone" in s["qualified_name"] for s in symbols)
    assert any(s["kind"] == "TestFixture" and "sample_fixture" in s["qualified_name"] for s in symbols)

    # Check relations
    assert any(e["relation"] == "SUITE_CONTAINS_CASE" for e in edges)
    assert any(e["relation"] == "TESTS_SYMBOL" for e in edges)
