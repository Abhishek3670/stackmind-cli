"""Bounded AST data-flow analysis producing FLOWS_TO observations."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from validators.knowledge.analysis.base import (
    AnalysisEvidence,
    ObservedRelationship,
    RELATION_FLOWS_TO,
)
from validators.knowledge.analysis.runtime import merge_evidence_edges
from validators.knowledge.analysis.normalize import normalize_observations
from validators.knowledge.compiler.ir import CompilerIR, EdgeIR
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge

DEFAULT_SOURCES = ("request.args", "request.form", "request.json", "os.environ")
DEFAULT_SINKS = ("db.execute", "cursor.execute", "subprocess.run", "eval", "exec")


@dataclass(frozen=True)
class FlowFinding:
    source_external_id: str
    target_external_id: str
    origin: str
    sink: str
    path: tuple[str, ...]
    confidence: float


@dataclass
class FlowAnalyzer:
    """Small, bounded data-flow analyzer for direct Python AST patterns."""

    project_path: Path
    sources: Sequence[str] = DEFAULT_SOURCES
    sinks: Sequence[str] = DEFAULT_SINKS
    max_steps: int = 12

    def analyze_paths(self, paths: Iterable[Path]) -> list[FlowFinding]:
        findings: list[FlowFinding] = []
        for path in sorted((Path(item) for item in paths), key=lambda item: item.as_posix()):
            findings.extend(self.analyze_file(path))
        return findings

    def analyze_file(self, path: Path) -> list[FlowFinding]:
        full_path = path if path.is_absolute() else self.project_path / path
        try:
            source = full_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(full_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return []
        rel_path = full_path.resolve().relative_to(self.project_path.resolve()).as_posix()
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        summaries = {node.name: _FunctionFlow(node, rel_path, self).summary() for node in functions}
        findings: list[FlowFinding] = []
        for node in functions:
            findings.extend(_FunctionFlow(node, rel_path, self, summaries=summaries).findings())
        return sorted(findings, key=lambda item: (item.source_external_id, item.target_external_id, item.sink))


class FlowAnalysisProvider:
    """AnalysisProvider implementation for bounded static FLOWS_TO analysis."""

    name = "flow-analyzer"

    def __init__(
        self,
        project_path: Path,
        paths: Iterable[Path | str],
        *,
        sources: Sequence[str] = DEFAULT_SOURCES,
        sinks: Sequence[str] = DEFAULT_SINKS,
        run_id: str = "flow-analysis",
    ) -> None:
        self.project_path = project_path.resolve()
        self.paths = tuple(Path(path) for path in paths)
        self.sources = tuple(sources)
        self.sinks = tuple(sinks)
        self.run_id = run_id

    def analyze(self) -> list[ObservedRelationship]:
        analyzer = FlowAnalyzer(self.project_path, sources=self.sources, sinks=self.sinks)
        return [_observation(item, self.run_id) for item in analyzer.analyze_paths(self.paths)]


@dataclass
class _FunctionSummary:
    return_flow: tuple[str, ...] | None = None


@dataclass
class _FunctionFlow:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    rel_path: str
    analyzer: FlowAnalyzer
    summaries: dict[str, _FunctionSummary] = field(default_factory=dict)

    def summary(self) -> _FunctionSummary:
        tainted: dict[str, tuple[str, ...]] = {}
        for statement in self.node.body:
            self._visit_statement(statement, tainted, findings=None)
            if isinstance(statement, ast.Return):
                flow = self._flow_for_expr(statement.value, tainted)
                if flow:
                    return _FunctionSummary(return_flow=flow)
        return _FunctionSummary()

    def findings(self) -> list[FlowFinding]:
        tainted: dict[str, tuple[str, ...]] = {}
        findings: list[FlowFinding] = []
        for statement in self.node.body:
            self._visit_statement(statement, tainted, findings=findings)
        return findings

    def _visit_statement(
        self,
        statement: ast.stmt,
        tainted: dict[str, tuple[str, ...]],
        *,
        findings: list[FlowFinding] | None,
    ) -> None:
        if isinstance(statement, ast.Assign):
            flow = self._flow_for_expr(statement.value, tainted)
            if flow:
                for target in statement.targets:
                    for name in _assigned_names(target):
                        tainted[name] = _bounded((*flow, name), self.analyzer.max_steps)
            self._check_call(statement.value, tainted, findings)
            return
        if isinstance(statement, ast.AnnAssign):
            flow = self._flow_for_expr(statement.value, tainted)
            if flow:
                for name in _assigned_names(statement.target):
                    tainted[name] = _bounded((*flow, name), self.analyzer.max_steps)
            self._check_call(statement.value, tainted, findings)
            return
        if isinstance(statement, ast.Expr):
            self._check_call(statement.value, tainted, findings)
            return
        if isinstance(statement, ast.Return):
            self._check_call(statement.value, tainted, findings)

    def _check_call(
        self,
        expr: ast.AST | None,
        tainted: dict[str, tuple[str, ...]],
        findings: list[FlowFinding] | None,
    ) -> None:
        if findings is None:
            return
        for call in [node for node in ast.walk(expr) if isinstance(node, ast.Call)] if expr else []:
            call_name = _expr_name(call.func)
            if not call_name:
                continue
            arg_flows = [self._flow_for_expr(arg, tainted) for arg in call.args]
            arg_flows.extend(self._flow_for_expr(keyword.value, tainted) for keyword in call.keywords)
            flows = [flow for flow in arg_flows if flow]
            if not flows:
                continue
            if _matches(call_name, self.analyzer.sinks):
                flow = _bounded((*flows[0], call_name), self.analyzer.max_steps)
                findings.append(
                    FlowFinding(
                        source_external_id=self._function_external_id,
                        target_external_id=f"{self.rel_path}:{call_name}",
                        origin=flow[0],
                        sink=call_name,
                        path=flow,
                        confidence=_confidence(flow),
                    )
                )
            elif call_name in self.summaries:
                flow = _bounded((*flows[0], call_name), self.analyzer.max_steps)
                findings.append(
                    FlowFinding(
                        source_external_id=self._function_external_id,
                        target_external_id=f"{self.rel_path}:{call_name}",
                        origin=flow[0],
                        sink=call_name,
                        path=flow,
                        confidence=_confidence(flow),
                    )
                )

    def _flow_for_expr(
        self,
        expr: ast.AST | None,
        tainted: dict[str, tuple[str, ...]],
    ) -> tuple[str, ...] | None:
        if expr is None:
            return None
        expr_name = _expr_name(expr)
        if expr_name and _matches(expr_name, self.analyzer.sources):
            return (expr_name,)
        if isinstance(expr, ast.Name) and expr.id in tainted:
            return tainted[expr.id]
        if isinstance(expr, ast.Call):
            call_name = _expr_name(expr.func)
            if call_name in self.summaries and self.summaries[call_name].return_flow is not None:
                return self.summaries[call_name].return_flow
        child_flows = [
            self._flow_for_expr(child, tainted)
            for child in ast.iter_child_nodes(expr)
            if not isinstance(child, ast.Load)
        ]
        return next((flow for flow in child_flows if flow), None)

    @property
    def _function_external_id(self) -> str:
        return f"{self.rel_path}:{self.node.name}"


def run_flow_analysis(
    project_path: Path,
    paths: Iterable[str],
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    sinks: Sequence[str] = DEFAULT_SINKS,
    agent: str = "codex",
) -> tuple[list[ObservedRelationship], int]:
    """Run static flow analysis, merge resolved evidence, and return counts."""
    project = project_path.resolve()
    provider = FlowAnalysisProvider(project, paths, sources=sources, sinks=sinks)
    observations = provider.analyze()
    registry = SymbolRegistry(project, agent=agent)
    additions = normalize_observations(observations, registry)
    current_ir = read_ir(project)
    merged_edges = merge_evidence_edges(current_ir.edges, additions)
    write_knowledge(
        project,
        CompilerIR(
            revision_inputs=current_ir.revision_inputs,
            symbols=current_ir.symbols,
            edges=merged_edges,
            diagnostics=current_ir.diagnostics,
        ),
        agent=agent,
    )
    return observations, len(additions)


def _observation(finding: FlowFinding, run_id: str) -> ObservedRelationship:
    return ObservedRelationship(
        source_external_id=finding.source_external_id,
        target_external_id=finding.target_external_id,
        edge_kind=RELATION_FLOWS_TO,
        evidence=[
            AnalysisEvidence(
                provider="flow-analyzer",
                evidence_type="static-flow",
                confidence=finding.confidence,
                run_id=run_id,
                analyzer_version="flow-analyzer-1",
                metadata={
                    "intermediate_steps": list(finding.path[1:-1]),
                    "origin": finding.origin,
                    "path_length": len(finding.path),
                    "sink": finding.sink,
                },
            )
        ],
    )


def _expr_name(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        parent = _expr_name(expr.value)
        return f"{parent}.{expr.attr}" if parent else expr.attr
    if isinstance(expr, ast.Subscript):
        return _expr_name(expr.value)
    if isinstance(expr, ast.Call):
        return _expr_name(expr.func)
    return None


def _assigned_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _assigned_names(item)]
    return []


def _matches(value: str, patterns: Sequence[str]) -> bool:
    return any(value == pattern or value.endswith(f".{pattern}") for pattern in patterns)


def _bounded(values: tuple[str, ...], max_steps: int) -> tuple[str, ...]:
    return values[:max_steps]


def _confidence(path: Sequence[str]) -> float:
    return round(max(0.5, 1.0 - (max(len(path) - 2, 0) * 0.1)), 4)
