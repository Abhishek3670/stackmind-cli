"""Canonical IR data structures for the knowledge compiler frontend."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from validators.knowledge.analysis.base import AnalysisEvidence

ResolutionTier = Literal["RESOLVED", "EXTERNAL", "UNRESOLVED"]

IR_SCHEMA_VERSION = "1"
COMPILER_VERSION = "frontend-1"
RELATION_CALLS = "CALLS"
RELATION_FLOWS_TO = "FLOWS_TO"


@dataclass(frozen=True)
class SymbolIR:
    """A code symbol with stable registry identity."""

    node_id: str
    kind: str
    path: str
    qualified_name: str
    signature: str
    location: dict[str, int]
    content_hash: str
    owner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "kind": self.kind,
            "location": dict(sorted(self.location.items())),
            "node_id": self.node_id,
            "owner": self.owner,
            "path": self.path,
            "qualified_name": self.qualified_name,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class EdgeIR:
    """A deterministic relationship extracted from source."""

    source_id: str
    relation: str
    target_id: str | None
    target_name: str
    resolution: ResolutionTier
    confidence: float
    path: str
    line: int
    evidence: list[AnalysisEvidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        evidence = [
            item if isinstance(item, AnalysisEvidence) else AnalysisEvidence.from_dict(item)
            for item in self.evidence
        ]
        object.__setattr__(self, "evidence", evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
            "line": self.line,
            "path": self.path,
            "relation": self.relation,
            "resolution": self.resolution,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "target_name": self.target_name,
        }


@dataclass(frozen=True)
class DiagnosticIR:
    """A non-fatal compiler diagnostic."""

    path: str
    severity: str
    code: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "line": self.line,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class CompilerIR:
    """Canonical source-to-IR output."""

    revision_inputs: dict[str, str | None]
    symbols: list[SymbolIR] = field(default_factory=list)
    edges: list[EdgeIR] = field(default_factory=list)
    diagnostics: list[DiagnosticIR] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        symbols = sorted(
            (symbol.to_dict() for symbol in self.symbols),
            key=lambda item: (item["path"], item["qualified_name"], item["node_id"]),
        )
        edges = sorted(
            (edge.to_dict() for edge in self.edges),
            key=lambda item: (
                item["path"],
                item["line"],
                item["source_id"],
                item["relation"],
                item["target_name"],
                item["resolution"],
            ),
        )
        diagnostics = sorted(
            (diagnostic.to_dict() for diagnostic in self.diagnostics),
            key=lambda item: (item["path"], item["line"] or 0, item["code"], item["message"]),
        )
        return {
            "diagnostics": diagnostics,
            "edges": edges,
            "revision_inputs": dict(sorted(self.revision_inputs.items())),
            "schema_version": IR_SCHEMA_VERSION,
            "symbols": symbols,
        }

    def to_json(self) -> str:
        """Return canonical, byte-stable JSON for compile-twice comparison."""
        return json.dumps(
            self.to_dict(),
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        ) + "\n"
