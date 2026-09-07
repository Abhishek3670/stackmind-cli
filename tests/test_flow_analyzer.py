from __future__ import annotations

from pathlib import Path

from cli.init import init
from cli.lock import acquire_lock, release_lock
from validators.knowledge.analysis.flow import FlowAnalysisProvider, FlowAnalyzer
from validators.knowledge.analysis.normalize import normalize_observations
from validators.knowledge.registry import SymbolRegistry


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def _write(project: Path, text: str) -> Path:
    path = project / "app.py"
    path.write_text(text, encoding="utf-8")
    return path


def _register(registry: SymbolRegistry, project: Path, *names: str) -> None:
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        for name in names:
            registry.get_or_create(kind="Function", path="app.py", qualified_name=name)
    finally:
        release_lock(project / ".sync", "codex")


def test_simple_source_to_sink_flow_produces_observation(tmp_path):
    project = _project(tmp_path)
    path = _write(project, "def handler(request, db):\n    value = request.args\n    db.execute(value)\n")

    findings = FlowAnalyzer(project).analyze_file(path)

    assert len(findings) == 1
    assert findings[0].source_external_id == "app.py:handler"
    assert findings[0].target_external_id == "app.py:db.execute"
    assert findings[0].sink == "db.execute"


def test_multi_step_path_and_confidence_are_recorded(tmp_path):
    project = _project(tmp_path)
    path = _write(
        project,
        "def handler(request, db):\n"
        "    raw = request.args\n"
        "    cleaned = raw\n"
        "    final = cleaned\n"
        "    db.execute(final)\n",
    )

    finding = FlowAnalyzer(project).analyze_file(path)[0]

    assert finding.path == ("request.args", "raw", "cleaned", "final", "db.execute")
    assert finding.confidence == 0.7


def test_unknown_sources_and_sinks_are_ignored(tmp_path):
    project = _project(tmp_path)
    path = _write(project, "def handler(request, db):\n    value = request.cookies\n    logger.info(value)\n")

    assert FlowAnalyzer(project).analyze_file(path) == []


def test_no_false_positive_on_unrelated_assignment(tmp_path):
    project = _project(tmp_path)
    path = _write(project, "def handler(db):\n    value = 'constant'\n    db.execute(value)\n")

    assert FlowAnalyzer(project).analyze_file(path) == []


def test_cross_function_argument_flow_is_captured(tmp_path):
    project = _project(tmp_path)
    path = _write(
        project,
        "def sanitize(value):\n"
        "    return value\n\n"
        "def handler(request):\n"
        "    raw = request.args\n"
        "    return sanitize(raw)\n",
    )

    finding = FlowAnalyzer(project).analyze_file(path)[0]

    assert finding.target_external_id == "app.py:sanitize"
    assert finding.path == ("request.args", "raw", "sanitize")


def test_provider_emits_flows_to_with_complete_metadata(tmp_path):
    project = _project(tmp_path)
    _write(project, "def handler(request, db):\n    value = request.args\n    db.execute(value)\n")
    provider = FlowAnalysisProvider(project, ["app.py"], run_id="flow-run")

    observation = provider.analyze()[0]
    evidence = observation.evidence[0]

    assert observation.edge_kind == "FLOWS_TO"
    assert evidence.provider == "flow-analyzer"
    assert evidence.evidence_type == "static-flow"
    assert evidence.run_id == "flow-run"
    assert evidence.metadata["origin"] == "request.args"
    assert evidence.metadata["sink"] == "db.execute"
    assert evidence.metadata["path_length"] == 3


def test_flow_observations_normalize_to_edgeir(tmp_path):
    project = _project(tmp_path)
    _write(project, "def handler(request, db):\n    value = request.args\n    db.execute(value)\n")
    registry = SymbolRegistry(project, agent="codex")
    _register(registry, project, "handler")

    observations = FlowAnalysisProvider(project, ["app.py"], run_id="flow-run").analyze()
    edges = normalize_observations(observations, registry)

    assert len(edges) == 1
    assert edges[0].relation == "FLOWS_TO"
    assert edges[0].target_id is None
    assert edges[0].resolution == "UNRESOLVED"
    assert edges[0].evidence[0].metadata["intermediate_steps"] == ["value"]
