"""Aggregate metrics projection over deterministic knowledge facts."""

from __future__ import annotations

from collections import Counter
from typing import Any

from validators.knowledge.compiler.ir import CompilerIR

PROJECTOR_NAME = 'metrics'
PROJECTOR_VERSION = 'metrics-1'
PROJECTOR_INPUTS = (
    'symbols.kind',
    'symbols.owner',
    'symbols.path',
    'edges.relation',
    'edges.resolution',
    'diagnostics.code',
)


def build_metrics_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]:
    """Build one deterministic metrics summary document."""
    kind_counts = Counter(symbol.kind.lower() for symbol in ir.symbols)
    relation_counts = Counter(edge.relation for edge in ir.edges)
    resolution_counts = Counter(edge.resolution for edge in ir.edges)

    modules = kind_counts.get('module', 0)
    classes = kind_counts.get('class', 0)
    functions = kind_counts.get('function', 0)
    methods = kind_counts.get('method', 0)
    edges_total = len(ir.edges)
    resolved_edges = resolution_counts.get('RESOLVED', 0)

    return {
        'summary.json': {
            'declared_inputs': list(PROJECTOR_INPUTS),
            'diagnostics': {
                'by_code': dict(
                    sorted(Counter(item.code for item in ir.diagnostics).items())
                ),
                'total': len(ir.diagnostics),
            },
            'edges': {
                'by_relation': dict(sorted(relation_counts.items())),
                'by_resolution': dict(sorted(resolution_counts.items())),
                'resolved_ratio': round(resolved_edges / edges_total, 4) if edges_total else 0.0,
                'total': edges_total,
            },
            'nodes': {
                'by_kind': dict(sorted(kind_counts.items())),
                'classes': classes,
                'function_like': functions + methods,
                'functions': functions,
                'methods': methods,
                'modules': modules,
                'paths': len({symbol.path for symbol in ir.symbols}),
                'top_level_symbols': sum(
                    1
                    for symbol in ir.symbols
                    if symbol.owner is None and symbol.kind.lower() != 'module'
                ),
                'total': len(ir.symbols),
            },
            'projector': PROJECTOR_NAME,
            'version': PROJECTOR_VERSION,
        }
    }


__all__ = [
    'PROJECTOR_INPUTS',
    'PROJECTOR_NAME',
    'PROJECTOR_VERSION',
    'build_metrics_documents',
]
