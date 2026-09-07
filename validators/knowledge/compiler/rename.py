"""Rename and move continuity helpers for incremental compilation."""

from __future__ import annotations

from dataclasses import dataclass

from validators.knowledge.compiler.parse import ParsedSymbol


@dataclass(frozen=True)
class HistoricalSymbol:
    """Existing live symbol state used for continuity matching."""

    node_id: str
    kind: str
    path: str
    qualified_name: str
    signature: str
    content_hash: str
    owner_qualified_name: str | None


@dataclass(frozen=True)
class AppearedSymbol:
    """A newly appeared birth key in a dirty or moved file."""

    kind: str
    path: str
    qualified_name: str
    signature: str
    content_hash: str
    owner_qualified_name: str | None

    @classmethod
    def from_parsed(cls, symbol: ParsedSymbol) -> 'AppearedSymbol':
        return cls(
            kind=symbol.kind,
            path=symbol.path,
            qualified_name=symbol.qualified_name,
            signature=symbol.signature,
            content_hash=symbol.content_hash,
            owner_qualified_name=symbol.owner_qualified_name,
        )


@dataclass(frozen=True)
class RenameMatch:
    """A high-confidence continuity match from old birth key to new birth key."""

    node_id: str
    old_path: str
    old_qualified_name: str
    new_path: str
    new_qualified_name: str


def detect_renames(
    vanished: list[HistoricalSymbol],
    appeared: list[AppearedSymbol],
) -> tuple[tuple[RenameMatch, ...], tuple[AppearedSymbol, ...]]:
    """Match vanished birth keys to appeared keys using the Phase 5 heuristic."""
    old_buckets: dict[tuple[str, str, str | None, str], list[HistoricalSymbol]] = {}
    new_buckets: dict[tuple[str, str, str | None, str], list[AppearedSymbol]] = {}

    for symbol in vanished:
        old_buckets.setdefault(_continuity_key(symbol), []).append(symbol)
    for symbol in appeared:
        new_buckets.setdefault(_continuity_key(symbol), []).append(symbol)

    matches: list[RenameMatch] = []
    unmatched: list[AppearedSymbol] = []
    matched_new: set[tuple[str, str]] = set()

    for key, new_symbols in sorted(new_buckets.items()):
        old_symbols = old_buckets.get(key, [])
        if len(old_symbols) == 1 and len(new_symbols) == 1:
            old_symbol = old_symbols[0]
            new_symbol = new_symbols[0]
            matches.append(
                RenameMatch(
                    node_id=old_symbol.node_id,
                    old_path=old_symbol.path,
                    old_qualified_name=old_symbol.qualified_name,
                    new_path=new_symbol.path,
                    new_qualified_name=new_symbol.qualified_name,
                )
            )
            matched_new.add((new_symbol.path, new_symbol.qualified_name))
            continue
        unmatched.extend(new_symbols)

    unmatched.extend(
        symbol
        for symbol in appeared
        if (symbol.path, symbol.qualified_name) not in matched_new
        and symbol not in unmatched
    )
    ordered_matches = tuple(
        sorted(matches, key=lambda item: (item.new_path, item.new_qualified_name, item.node_id))
    )
    ordered_unmatched = tuple(
        sorted(unmatched, key=lambda item: (item.path, item.qualified_name, item.kind))
    )
    return ordered_matches, ordered_unmatched


def _continuity_key(symbol: HistoricalSymbol | AppearedSymbol) -> tuple[str, str, str | None, str]:
    return (
        symbol.kind.lower(),
        symbol.content_hash,
        symbol.owner_qualified_name,
        _normalized_signature(symbol.signature),
    )


def _normalized_signature(signature: str) -> str:
    if signature.startswith('async def '):
        prefix = 'async def '
    elif signature.startswith('def '):
        prefix = 'def '
    elif signature.startswith('class '):
        return 'class <symbol>'
    else:
        return signature

    if '(' not in signature:
        return f'{prefix}<symbol>'
    _, _, remainder = signature.partition('(')
    return f'{prefix}<symbol>({remainder}'


__all__ = [
    'AppearedSymbol',
    'HistoricalSymbol',
    'RenameMatch',
    'detect_renames',
]
