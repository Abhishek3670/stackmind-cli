from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli.init import init
from cli.lock import acquire_lock, release_lock
from cli.main import cli
from validators.knowledge.analysis.base import AnalysisEvidence
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.compiler.ir import CompilerIR, EdgeIR, SymbolIR
from validators.knowledge.contract import AgentContract, ContractAccessDenied
from validators.knowledge.projections import build_projections
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.writer import write_knowledge


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def _symbol(node_id: str, path: str, name: str, content_hash: str) -> SymbolIR:
    return SymbolIR(
        node_id=node_id,
        kind="Function",
        path=path,
        qualified_name=name,
        signature=f"def {name}()",
        location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
        content_hash=content_hash,
    )


def _seed_graph(project: Path) -> dict[str, str]:
    registry = SymbolRegistry(project, agent="codex")
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        target = registry.get_or_create(kind="Function", path="app.py", qualified_name="target")
        static = registry.get_or_create(kind="Function", path="app.py", qualified_name="static_caller")
        runtime = registry.get_or_create(kind="Function", path="app.py", qualified_name="runtime_caller")
        flow = registry.get_or_create(kind="Function", path="app.py", qualified_name="handler")
    finally:
        release_lock(project / ".sync", "codex")
    runtime_evidence = AnalysisEvidence(
        provider="runtime-tracer",
        evidence_type="runtime-observed",
        confidence=1.0,
        run_id="run-1",
        analyzer_version="runtime-tracer-1",
        metadata={"partial": False},
    )
    flow_evidence = AnalysisEvidence(
        provider="flow-analyzer",
        evidence_type="static-flow",
        confidence=0.8,
        run_id="flow-1",
        analyzer_version="flow-analyzer-1",
        metadata={
            "intermediate_steps": ["value"],
            "origin": "request.args",
            "path_length": 3,
            "sink": "db.execute",
        },
    )
    symbols = [
        _symbol(target.node_id, "app.py", "target", "hash-target"),
        _symbol(static.node_id, "app.py", "static_caller", "hash-static"),
        _symbol(runtime.node_id, "app.py", "runtime_caller", "hash-runtime"),
        _symbol(flow.node_id, "app.py", "handler", "hash-handler"),
    ]
    edges = [
        EdgeIR(static.node_id, "CALLS", target.node_id, "target", "RESOLVED", 1.0, "app.py", 4),
        EdgeIR(
            runtime.node_id,
            "CALLS",
            target.node_id,
            "target",
            "RESOLVED",
            1.0,
            "app.py",
            8,
            evidence=[runtime_evidence],
        ),
        EdgeIR(
            flow.node_id,
            "FLOWS_TO",
            None,
            "app.py:db.execute",
            "UNRESOLVED",
            0.8,
            "app.py",
            12,
            evidence=[flow_evidence],
        ),
    ]
    write_knowledge(project, CompilerIR(revision_inputs={}, symbols=symbols, edges=edges), built_at="fixed")
    build_projections(project)
    return {
        "target": target.node_id,
        "static": static.node_id,
        "runtime": runtime.node_id,
        "flow": flow.node_id,
    }


def test_query_result_includes_evidence_and_provenance(tmp_path):
    project = _project(tmp_path)
    _seed_graph(project)

    caller = KnowledgeAPI(project).callers("target").results[0]

    assert caller.evidence
    assert caller.evidence[0].provider == "runtime-tracer"
    assert "runtime-tracer" in caller.provenance_summary
    assert caller.to_dict()["evidence"][0]["evidence_type"] == "runtime-observed"


def test_evidence_filtering_runtime_and_static(tmp_path):
    project = _project(tmp_path)
    _seed_graph(project)
    api = KnowledgeAPI(project)

    runtime = api.callers("target", evidence_type=["runtime"])
    static = api.callers("target", evidence_type=["static"])

    assert [item.qualified_name for item in runtime.results] == ["runtime_caller"]
    assert [item.qualified_name for item in static.results] == ["static_caller"]


def test_flows_returns_path_with_provenance(tmp_path):
    project = _project(tmp_path)
    _seed_graph(project)

    flows = KnowledgeAPI(project).flows("handler", "db.execute")

    assert len(flows.results) == 1
    result = flows.results[0]
    assert result.kind == "FlowPath"
    assert result.qualified_name == "app.py:db.execute"
    assert result.metadata["origin"] == "request.args"
    assert result.metadata["sink"] == "db.execute"
    assert result.metadata["why_retrieved"] == ["flow_path"]


def test_unified_context_combines_lexical_graph_and_provenance_reasons(tmp_path):
    project = _project(tmp_path)
    _seed_graph(project)

    bundle = KnowledgeAPI(project).assemble_context("target", token_budget=600, limit=5)

    payload = bundle.to_dict()
    reasons = {reason for entry in payload["entries"] for reason in entry["why_retrieved"]}
    assert "caller_relationship" in reasons
    assert "runtime_confirmed" in reasons
    assert all(entry["access_status"] == "allowed" for entry in payload["entries"])
    assert "provenance:" in bundle.text


def test_agent_contract_enforces_access_on_retrieval_paths(tmp_path):
    project = _project(tmp_path)
    ids = _seed_graph(project)
    contract = AgentContract(
        {
            "agent_id": "codex",
            "work_order": "WO-034",
            "scope": {
                "allow": [{"module": "app.target", "depth": 0}],
                "deny": [{"module": "app.runtime_caller"}],
            },
            "budget": {},
        }
    )
    api = KnowledgeAPI(project, contract=contract)

    assert ids["target"]
    try:
        result = api.callers("target", evidence_type=["runtime"])
    except ContractAccessDenied:
        result = None

    assert result is None or result.results == ()


def test_graph_callers_and_flows_cli_show_evidence(tmp_path):
    project = _project(tmp_path)
    _seed_graph(project)
    runner = CliRunner()

    callers = runner.invoke(cli, ["graph", "callers", "target", "--project", str(project)])
    flows = runner.invoke(
        cli,
        ["graph", "flows", "handler", "db.execute", "--project", str(project), "--json-output"],
    )

    assert callers.exit_code == 0
    assert "evidence: runtime-tracer/runtime-observed" in callers.output
    assert flows.exit_code == 0
    payload = json.loads(flows.output)
    assert payload["results"][0]["metadata"]["sink"] == "db.execute"
