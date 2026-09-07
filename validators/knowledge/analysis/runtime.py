"""Runtime call tracing provider for analysis-derived CALLS evidence."""

from __future__ import annotations

import fnmatch
import runpy
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Callable, Iterable, Sequence

from validators.knowledge.analysis.base import AnalysisEvidence, ObservedRelationship
from validators.knowledge.analysis.normalize import normalize_observations
from validators.knowledge.compiler.ir import CompilerIR, EdgeIR
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge

RELATION_CALLS = "CALLS"


@dataclass(frozen=True)
class RuntimeFrame:
    module: str
    qualname: str
    path: str
    line: int

    @property
    def external_id(self) -> str:
        return f"{self.path}:{self.qualname}"

    def to_dict(self) -> dict[str, str | int]:
        return {
            "line": self.line,
            "module": self.module,
            "path": self.path,
            "qualname": self.qualname,
        }


@dataclass
class RuntimeTraceResult:
    run_id: str
    observations: list[ObservedRelationship] = field(default_factory=list)
    quarantined: list[dict[str, str]] = field(default_factory=list)
    event_count: int = 0
    partial: bool = False


class RuntimeTracer:
    """Capture Python call relationships with `sys.setprofile`."""

    def __init__(
        self,
        project_path: Path,
        *,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        event_cap: int = 100_000,
        run_id: str | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.include = tuple(include or ())
        self.exclude = tuple(exclude or ())
        self.event_cap = event_cap
        self.run_id = run_id or uuid.uuid4().hex
        self.result = RuntimeTraceResult(run_id=self.run_id)
        self._stack: list[RuntimeFrame | None] = []
        self._previous_profile = None

    def trace_callable(self, func: Callable[[], object]) -> RuntimeTraceResult:
        """Run a callable while collecting bounded call observations."""
        self._previous_profile = sys.getprofile()
        sys.setprofile(self._profile)
        try:
            func()
        finally:
            sys.setprofile(self._previous_profile)
            self._previous_profile = None
        return self.result

    def _profile(self, frame: FrameType, event: str, arg: object) -> None:
        if event == "call":
            self._handle_call(frame)
        elif event == "return":
            self._handle_return()

    def _handle_call(self, frame: FrameType) -> None:
        if self.result.event_count >= self.event_cap:
            self.result.partial = True
            self._stack.append(None)
            return
        self.result.event_count += 1
        current = self._frame_info(frame)
        if current is None or not self._included(current):
            self._stack.append(None)
            return
        caller = next((item for item in reversed(self._stack) if item is not None), None)
        if caller is not None:
            self.result.observations.append(self._observation(caller, current))
        self._stack.append(current)

    def _handle_return(self) -> None:
        if self._stack:
            self._stack.pop()

    def _frame_info(self, frame: FrameType) -> RuntimeFrame | None:
        filename = frame.f_code.co_filename
        resolved = Path(filename).resolve()
        try:
            path = resolved.relative_to(self.project_path).as_posix()
        except ValueError:
            try:
                path = resolved.relative_to(Path.cwd().resolve()).as_posix()
            except ValueError:
                return None
        module = str(frame.f_globals.get("__name__", ""))
        qualname = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
        if qualname == "<module>":
            qualname = module
        return RuntimeFrame(module=module, qualname=qualname, path=path, line=frame.f_code.co_firstlineno)

    def _included(self, frame: RuntimeFrame) -> bool:
        haystack = (frame.module, frame.path, frame.qualname, frame.external_id)
        if self.include and not any(
            fnmatch.fnmatch(value, pattern) for pattern in self.include for value in haystack
        ):
            return False
        if self.exclude and any(
            fnmatch.fnmatch(value, pattern) for pattern in self.exclude for value in haystack
        ):
            return False
        return True

    def _observation(self, caller: RuntimeFrame, callee: RuntimeFrame) -> ObservedRelationship:
        return ObservedRelationship(
            source_external_id=caller.external_id,
            target_external_id=callee.external_id,
            edge_kind=RELATION_CALLS,
            evidence=[
                AnalysisEvidence(
                    provider="runtime-tracer",
                    evidence_type="runtime-observed",
                    confidence=1.0,
                    run_id=self.run_id,
                    analyzer_version="runtime-tracer-1",
                    metadata={
                        "callee": callee.to_dict(),
                        "caller": caller.to_dict(),
                        "partial": self.result.partial,
                    },
                )
            ],
        )


class RuntimeTracingProvider:
    """AnalysisProvider implementation backed by RuntimeTracer."""

    name = "runtime-tracer"

    def __init__(
        self,
        project_path: Path,
        command: Sequence[str] | None = None,
        *,
        target: Callable[[], object] | None = None,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        event_cap: int = 100_000,
        run_id: str | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.command = tuple(command or ())
        self.target = target
        self.include = tuple(include or ())
        self.exclude = tuple(exclude or ())
        self.event_cap = event_cap
        self.run_id = run_id
        self.last_result: RuntimeTraceResult | None = None

    def analyze(self) -> list[ObservedRelationship]:
        """Run the configured target and return runtime call observations."""
        tracer = RuntimeTracer(
            self.project_path,
            include=self.include,
            exclude=self.exclude,
            event_cap=self.event_cap,
            run_id=self.run_id,
        )
        self.last_result = tracer.trace_callable(self._runner)
        return list(self.last_result.observations)

    def _runner(self) -> None:
        if self.target is not None:
            self.target()
            return
        if not self.command:
            return
        if self.command[0] == "pytest":
            import pytest

            code = pytest.main(list(self.command[1:]))
            if code:
                raise SystemExit(code)
            return
        if self.command[0] == "python" and len(self.command) >= 3 and self.command[1] == "-m":
            original_argv = sys.argv[:]
            sys.argv = [self.command[2], *self.command[3:]]
            try:
                runpy.run_module(self.command[2], run_name="__main__", alter_sys=True)
            finally:
                sys.argv = original_argv
            return
        raise ValueError("runtime tracing supports callables, `pytest ...`, or `python -m ...`")


def normalize_runtime_observations(
    observations: Iterable[ObservedRelationship],
    registry: SymbolRegistry,
) -> list[EdgeIR]:
    """Normalize runtime observations to canonical CALLS edges."""
    return normalize_observations(observations, registry)


def merge_evidence_edges(existing: Iterable[EdgeIR], additions: Iterable[EdgeIR]) -> list[EdgeIR]:
    """Merge evidence into existing logical edges without creating duplicates."""
    merged: dict[tuple[str, str | None, str], EdgeIR] = {
        (edge.source_id, edge.target_id, edge.relation): edge for edge in existing
    }
    for edge in additions:
        key = (edge.source_id, edge.target_id, edge.relation)
        current = merged.get(key)
        if current is None:
            merged[key] = edge
            continue
        merged[key] = EdgeIR(
            source_id=current.source_id,
            relation=current.relation,
            target_id=current.target_id,
            target_name=current.target_name,
            resolution=current.resolution,
            confidence=max(current.confidence, edge.confidence),
            path=current.path,
            line=current.line,
            evidence=[*current.evidence, *edge.evidence],
        )
    return sorted(
        merged.values(),
        key=lambda edge: (edge.path, edge.line, edge.source_id, edge.relation, edge.target_name),
    )


def run_runtime_analysis(
    project_path: Path,
    command: Sequence[str],
    *,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    event_cap: int = 100_000,
    agent: str = "codex",
) -> tuple[RuntimeTraceResult, int]:
    """Trace a command, merge resolved evidence into SKC storage, and return counts."""
    project = project_path.resolve()
    provider = RuntimeTracingProvider(
        project,
        command,
        include=include,
        exclude=exclude,
        event_cap=event_cap,
    )
    observations = provider.analyze()
    result = provider.last_result or RuntimeTraceResult(run_id="")
    registry = SymbolRegistry(project, agent=agent)
    additions = normalize_runtime_observations(observations, registry)
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
    return result, len(additions)
