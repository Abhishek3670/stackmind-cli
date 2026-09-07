"""Deterministic persistence helpers for the compiled knowledge IR."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validators.knowledge.compiler.ir import CompilerIR, DiagnosticIR, EdgeIR, SymbolIR

KNOWLEDGE_SCHEMA_VERSION = "knowledge-1"
REVISION_ID_WIDTH = 10


def canonical_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def knowledge_root(project_path: Path) -> Path:
    return project_path / ".sync" / "knowledge"


def node_bucket(node_id: str) -> str:
    return node_id.split("-", 1)[1][:2]


def node_path(project_path: Path, kind: str, node_id: str) -> Path:
    return knowledge_root(project_path) / "nodes" / kind / node_bucket(node_id) / f"{node_id}.json"


def revision_path(project_path: Path, number: int) -> Path:
    return knowledge_root(project_path) / "revisions" / f"REV-{number:0{REVISION_ID_WIDTH}d}.json"


def latest_revision_id(project_path: Path) -> int:
    revisions = knowledge_root(project_path) / "revisions"
    values = []
    for path in revisions.glob("REV-*.json") if revisions.exists() else []:
        try:
            values.append(int(path.stem.split("-", 1)[1]))
        except (IndexError, ValueError):
            pass
    return max(values, default=0)


def _edge(edge: EdgeIR) -> dict[str, Any]:
    """Serialize an edge, including optional evidence arrays."""
    return edge.to_dict()


def symbol_document(symbol: SymbolIR, edges: list[EdgeIR]) -> dict[str, Any]:
    outgoing = [_edge(edge) for edge in edges if edge.source_id == symbol.node_id]
    return {
        "ai": {},
        "deterministic": {
            "content_hash": symbol.content_hash,
            "location": dict(sorted(symbol.location.items())),
            "outgoing": outgoing,
            "owner": symbol.owner,
            "path": symbol.path,
            "qualified_name": symbol.qualified_name,
            "signature": symbol.signature,
        },
        "kind": symbol.kind,
        "node_id": symbol.node_id,
        "status": "active",
    }


def build_node_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]:
    return {symbol.node_id: symbol_document(symbol, ir.edges) for symbol in ir.symbols}


def build_revision_document(
    ir: CompilerIR, number: int, parent: int | None, built_at: str
) -> dict[str, Any]:
    return {
        "built_at": built_at,
        "diagnostics": [item.to_dict() for item in ir.diagnostics],
        "id": number,
        "parent": parent,
        "revision_inputs": dict(sorted(ir.revision_inputs.items())),
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
    }


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_ir(project_path: Path) -> CompilerIR:
    root = knowledge_root(project_path)
    symbols: list[SymbolIR] = []
    edges: list[EdgeIR] = []
    for path in sorted(root.glob("nodes/*/*/*.json")):
        node = _load(path)
        data = node["deterministic"]
        symbols.append(
            SymbolIR(
                node_id=node["node_id"],
                kind=node["kind"],
                path=data["path"],
                qualified_name=data["qualified_name"],
                signature=data["signature"],
                location=data["location"],
                content_hash=data["content_hash"],
                owner=data.get("owner"),
            )
        )
        edges.extend(EdgeIR(**edge) for edge in data.get("outgoing", []))
    revision = (
        _load(revision_path(project_path, latest_revision_id(project_path)))
        if latest_revision_id(project_path)
        else {}
    )
    return CompilerIR(
        revision_inputs=revision.get("revision_inputs", {}),
        symbols=symbols,
        edges=edges,
        diagnostics=[DiagnosticIR(**item) for item in revision.get("diagnostics", [])],
    )
