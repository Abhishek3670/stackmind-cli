"""Reverse-edge projection for direct inbound lookups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validators.knowledge.compiler.ir import CompilerIR
from validators.knowledge.storage import node_bucket

PROJECTOR_NAME = 'reverse_index'
PROJECTOR_VERSION = 'reverse-index-1'
PROJECTOR_INPUTS = (
    'symbols.node_id',
    'edges.source_id',
    'edges.target_id',
    'edges.target_name',
    'edges.relation',
    'edges.resolution',
    'edges.confidence',
    'edges.path',
    'edges.line',
    'edges.evidence',
)


def build_reverse_index_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]:
    """Build target-sharded inbound edge documents."""
    by_target: dict[str, list[dict[str, Any]]] = {}
    for edge in sorted(
        ir.edges,
        key=lambda item: (
            item.target_id or '',
            item.source_id,
            item.relation,
            item.line,
            item.path,
            item.target_name,
            item.resolution,
        ),
    ):
        if edge.target_id is None:
            continue
        by_target.setdefault(edge.target_id, []).append(
            {
                'confidence': edge.confidence,
                'evidence': [item.to_dict() for item in edge.evidence],
                'line': edge.line,
                'path': edge.path,
                'relation': edge.relation,
                'resolution': edge.resolution,
                'source_id': edge.source_id,
                'target_name': edge.target_name,
            }
        )

    documents: dict[str, dict[str, Any]] = {
        'manifest.json': {
            'declared_inputs': list(PROJECTOR_INPUTS),
            'projector': PROJECTOR_NAME,
            'target_count': len(by_target),
            'version': PROJECTOR_VERSION,
        }
    }
    for target_id, inbound in sorted(by_target.items()):
        documents[f'{node_bucket(target_id)}/{target_id}.json'] = {
            'inbound': inbound,
            'projector': PROJECTOR_NAME,
            'target_id': target_id,
            'version': PROJECTOR_VERSION,
        }
    return documents


def reverse_index_path(project_path: Path, target_id: str) -> Path:
    """Return the shard path for one target node."""
    project_path = project_path.resolve()
    return (
        project_path
        / '.sync'
        / 'knowledge'
        / 'cache'
        / PROJECTOR_NAME
        / node_bucket(target_id)
        / f'{target_id}.json'
    )


def lookup_reverse_edges(
    project_path: Path,
    target_id: str,
    *,
    relation: str | None = None,
) -> list[dict[str, Any]]:
    """Load inbound edges for one target without scanning the node store."""
    path = reverse_index_path(project_path, target_id)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    inbound = data.get('inbound', [])
    if relation is None:
        return inbound
    return [item for item in inbound if item.get('relation') == relation]


__all__ = [
    'PROJECTOR_INPUTS',
    'PROJECTOR_NAME',
    'PROJECTOR_VERSION',
    'build_reverse_index_documents',
    'lookup_reverse_edges',
    'reverse_index_path',
]
