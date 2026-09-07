"""Tests for the deterministic knowledge compiler frontend."""

from __future__ import annotations

import json
import shutil

import pytest

from cli.init import init
from validators.knowledge.compiler import compile_project


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "test-project"
    init(project, name="Test Project", no_git=True)
    return project


def _write(project, rel_path: str, content: str) -> None:
    path = project / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_compile_twice_is_byte_identical(fresh_project):
    _write(
        fresh_project,
        "src/app.py",
        "def later():\n"
        "    return 1\n\n"
        "def main():\n"
        "    return later()\n",
    )

    first = compile_project(fresh_project).to_json()
    second = compile_project(fresh_project).to_json()

    assert first == second


def test_ir_byte_identical_across_fresh_project_same_inputs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    init(first, name="Same", no_git=True)
    init(second, name="Same", no_git=True)
    source = (
        "from helpers import answer\n\n"
        "def main():\n"
        "    return answer()\n"
    )
    helper = "def answer():\n    return 42\n"
    _write(first, "app.py", source)
    _write(first, "helpers.py", helper)
    _write(second, "app.py", source)
    _write(second, "helpers.py", helper)

    assert compile_project(first).to_json() == compile_project(second).to_json()


def test_forward_reference_resolves_with_two_pass(fresh_project):
    _write(
        fresh_project,
        "app.py",
        "def main():\n"
        "    return later()\n\n"
        "def later():\n"
        "    return 1\n",
    )

    data = json.loads(compile_project(fresh_project).to_json())
    edges = data["edges"]

    assert any(edge["target_name"] == "later" and edge["resolution"] == "RESOLVED" for edge in edges)
    assert all(edge["target_id"] for edge in edges if edge["target_name"] == "later")


def test_cross_file_explicit_import_resolves(fresh_project):
    _write(
        fresh_project,
        "app.py",
        "from helpers import answer\n\n"
        "def main():\n"
        "    return answer()\n",
    )
    _write(fresh_project, "helpers.py", "def answer():\n    return 42\n")

    data = json.loads(compile_project(fresh_project).to_json())

    assert any(
        edge["resolution"] == "RESOLVED"
        and edge["target_name"].endswith("helpers.py:answer")
        for edge in data["edges"]
    )


def test_external_calls_are_not_linked_to_node_ids(fresh_project):
    _write(
        fresh_project,
        "app.py",
        "import json\n\n"
        "def main(value):\n"
        "    return json.dumps(value)\n",
    )

    data = json.loads(compile_project(fresh_project).to_json())
    external_edges = [edge for edge in data["edges"] if edge["resolution"] == "EXTERNAL"]

    assert external_edges
    assert external_edges[0]["target_name"] == "json.dumps"
    assert external_edges[0]["target_id"] is None


def test_unresolved_calls_are_recorded(fresh_project):
    _write(
        fresh_project,
        "app.py",
        "def main():\n"
        "    return dynamic_factory()()\n",
    )

    data = json.loads(compile_project(fresh_project).to_json())

    assert any(edge["target_name"] == "dynamic_factory" for edge in data["edges"])
    assert any(edge["resolution"] == "UNRESOLVED" for edge in data["edges"])


def test_parse_error_emits_diagnostic_and_does_not_abort(fresh_project):
    _write(fresh_project, "good.py", "def ok():\n    return 1\n")
    _write(fresh_project, "bad.py", "def broken(:\n    pass\n")

    data = json.loads(compile_project(fresh_project).to_json())

    assert any(symbol["qualified_name"] == "ok" for symbol in data["symbols"])
    assert any(diagnostic["path"] == "bad.py" for diagnostic in data["diagnostics"])
    assert any(diagnostic["code"] == "PARSE_ERROR" for diagnostic in data["diagnostics"])


def test_ir_contains_no_absolute_path_wall_clock_rng_or_pid(fresh_project):
    _write(fresh_project, "app.py", "def main():\n    return missing()\n")

    rendered = compile_project(fresh_project).to_json()
    data = json.loads(rendered)

    assert str(fresh_project) not in rendered
    assert "\\\\" not in rendered
    assert "pid" not in rendered.lower()
    assert "random" not in rendered.lower()
    assert "timestamp" not in rendered.lower()
    assert data["revision_inputs"]["git_commit"] is None


def test_registry_output_is_recreated_deterministically_after_copy(tmp_path):
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    _write(project, "app.py", "def main():\n    return 1\n")
    first = compile_project(project).to_json()

    copied = tmp_path / "copy"
    shutil.copytree(project, copied)
    second = compile_project(copied).to_json()

    assert first == second
