"""Normalize provider observations into canonical SKC edges."""

from __future__ import annotations

from collections.abc import Iterable

from validators.knowledge.analysis.base import ObservedRelationship
from validators.knowledge.compiler.ir import EdgeIR
from validators.knowledge.registry import SymbolRegistry, birth_key


def resolve_external_id(external_id: str, registry: SymbolRegistry) -> str | None:
    """Resolve a provider external ID to a canonical SKC NodeID."""
    key = _external_birth_key(external_id)
    record = registry.lookup_birth_key(key)
    if record is None:
        return None
    node_id = record.get("node_id")
    return str(node_id) if isinstance(node_id, str) and node_id else None


def normalize_observations(
    observations: Iterable[ObservedRelationship],
    registry: SymbolRegistry,
) -> list[EdgeIR]:
    """Convert provider observations to canonical edges, merging duplicate facts."""
    merged: dict[tuple[str, str | None, str], EdgeIR] = {}
    for observation in observations:
        source_id = resolve_external_id(observation.source_external_id, registry)
        target_id = resolve_external_id(observation.target_external_id, registry)
        if source_id is None:
            continue

        edge = EdgeIR(
            source_id=source_id,
            relation=observation.edge_kind,
            target_id=target_id,
            target_name=observation.target_external_id,
            resolution="RESOLVED" if target_id is not None else "UNRESOLVED",
            confidence=_max_confidence(observation),
            path=_external_path(observation.source_external_id),
            line=0,
            evidence=list(observation.evidence),
        )
        key = (edge.source_id, edge.target_id, edge.relation)
        existing = merged.get(key)
        if existing is None:
            merged[key] = edge
            continue
        merged[key] = EdgeIR(
            source_id=existing.source_id,
            relation=existing.relation,
            target_id=existing.target_id,
            target_name=existing.target_name,
            resolution=existing.resolution,
            confidence=max(existing.confidence, edge.confidence),
            path=existing.path,
            line=existing.line,
            evidence=[*existing.evidence, *edge.evidence],
        )
    return sorted(
        merged.values(),
        key=lambda edge: (edge.path, edge.line, edge.source_id, edge.relation, edge.target_name),
    )


def _external_birth_key(external_id: str) -> str:
    path, separator, qualified_name = external_id.partition(":")
    if not separator:
        return external_id
    return birth_key(path, qualified_name)


def _external_path(external_id: str) -> str:
    path, separator, _qualified_name = external_id.partition(":")
    return path if separator else ""


def _max_confidence(observation: ObservedRelationship) -> float:
    if not observation.evidence:
        return 0.0
    return max(item.confidence for item in observation.evidence)
