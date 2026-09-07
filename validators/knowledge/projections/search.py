"""Text-search projection over deterministic symbol facts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from validators.knowledge.compiler.ir import CompilerIR, SymbolIR
from validators.knowledge.storage import node_bucket

PROJECTOR_NAME = 'search'
PROJECTOR_VERSION = 'search-1'
PROJECTOR_INPUTS = (
    'symbols.node_id',
    'symbols.kind',
    'symbols.path',
    'symbols.qualified_name',
    'symbols.signature',
)

_TOKEN_RE = re.compile(r'[A-Za-z0-9_]+')
_CAMEL_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')


def build_search_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]:
    """Build postings and per-symbol search documents."""
    postings: dict[str, list[str]] = {}
    documents: dict[str, dict[str, Any]] = {}

    for symbol in sorted(
        ir.symbols,
        key=lambda item: (item.path, item.qualified_name, item.node_id),
    ):
        tokens = list(_tokens_for_symbol(symbol))
        documents[f'documents/{node_bucket(symbol.node_id)}/{symbol.node_id}.json'] = {
            'kind': symbol.kind,
            'node_id': symbol.node_id,
            'path': symbol.path,
            'projector': PROJECTOR_NAME,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
            'tokens': tokens,
            'version': PROJECTOR_VERSION,
        }
        for token in tokens:
            postings.setdefault(token, []).append(symbol.node_id)

    documents['index.json'] = {
        'declared_inputs': list(PROJECTOR_INPUTS),
        'document_count': len(ir.symbols),
        'postings': {
            token: sorted(node_ids) for token, node_ids in sorted(postings.items())
        },
        'projector': PROJECTOR_NAME,
        'token_count': len(postings),
        'version': PROJECTOR_VERSION,
    }
    return documents


def search_root(project_path: Path) -> Path:
    return project_path.resolve() / '.sync' / 'knowledge' / 'cache' / PROJECTOR_NAME


def search_document_path(project_path: Path, node_id: str) -> Path:
    return search_root(project_path) / 'documents' / node_bucket(node_id) / f'{node_id}.json'


def search_symbols(project_path: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Search the disposable index and return ranked symbol documents."""
    index_path = search_root(project_path) / 'index.json'
    if not index_path.exists():
        return []

    data = json.loads(index_path.read_text(encoding='utf-8'))
    postings = data.get('postings', {})
    matches: dict[str, int] = {}
    for token in _tokens_from_text(query):
        for node_id in postings.get(token, []):
            matches[node_id] = matches.get(node_id, 0) + 1

    ranked = sorted(matches.items(), key=lambda item: (-item[1], item[0]))
    results: list[dict[str, Any]] = []
    for node_id, score in ranked[:limit]:
        path = search_document_path(project_path, node_id)
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding='utf-8'))
        results.append({**document, 'score': score})
    return results


def _tokens_for_symbol(symbol: SymbolIR) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(
                _tokens_from_text(
                    ' '.join(
                        [
                            symbol.kind,
                            symbol.path,
                            symbol.qualified_name,
                            symbol.signature,
                        ]
                    )
                )
            )
        )
    )


def _tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        for chunk in raw.split('_'):
            expanded = _CAMEL_RE.sub(' ', chunk)
            for part in expanded.split():
                lowered = part.lower()
                if lowered:
                    tokens.append(lowered)
        raw_lower = raw.lower()
        if raw_lower and raw_lower not in tokens:
            tokens.append(raw_lower)
    return tokens


__all__ = [
    'PROJECTOR_INPUTS',
    'PROJECTOR_NAME',
    'PROJECTOR_VERSION',
    'build_search_documents',
    'search_document_path',
    'search_root',
    'search_symbols',
]
