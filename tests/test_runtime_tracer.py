from __future__ import annotations

from pathlib import Path

import pytest

from cli.init import init
from cli.lock import acquire_lock, release_lock
from validators.knowledge.analysis.runtime import (
    RuntimeTracer,
    RuntimeTracingProvider,
    merge_evidence_edges,
    normalize_runtime_observations,
)
from validators.knowledge.compiler.ir import EdgeIR
from validators.knowledge.registry import SymbolRegistry


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def _register(registry: SymbolRegistry, project: Path, *names: str) -> list[str]:
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        return [
            registry.get_or_create(kind="Function", path="tests/test_runtime_tracer.py", qualified_name=name).node_id
            for name in names
        ]
    finally:
        release_lock(project / ".sync", "codex")


def helper_b() -> str:
    return "b"


def helper_a() -> str:
    return helper_b()


def test_simple_call_tracing_produces_calls_with_runtime_evidence(tmp_path):
    project = _project(tmp_path)
    tracer = RuntimeTracer(project, include=["*test_runtime_tracer*"], run_id="run-1")

    result = tracer.trace_callable(helper_a)

    match = next(
        item
        for item in result.observations
        if item.source_external_id.endswith(":helper_a")
        and item.target_external_id.endswith(":helper_b")
    )
    assert match.edge_kind == "CALLS"
    assert match.evidence[0].provider == "runtime-tracer"
    assert match.evidence[0].confidence == 1.0
    assert match.evidence[0].metadata["caller"]["qualname"] == "helper_a"


def test_runtime_provider_implements_analysis_protocol(tmp_path):
    project = _project(tmp_path)
    provider = RuntimeTracingProvider(
        project,
        target=helper_a,
        include=["*test_runtime_tracer*"],
        run_id="run-2",
    )

    observations = provider.analyze()

    assert provider.name == "runtime-tracer"
    assert observations
    assert provider.last_result is not None


def test_include_exclude_filtering(tmp_path):
    project = _project(tmp_path)
    included = RuntimeTracer(project, include=["*helper_a*"], run_id="run-include").trace_callable(helper_a)
    excluded = RuntimeTracer(project, exclude=["*helper_b*"], run_id="run-exclude").trace_callable(helper_a)

    assert not [
        item for item in included.observations if item.target_external_id.endswith(":helper_b")
    ]
    assert not [
        item for item in excluded.observations if item.target_external_id.endswith(":helper_b")
    ]


def test_event_cap_marks_partial_and_bounds_observations(tmp_path):
    project = _project(tmp_path)
    result = RuntimeTracer(
        project,
        include=["*test_runtime_tracer*"],
        event_cap=1,
        run_id="run-cap",
    ).trace_callable(helper_a)

    assert result.partial
    assert result.event_count == 1
    assert len(result.observations) <= 1


def test_tracer_crash_leaves_storage_to_caller(tmp_path):
    project = _project(tmp_path)

    def crashing_target() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        RuntimeTracer(project, include=["*test_runtime_tracer*"]).trace_callable(crashing_target)
    assert not (project / ".sync" / "knowledge" / "nodes").exists()


def test_unresolved_symbols_are_dropped_by_normalization(tmp_path):
    project = _project(tmp_path)
    registry = SymbolRegistry(project, agent="codex")
    result = RuntimeTracer(project, include=["*test_runtime_tracer*"], run_id="run-unresolved").trace_callable(helper_a)

    edges = normalize_runtime_observations(result.observations, registry)

    assert edges == []


def test_runtime_observations_normalize_to_registry_identities(tmp_path):
    project = _project(tmp_path)
    registry = SymbolRegistry(project, agent="codex")
    source_id, target_id = _register(registry, project, "helper_a", "helper_b")
    result = RuntimeTracer(project, include=["*test_runtime_tracer*"], run_id="run-normalize").trace_callable(helper_a)

    edges = normalize_runtime_observations(result.observations, registry)
    edge = next(item for item in edges if item.source_id == source_id and item.target_id == target_id)

    assert edge.relation == "CALLS"
    assert edge.evidence[0].provider == "runtime-tracer"


def test_runtime_evidence_merges_with_existing_static_edge():
    static = EdgeIR(
        source_id="FUNC-1111111111111111",
        relation="CALLS",
        target_id="FUNC-2222222222222222",
        target_name="target",
        resolution="RESOLVED",
        confidence=1.0,
        path="app.py",
        line=1,
    )
    runtime = EdgeIR(
        source_id=static.source_id,
        relation=static.relation,
        target_id=static.target_id,
        target_name=static.target_name,
        resolution="RESOLVED",
        confidence=1.0,
        path="app.py",
        line=10,
        evidence=[
            {
                "provider": "runtime-tracer",
                "evidence_type": "runtime-observed",
                "confidence": 1.0,
                "run_id": "run-1",
                "analyzer_version": "runtime-tracer-1",
                "metadata": {"partial": False},
            }
        ],
    )

    merged = merge_evidence_edges([static], [runtime])

    assert len(merged) == 1
    assert merged[0].line == 1
    assert merged[0].evidence[0].provider == "runtime-tracer"
