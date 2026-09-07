from __future__ import annotations

import json

from cli.init import init
from cli.lock import acquire_lock, release_lock
from validators.knowledge.analysis.base import (
    AnalysisEvidence,
    AnalysisProvider,
    ObservedRelationship,
    RELATION_FLOWS_TO,
)
from validators.knowledge.analysis.normalize import normalize_observations, resolve_external_id
from validators.knowledge.compiler.ir import (
    CompilerIR,
    EdgeIR,
    RELATION_FLOWS_TO as IR_RELATION_FLOWS_TO,
    SymbolIR,
)
from validators.knowledge.registry import SymbolRegistry, birth_key
from validators.knowledge.storage import build_node_documents, node_path, read_ir


def _project(tmp_path):
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def _evidence(provider: str = "trace") -> AnalysisEvidence:
    return AnalysisEvidence(
        provider=provider,
        evidence_type="runtime_trace",
        confidence=0.82,
        run_id="run-001",
        analyzer_version="trace-1",
        metadata={"span": "s1"},
    )


def _symbol(node_id: str, qualified_name: str) -> SymbolIR:
    return SymbolIR(
        node_id=node_id,
        kind="Function",
        path="app.py",
        qualified_name=qualified_name,
        signature=f"def {qualified_name}()",
        location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
        content_hash=f"hash-{qualified_name}",
    )


def test_analysis_evidence_serializes_deterministically():
    evidence = AnalysisEvidence(
        provider="runtime",
        evidence_type="trace",
        confidence=0.9,
        run_id="run-1",
        analyzer_version="runtime-1",
        metadata={"z": 2, "a": 1},
    )

    assert evidence.to_dict() == {
        "analyzer_version": "runtime-1",
        "confidence": 0.9,
        "evidence_type": "trace",
        "metadata": {"a": 1, "z": 2},
        "provider": "runtime",
        "run_id": "run-1",
    }
    assert AnalysisEvidence.from_dict(evidence.to_dict()) == evidence


def test_flows_to_is_supported_relation_string():
    assert RELATION_FLOWS_TO == "FLOWS_TO"
    assert IR_RELATION_FLOWS_TO == "FLOWS_TO"


def test_analysis_provider_protocol_shape():
    class Provider:
        name = "static-flow"

        def analyze(self) -> list[ObservedRelationship]:
            return [
                ObservedRelationship(
                    "app.py:source",
                    "app.py:target",
                    RELATION_FLOWS_TO,
                    [_evidence()],
                )
            ]

    provider: AnalysisProvider = Provider()

    assert provider.name == "static-flow"
    assert provider.analyze()[0].edge_kind == "FLOWS_TO"


def test_observed_relationship_normalizes_to_registry_node_ids(tmp_path):
    project = _project(tmp_path)
    registry = SymbolRegistry(project, agent="codex")
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        source = registry.get_or_create(kind="Function", path="app.py", qualified_name="source")
        target = registry.get_or_create(kind="Function", path="app.py", qualified_name="target")
    finally:
        release_lock(project / ".sync", "codex")

    edges = normalize_observations(
        [
            ObservedRelationship(
                "app.py:source",
                "app.py:target",
                RELATION_FLOWS_TO,
                [_evidence()],
            )
        ],
        registry,
    )

    assert resolve_external_id(birth_key("app.py", "source"), registry) == source.node_id
    assert len(edges) == 1
    assert edges[0].source_id == source.node_id
    assert edges[0].target_id == target.node_id
    assert edges[0].relation == "FLOWS_TO"
    assert edges[0].resolution == "RESOLVED"
    assert edges[0].evidence[0].provider == "trace"


def test_evidence_merges_for_same_logical_edge(tmp_path):
    project = _project(tmp_path)
    registry = SymbolRegistry(project, agent="codex")
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        registry.get_or_create(kind="Function", path="app.py", qualified_name="source")
        registry.get_or_create(kind="Function", path="app.py", qualified_name="target")
    finally:
        release_lock(project / ".sync", "codex")

    edges = normalize_observations(
        [
            ObservedRelationship("app.py:source", "app.py:target", "FLOWS_TO", [_evidence("trace")]),
            ObservedRelationship("app.py:source", "app.py:target", "FLOWS_TO", [_evidence("static")]),
        ],
        registry,
    )

    assert len(edges) == 1
    assert [item.provider for item in edges[0].evidence] == ["trace", "static"]


def test_edgeir_with_evidence_round_trips_through_storage(tmp_path):
    project = _project(tmp_path)
    source = _symbol("FUNC-1111111111111111", "source")
    target = _symbol("FUNC-2222222222222222", "target")
    edge = EdgeIR(
        source_id=source.node_id,
        relation="FLOWS_TO",
        target_id=target.node_id,
        target_name="target",
        resolution="RESOLVED",
        confidence=0.82,
        path="app.py",
        line=12,
        evidence=[_evidence()],
    )
    ir = CompilerIR(revision_inputs={}, symbols=[source, target], edges=[edge])

    for node_id, document in build_node_documents(ir).items():
        symbol = source if node_id == source.node_id else target
        path = node_path(project, symbol.kind, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    stored = read_ir(project)
    assert stored.edges == [edge]
    assert stored.edges[0].to_dict()["evidence"][0]["provider"] == "trace"


def test_old_edges_without_evidence_still_load(tmp_path):
    project = _project(tmp_path)
    source = _symbol("FUNC-1111111111111111", "source")
    target = _symbol("FUNC-2222222222222222", "target")
    node = build_node_documents(
        CompilerIR(
            revision_inputs={},
            symbols=[source],
            edges=[
                EdgeIR(
                    source_id=source.node_id,
                    relation="CALLS",
                    target_id=target.node_id,
                    target_name="target",
                    resolution="RESOLVED",
                    confidence=1.0,
                    path="app.py",
                    line=2,
                )
            ],
        )
    )[source.node_id]
    del node["deterministic"]["outgoing"][0]["evidence"]
    path = node_path(project, source.kind, source.node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(node, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    stored = read_ir(project)

    assert len(stored.edges) == 1
    assert stored.edges[0].evidence == []
