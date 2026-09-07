from __future__ import annotations

import json
import os

import pytest

from cli.init import init
from cli.validate import validate
from validators.knowledge.compiler import compile_project
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def put(project, name, text):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_round_trip_and_deterministic_nodes(fresh_project):
    put(fresh_project, "app.py", "def later():\n    return 1\n\ndef main():\n    return later()\n")
    ir = compile_project(fresh_project)
    first = write_knowledge(fresh_project, ir, built_at="fixed")
    stored = read_ir(fresh_project)
    assert stored.to_json() == ir.to_json()
    before = {path: path.read_bytes() for path in first.written_paths if "nodes" in str(path)}
    second = write_knowledge(fresh_project, ir, built_at="fixed")
    assert not [path for path in second.written_paths if "nodes" in str(path)]
    assert before == {path: path.read_bytes() for path in before}


def test_one_symbol_change_rewrites_only_that_node(fresh_project):
    put(fresh_project, "app.py", "def one():\n    return 1\n\ndef two():\n    return 2\n")
    first = write_knowledge(fresh_project, compile_project(fresh_project), built_at="fixed")
    put(fresh_project, "app.py", "def one():\n    return 10\n\ndef two():\n    return 2\n")
    second = write_knowledge(fresh_project, compile_project(fresh_project), built_at="fixed")
    node_writes = [path for path in second.written_paths if "nodes" in str(path)]
    assert len([path for path in node_writes if "/function/" in path.as_posix().lower()]) == 1
    assert len(node_writes) == 2
    assert second.revision_id == first.revision_id + 1


def test_atomic_failure_preserves_original(fresh_project, monkeypatch):
    put(fresh_project, "app.py", "def one():\n    return 1\n")
    first = write_knowledge(fresh_project, compile_project(fresh_project))
    node = next(path for path in first.written_paths if "nodes" in str(path))
    before = node.read_bytes()
    put(fresh_project, "app.py", "def one():\n    return 2\n")
    monkeypatch.setattr(
        os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("fail"))
    )
    with pytest.raises(OSError):
        write_knowledge(fresh_project, compile_project(fresh_project))
    assert node.read_bytes() == before


def test_layer5_detects_dangling_edge(fresh_project):
    put(fresh_project, "app.py", "def later():\n    return 1\n\ndef main():\n    return later()\n")
    write_knowledge(fresh_project, compile_project(fresh_project))

    nodes = sorted((fresh_project / ".sync" / "knowledge" / "nodes").glob("*/*/*.json"))
    data = json.loads(nodes[-1].read_text(encoding="utf-8"))
    if data["deterministic"]["outgoing"]:
        data["deterministic"]["outgoing"][0]["target_id"] = "FUNC-0000000000000000"
        nodes[-1].write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        assert any(
            "Dangling resolved edge" in issue.message for issue in validate(fresh_project).issues
        )
