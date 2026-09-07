"""Read-only Knowledge API over deterministic and AI-derived graph artifacts."""

from __future__ import annotations

import json
import math
import subprocess
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from validators.knowledge.analysis.base import AnalysisEvidence
from validators.knowledge.enricher import (
    EmbeddingBackend,
    EmbeddingRequest,
    embedding_cache_path,
    is_ai_stale,
)
from validators.knowledge.projections.reverse_index import lookup_reverse_edges
from validators.knowledge.projections.search import search_symbols
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.storage import latest_revision_id, node_path, revision_path
from validators.knowledge.contract import AgentContract, ContractAccessDenied, ContractExpiredError


@dataclass(frozen=True)
class KnowledgeResult:
    """One knowledge lookup/search/traversal hit."""

    node_id: str
    kind: str
    path: str
    qualified_name: str
    signature: str
    confidence: float
    summary: str | None = None
    alias_matched: bool = False
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[AnalysisEvidence, ...] = ()
    provenance_summary: str = ''

    def to_dict(self) -> dict[str, Any]:
        payload = {
            'alias_matched': self.alias_matched,
            'confidence': self.confidence,
            'evidence': [item.to_dict() for item in self.evidence],
            'kind': self.kind,
            'metadata': dict(sorted(self.metadata.items())),
            'node_id': self.node_id,
            'path': self.path,
            'provenance_summary': self.provenance_summary,
            'qualified_name': self.qualified_name,
            'reason': self.reason,
            'signature': self.signature,
            'summary': self.summary,
        }
        return payload


@dataclass(frozen=True)
class KnowledgeEnvelope:
    """Response envelope stamped with revision metadata and freshness flags."""

    revision: int
    git_commit: str | None
    stale: bool
    semantic: bool
    results: tuple[KnowledgeResult, ...]
    truncated: bool = False
    truncation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'git_commit': self.git_commit,
            'results': [item.to_dict() for item in self.results],
            'revision': self.revision,
            'semantic': self.semantic,
            'stale': self.stale,
            'truncated': self.truncated,
            'truncation_reason': self.truncation_reason,
        }


@dataclass(frozen=True)
class ContextEntry:
    """One bounded context snippet for an agent prompt."""

    node_id: str
    rank: int
    confidence: float
    text: str
    reason: str
    why_retrieved: tuple[str, ...] = ()
    access_status: str = 'allowed'

    def to_dict(self) -> dict[str, Any]:
        return {
            'access_status': self.access_status,
            'confidence': self.confidence,
            'node_id': self.node_id,
            'rank': self.rank,
            'reason': self.reason,
            'text': self.text,
            'why_retrieved': list(self.why_retrieved),
        }


@dataclass(frozen=True)
class ContextBundle:
    """Prompt-ready context bundle assembled from graph hits."""

    revision: int
    git_commit: str | None
    stale: bool
    semantic: bool
    token_budget: int
    estimated_tokens: int
    truncated: bool
    truncation_reason: str | None
    entries: tuple[ContextEntry, ...]
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            'entries': [item.to_dict() for item in self.entries],
            'estimated_tokens': self.estimated_tokens,
            'git_commit': self.git_commit,
            'revision': self.revision,
            'semantic': self.semantic,
            'stale': self.stale,
            'text': self.text,
            'token_budget': self.token_budget,
            'truncated': self.truncated,
            'truncation_reason': self.truncation_reason,
        }


class KnowledgeAPI:
    """Read-only query surface for compiled StackMind knowledge."""

    def __init__(
        self,
        project_path: Path,
        contract: AgentContract | str | Path | None = None,
        embedding_backend: EmbeddingBackend | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.registry = SymbolRegistry(self.project_path)
        if contract is not None and not isinstance(contract, AgentContract):
            self.contract = AgentContract.load(contract, self.project_path)
        else:
            self.contract = contract
        self.embedding_backend = embedding_backend
        self._cached_ir = None

    @property
    def ir(self):
        if self._cached_ir is None:
            from validators.knowledge.storage import read_ir
            self._cached_ir = read_ir(self.project_path)
        return self._cached_ir

    def _resolve_contract(self, contract: AgentContract | str | Path | None) -> AgentContract | None:
        if contract is None:
            return self.contract
        if isinstance(contract, AgentContract):
            return contract
        return AgentContract.load(contract, self.project_path)

    def _enforce_node(self, node_id: str, contract: AgentContract | str | Path | None = None) -> None:
        active_contract = self._resolve_contract(contract)
        if active_contract:
            if active_contract.is_expired():
                raise ContractExpiredError(f"Contract {active_contract.work_order} has expired")
            if not active_contract.is_node_in_scope(node_id, self.ir):
                raise ContractAccessDenied(
                    f"Access to node {node_id} is denied by contract {active_contract.work_order}"
                )

    def _check_expiration(self, contract: AgentContract | str | Path | None = None) -> None:
        active_contract = self._resolve_contract(contract)
        if active_contract and active_contract.is_expired():
            raise ContractExpiredError(f"Contract {active_contract.work_order} has expired")

    def lookup(
        self,
        needle: str,
        *,
        limit: int = 10,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Resolve one symbol by NodeID, birth key, current name, or alias."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        candidates: list[tuple[int, str, KnowledgeResult]] = []
        for record in self._active_records():
            rank, confidence, alias_matched = _lookup_rank(record, needle)
            if rank < 0:
                continue
            result = self._result_from_record(
                record,
                confidence=confidence,
                alias_matched=alias_matched,
                reason='lookup',
                contract=contract,
            )
            candidates.append((rank, result.qualified_name.lower(), result))
        ordered = tuple(item[2] for item in sorted(candidates)[:limit])
        return KnowledgeEnvelope(
            revision=revision,
            git_commit=git_commit,
            stale=stale,
            semantic=False,
            results=ordered,
        )

    def filter(
        self,
        *,
        query: str = '',
        kind: str | None = None,
        path_contains: str | None = None,
        qualified_name_contains: str | None = None,
        limit: int = 10,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Filter active nodes by deterministic attributes without mutating state."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        lowered_kind = (kind or '').lower()
        lowered_query = query.lower()
        lowered_path = (path_contains or '').lower()
        lowered_name = (qualified_name_contains or '').lower()
        results: list[KnowledgeResult] = []

        for record in self._active_records():
            node = self._load_node_for_record(record, contract=contract)
            if node is None:
                continue
            if lowered_kind and str(node.get('kind', '')).lower() != lowered_kind:
                continue
            deterministic = node.get('deterministic', {})
            path_value = str(deterministic.get('path', ''))
            qualified_name = str(deterministic.get('qualified_name', ''))
            signature = str(deterministic.get('signature', ''))
            if lowered_path and lowered_path not in path_value.lower():
                continue
            if lowered_name and lowered_name not in qualified_name.lower():
                continue
            haystack = ' '.join([str(node.get('kind', '')), path_value, qualified_name, signature])
            if lowered_query and lowered_query not in haystack.lower():
                continue
            results.append(
                self._result_from_node(
                    node,
                    confidence=1.0,
                    reason='filter',
                    contract=contract,
                )
            )

        ordered = tuple(
            sorted(results, key=lambda item: (item.path, item.qualified_name, item.node_id))[:limit]
        )
        return KnowledgeEnvelope(
            revision=revision,
            git_commit=git_commit,
            stale=stale,
            semantic=False,
            results=ordered,
            truncated=len(results) > limit,
            truncation_reason='filter result limit reached' if len(results) > limit else None,
        )

    def traverse(
        self,
        target: str,
        *,
        relation: str | None = None,
        direction: str = 'outbound',
        depth: int = 1,
        limit: int = 25,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Traverse inbound or outbound graph edges without reading source files."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        seed = self._resolve_one(target, contract=contract)
        if seed is None:
            return KnowledgeEnvelope(
                revision=revision,
                git_commit=git_commit,
                stale=stale,
                semantic=False,
                results=(),
            )

        results_by_node: dict[str, tuple[int, KnowledgeResult]] = {}
        queue: list[tuple[str, int]] = [(seed.node_id, 0)]
        seen_depths = {seed.node_id: 0}
        while queue:
            current_id, current_depth = queue.pop(0)
            if current_depth >= depth:
                continue
            for edge in self._edges_for(current_id, relation=relation, direction=direction, contract=contract):
                neighbor_id = edge['neighbor_id']
                next_depth = current_depth + 1
                known_depth = seen_depths.get(neighbor_id)
                if known_depth is not None and known_depth <= next_depth:
                    continue
                neighbor = self._lookup_node_by_id(neighbor_id, contract=contract)
                if neighbor is None:
                    continue
                seen_depths[neighbor_id] = next_depth
                queue.append((neighbor_id, next_depth))
                result = self._result_from_node(
                    neighbor,
                    confidence=float(edge.get('confidence', 0.0) or 0.0),
                    reason='traversal',
                    metadata={
                        'depth': next_depth,
                        'direction': direction,
                        'edge_path': edge.get('path'),
                        'line': edge.get('line'),
                        'relation': edge.get('relation'),
                        'resolution': edge.get('resolution'),
                        'source_id': edge.get('source_id'),
                        'target_name': edge.get('target_name'),
                        'why_retrieved': _why_for_edge(edge),
                    },
                    evidence=edge.get('evidence', ()),
                    contract=contract,
                )
                results_by_node[neighbor_id] = (next_depth, result)

        ordered = tuple(
            item[1]
            for item in sorted(
                results_by_node.values(),
                key=lambda entry: (
                    entry[0],
                    entry[1].path,
                    entry[1].qualified_name,
                    entry[1].node_id,
                ),
            )[:limit]
        )
        return KnowledgeEnvelope(
            revision=revision,
            git_commit=git_commit,
            stale=stale,
            semantic=False,
            results=ordered,
            truncated=len(results_by_node) > limit,
            truncation_reason='traversal result limit reached'
            if len(results_by_node) > limit
            else None,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        query_embedding: Sequence[float] | None = None,
        embedding_backend: EmbeddingBackend | None = None,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Search by embedding similarity when available, else text-search fallback."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        if query_embedding is None:
            backend = embedding_backend or self.embedding_backend
            if backend is not None:
                try:
                    query_embedding = backend.embed(
                        EmbeddingRequest(
                            node_id='query',
                            content_hash=_query_hash(query),
                            privacy_mode='query',
                            text=query,
                        )
                    ).vector
                except (ImportError, RuntimeError, ValueError):
                    query_embedding = None
        if query_embedding is not None:
            semantic_results = self._semantic_search(query_embedding, limit=limit, contract=contract)
            if semantic_results:
                return KnowledgeEnvelope(
                    revision=revision,
                    git_commit=git_commit,
                    stale=stale,
                    semantic=True,
                    results=semantic_results,
                )

        text_hits = search_symbols(self.project_path, query, limit=limit)
        results = []
        max_score = max((int(item.get('score', 0)) for item in text_hits), default=1)
        for item in text_hits:
            node = self._lookup_node_by_id(str(item['node_id']), contract=contract)
            if node is None:
                continue
            score = int(item.get('score', 0))
            confidence = round(0.45 + (0.5 * score / max_score), 4)
            results.append(
                self._result_from_node(
                    node,
                    confidence=confidence,
                    reason='text-search',
                    metadata={'why_retrieved': ['lexical_match']},
                    contract=contract,
                )
            )
        return KnowledgeEnvelope(
            revision=revision,
            git_commit=git_commit,
            stale=stale,
            semantic=False,
            results=tuple(results),
        )

    def callers(
        self,
        target: str,
        *,
        limit: int = 25,
        evidence_type: Sequence[str] | None = None,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Return direct callers of a symbol."""
        envelope = self.traverse(
            target,
            relation='CALLS',
            direction='inbound',
            depth=1,
            limit=limit,
            contract=contract,
        )
        if not evidence_type:
            return envelope
        filtered = tuple(
            result for result in envelope.results
            if _evidence_matches(result.evidence, evidence_type)
        )
        return KnowledgeEnvelope(
            revision=envelope.revision,
            git_commit=envelope.git_commit,
            stale=envelope.stale,
            semantic=envelope.semantic,
            results=filtered,
            truncated=envelope.truncated,
            truncation_reason=envelope.truncation_reason,
        )

    def flows(
        self,
        source: str,
        sink: str | None = None,
        *,
        limit: int = 25,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Return observed FLOWS_TO paths from a source symbol to optional sink."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        seed = self._resolve_one(source, contract=contract)
        if seed is None:
            return KnowledgeEnvelope(revision, git_commit, stale, False, ())

        node = self._lookup_node_by_id(seed.node_id, contract=contract)
        if node is None:
            return KnowledgeEnvelope(revision, git_commit, stale, False, ())
        results: list[KnowledgeResult] = []
        for edge in node.get('deterministic', {}).get('outgoing', []):
            if edge.get('relation') != 'FLOWS_TO':
                continue
            target_name = str(edge.get('target_name', ''))
            if sink and sink.lower() not in target_name.lower():
                continue
            evidence = _evidence_from_edge(edge)
            result_node = (
                self._lookup_node_by_id(str(edge.get('target_id')), contract=contract)
                if edge.get('target_id')
                else None
            )
            metadata = {
                'line': edge.get('line'),
                'relation': 'FLOWS_TO',
                'resolution': edge.get('resolution'),
                'source_id': edge.get('source_id'),
                'target_name': target_name,
                'why_retrieved': ['flow_path'],
            }
            if evidence:
                metadata.update(evidence[0].metadata)
            if result_node is not None:
                results.append(
                    self._result_from_node(
                        result_node,
                        confidence=float(edge.get('confidence', 0.0) or 0.0),
                        reason='flow_path',
                        metadata=metadata,
                        evidence=evidence,
                        contract=contract,
                    )
                )
            else:
                results.append(
                    KnowledgeResult(
                        node_id=str(edge.get('target_id') or target_name),
                        kind='FlowPath',
                        path=str(edge.get('path', '')),
                        qualified_name=target_name,
                        signature='',
                        confidence=round(float(edge.get('confidence', 0.0) or 0.0), 4),
                        reason='flow_path',
                        metadata=metadata,
                        evidence=evidence,
                        provenance_summary=_provenance_summary(evidence),
                    )
                )
        ordered = tuple(
            sorted(results, key=lambda item: (-item.confidence, item.path, item.qualified_name))[:limit]
        )
        return KnowledgeEnvelope(revision, git_commit, stale, False, ordered)

    def impact(
        self,
        target: str,
        *,
        depth: int = 3,
        limit: int = 50,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeEnvelope:
        """Return transitive inbound callers impacted by changing a symbol."""
        return self.traverse(
            target,
            relation='CALLS',
            direction='inbound',
            depth=depth,
            limit=limit,
            contract=contract,
        )

    def explain(
        self,
        target: str,
        *,
        contract: AgentContract | str | Path | None = None,
    ) -> dict[str, Any]:
        """Explain one symbol using deterministic facts and optional AI summary."""
        self._check_expiration(contract)
        revision, git_commit = self._revision_meta()
        stale = self._is_stale()
        subject = self._resolve_one(target, contract=contract)
        if subject is None:
            return {
                'git_commit': git_commit,
                'node': None,
                'outbound': [],
                'inbound': [],
                'revision': revision,
                'stale': stale,
            }

        outbound = self.traverse(
            subject.node_id,
            relation='CALLS',
            direction='outbound',
            depth=1,
            limit=10,
            contract=contract,
        )
        inbound = self.traverse(
            subject.node_id,
            relation='CALLS',
            direction='inbound',
            depth=1,
            limit=10,
            contract=contract,
        )
        return {
            'git_commit': git_commit,
            'inbound': [item.to_dict() for item in inbound.results],
            'node': subject.to_dict(),
            'outbound': [item.to_dict() for item in outbound.results],
            'revision': revision,
            'stale': stale,
        }

    def assemble_context(
        self,
        query: str,
        *,
        token_budget: int = 1200,
        limit: int = 8,
        query_embedding: Sequence[float] | None = None,
        contract: AgentContract | str | Path | None = None,
        include_skills: bool = True,
    ) -> ContextBundle:
        """Assemble a bounded context bundle for agent prompts, including verified active procedural skills."""
        self._check_expiration(contract)
        seed_hits = self.lookup(query, limit=max(1, limit), contract=contract)
        if seed_hits.results:
            seed_envelope = seed_hits
        else:
            seed_envelope = self.search(
                query,
                limit=max(3, limit),
                query_embedding=query_embedding,
                contract=contract,
            )

        ranked: dict[str, tuple[int, KnowledgeResult]] = {}
        for index, result in enumerate(seed_envelope.results[: max(1, min(limit, 3))]):
            ranked[result.node_id] = (100 - (index * 5), result)

            inbound = self.callers(result.node_id, limit=3, contract=contract)
            for neighbor in inbound.results:
                score = 80 - (10 * int(neighbor.metadata.get('depth', 1))) + _evidence_boost(neighbor.evidence)
                existing = ranked.get(neighbor.node_id)
                if existing is None or score > existing[0]:
                    ranked[neighbor.node_id] = (score, neighbor)

            outbound = self.traverse(
                result.node_id,
                relation='CALLS',
                direction='outbound',
                depth=1,
                limit=3,
                contract=contract,
            )
            for neighbor in outbound.results:
                score = 70 - (10 * int(neighbor.metadata.get('depth', 1))) + _evidence_boost(neighbor.evidence)
                existing = ranked.get(neighbor.node_id)
                if existing is None or score > existing[0]:
                    ranked[neighbor.node_id] = (score, neighbor)

        entries: list[ContextEntry] = []
        text_blocks: list[str] = []
        estimated_tokens = 0
        truncated = False
        truncation_reason = None

        # 1. Retrieve matching verified active skills (Phase 7 Procedural Learning Integration)
        if include_skills:
            try:
                from validators.skill.retriever import SkillRetriever
                retriever = SkillRetriever(self.project_path)
                matched_skills = retriever.retrieve_skills(query, contract=contract, limit=2)
                for res in matched_skills:
                    skill_block = res.formatted_guidance
                    skill_tokens = _estimate_tokens(skill_block)
                    if estimated_tokens + skill_tokens <= token_budget:
                        entries.append(
                            ContextEntry(
                                node_id=res.skill.skill_id,
                                rank=110,
                                confidence=res.skill.metrics.confidence_score,
                                text=skill_block,
                                reason="procedural_skill",
                                why_retrieved=("verified_procedural_skill", f"relevance={res.relevance_score:.2f}"),
                                access_status="allowed",
                            )
                        )
                        text_blocks.append(skill_block)
                        estimated_tokens += skill_tokens
            except Exception:
                # Skill retrieval failures must fail open for standard graph context
                pass

        for rank, result in sorted(
            ranked.values(),
            key=lambda item: (-item[0], item[1].path, item[1].qualified_name, item[1].node_id),
        ):
            block = _context_block(result)
            block_tokens = _estimate_tokens(block)
            if estimated_tokens + block_tokens > token_budget:
                truncated = True
                truncation_reason = f'context token budget {token_budget} reached'
                break
            entries.append(
                ContextEntry(
                    node_id=result.node_id,
                    rank=rank,
                    confidence=result.confidence,
                    text=block,
                    reason=result.reason or 'context',
                    why_retrieved=tuple(result.metadata.get('why_retrieved', [result.reason or 'context'])),
                    access_status='allowed',
                )
            )
            text_blocks.append(block)
            estimated_tokens += block_tokens
            if len(entries) >= limit:
                truncated = len(ranked) > limit
                if truncated:
                    truncation_reason = f'context entry limit {limit} reached'
                break

        return ContextBundle(
            revision=seed_envelope.revision,
            git_commit=seed_envelope.git_commit,
            stale=seed_envelope.stale,
            semantic=seed_envelope.semantic,
            token_budget=token_budget,
            estimated_tokens=estimated_tokens,
            truncated=truncated,
            truncation_reason=truncation_reason,
            entries=tuple(entries),
            text='\n\n'.join(text_blocks),
        )

    def _active_records(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.registry.load_all()
            if record.get('status') == 'active'
        ]

    def _edges_for(
        self,
        node_id: str,
        *,
        relation: str | None,
        direction: str,
        contract: AgentContract | str | Path | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if direction == 'inbound':
            return tuple(
                {
                    **edge,
                    'confidence': edge.get('confidence', 1.0),
                    'neighbor_id': edge.get('source_id'),
                }
                for edge in lookup_reverse_edges(self.project_path, node_id, relation=relation)
                if edge.get('source_id')
            )
        node = self._lookup_node_by_id(node_id, contract=contract)
        if node is None:
            return ()
        outbound = node.get('deterministic', {}).get('outgoing', [])
        values = []
        for edge in outbound:
            if relation is not None and edge.get('relation') != relation:
                continue
            if not edge.get('target_id'):
                continue
            values.append(
                {
                    **edge,
                    'neighbor_id': edge.get('target_id'),
                }
            )
        return tuple(values)

    def _is_stale(self) -> bool:
        revision, git_commit = self._revision_meta()
        if revision == 0:
            return False
        git_dir = self.project_path / '.git'
        if not git_dir.exists():
            return False
        status = _git_output(
            self.project_path,
            'status',
            '--porcelain',
            '--untracked-files=all',
        )
        if not status:
            return False
        return any(
            len(line) >= 4 and line[3:].strip().endswith('.py')
            for line in status.splitlines()
        )

    def _load_node_for_record(self, record: dict[str, Any], contract: AgentContract | str | Path | None = None) -> dict[str, Any] | None:
        node_id = str(record.get('node_id', ''))
        kind = str(record.get('kind', ''))
        if not node_id or not kind:
            return None
        self._enforce_node(node_id, contract)
        path = node_path(self.project_path, kind, node_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def _lookup_node_by_id(self, node_id: str, contract: AgentContract | str | Path | None = None) -> dict[str, Any] | None:
        self._enforce_node(node_id, contract)
        record = self.registry.load(node_id)
        if record is None:
            return None
        return self._load_node_for_record(record, contract=contract)

    def _resolve_one(self, needle: str, contract: AgentContract | str | Path | None = None) -> KnowledgeResult | None:
        exact = self.lookup(needle, limit=1, contract=contract)
        if exact.results:
            return exact.results[0]
        searched = self.search(needle, limit=1, contract=contract)
        if searched.results:
            return searched.results[0]
        return None

    def _result_from_node(
        self,
        node: dict[str, Any],
        *,
        confidence: float,
        alias_matched: bool = False,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        evidence: Sequence[AnalysisEvidence | dict[str, Any]] | None = None,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeResult:
        node_id = str(node.get('node_id', ''))
        if node_id:
            self._enforce_node(node_id, contract)
        deterministic = node.get('deterministic', {})
        ai_block = node.get('ai', {})
        summary = ai_block.get('summary') if isinstance(ai_block, dict) else None
        result_metadata = dict(metadata or {})
        if isinstance(ai_block, dict) and ai_block:
            result_metadata['ai_stale'] = is_ai_stale(node)
        evidence_items = _evidence_items(evidence or ())
        return KnowledgeResult(
            node_id=str(node.get('node_id', '')),
            kind=str(node.get('kind', '')),
            path=str(deterministic.get('path', '')),
            qualified_name=str(deterministic.get('qualified_name', '')),
            signature=str(deterministic.get('signature', '')),
            confidence=round(float(confidence), 4),
            summary=str(summary) if summary else None,
            alias_matched=alias_matched,
            reason=reason,
            metadata=result_metadata,
            evidence=tuple(evidence_items),
            provenance_summary=_provenance_summary(evidence_items),
        )

    def _result_from_record(
        self,
        record: dict[str, Any],
        *,
        confidence: float,
        alias_matched: bool = False,
        reason: str | None = None,
        contract: AgentContract | str | Path | None = None,
    ) -> KnowledgeResult:
        node_id = str(record.get('node_id', ''))
        if node_id:
            self._enforce_node(node_id, contract)
        node = self._load_node_for_record(record, contract=contract)
        if node is not None:
            return self._result_from_node(
                node,
                confidence=confidence,
                alias_matched=alias_matched,
                reason=reason,
                contract=contract,
            )

        current = record.get('current', {})
        return KnowledgeResult(
            node_id=str(record.get('node_id', '')),
            kind=str(record.get('kind', '')),
            path=str(current.get('path', '')),
            qualified_name=str(current.get('qualified_name', '')),
            signature='',
            confidence=round(float(confidence), 4),
            alias_matched=alias_matched,
            reason=reason,
            provenance_summary='registry metadata',
        )

    def _revision_meta(self) -> tuple[int, str | None]:
        revision = latest_revision_id(self.project_path)
        if revision == 0:
            return 0, None
        path = revision_path(self.project_path, revision)
        if not path.exists():
            return revision, None
        data = json.loads(path.read_text(encoding='utf-8'))
        revision_inputs = data.get('revision_inputs', {})
        if not isinstance(revision_inputs, dict):
            return revision, None
        git_commit = revision_inputs.get('git_commit')
        return revision, str(git_commit) if git_commit else None

    def _semantic_search(
        self,
        query_embedding: Sequence[float],
        *,
        limit: int,
        contract: AgentContract | str | Path | None = None,
    ) -> tuple[KnowledgeResult, ...]:
        scored: list[tuple[float, KnowledgeResult]] = []
        for record in self._active_records():
            node_id = str(record.get('node_id', ''))
            try:
                if node_id:
                    self._enforce_node(node_id, contract)
            except (ContractAccessDenied, ContractExpiredError):
                continue
            node = self._load_node_for_record(record, contract=contract)
            if node is None:
                continue
            deterministic = node.get('deterministic', {})
            content_hash = str(deterministic.get('content_hash', ''))
            if not content_hash:
                continue
            cache_path = embedding_cache_path(self.project_path, content_hash)
            if not cache_path.exists():
                continue
            data = json.loads(cache_path.read_text(encoding='utf-8'))
            vector = tuple(float(item) for item in data.get('vector', []))
            score = _cosine_similarity(query_embedding, vector)
            if score is None:
                continue
            scored.append(
                (
                    score,
                    self._result_from_node(
                        node,
                        confidence=round(score, 4),
                        reason='semantic-search',
                        metadata={'why_retrieved': ['semantic_similarity']},
                        contract=contract,
                    ),
                )
            )
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].path,
                item[1].qualified_name,
                item[1].node_id,
            )
        )
        return tuple(item[1] for item in scored[:limit])


def _context_block(result: KnowledgeResult) -> str:
    lines = [
        f'[{result.kind}] {result.qualified_name}',
        f'path: {result.path}',
    ]
    if result.signature:
        lines.append(f'signature: {result.signature}')
    if result.summary:
        lines.append(f'summary: {result.summary}')
    if result.reason:
        lines.append(f'reason: {result.reason}')
    if result.provenance_summary:
        lines.append(f'provenance: {result.provenance_summary}')
    why = result.metadata.get('why_retrieved')
    if why:
        lines.append(f"why_retrieved: {', '.join(str(item) for item in why)}")
    relation = result.metadata.get('relation')
    if relation:
        lines.append(f'relation: {relation}')
    depth = result.metadata.get('depth')
    if depth is not None:
        lines.append(f'depth: {depth}')
    return '\n'.join(lines)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left or not right:
        return None
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return numerator / (left_norm * right_norm)


def _evidence_from_edge(edge: dict[str, Any]) -> tuple[AnalysisEvidence, ...]:
    return tuple(_evidence_items(edge.get('evidence', ())))


def _evidence_items(values: Sequence[AnalysisEvidence | dict[str, Any]]) -> list[AnalysisEvidence]:
    return [
        item if isinstance(item, AnalysisEvidence) else AnalysisEvidence.from_dict(item)
        for item in values
    ]


def _provenance_summary(evidence: Sequence[AnalysisEvidence]) -> str:
    if not evidence:
        return 'static'
    providers = ', '.join(sorted({item.provider for item in evidence}))
    kinds = ', '.join(sorted({item.evidence_type for item in evidence}))
    return f'{len(evidence)} evidence item(s): {providers}; {kinds}'


def _evidence_matches(evidence: Sequence[AnalysisEvidence], wanted: Sequence[str]) -> bool:
    lowered = {item.lower() for item in wanted}
    if 'static' in lowered and not evidence:
        return True
    for item in evidence:
        evidence_type = item.evidence_type.lower()
        provider = item.provider.lower()
        if 'runtime' in lowered and ('runtime' in evidence_type or 'runtime' in provider):
            return True
        if 'static' in lowered and 'runtime' not in evidence_type and 'runtime' not in provider:
            return True
    return False


def _why_for_edge(edge: dict[str, Any]) -> list[str]:
    relation = edge.get('relation')
    reasons = ['caller_relationship' if relation == 'CALLS' else 'graph_relationship']
    if relation == 'FLOWS_TO':
        reasons = ['flow_path']
    if any(item.provider == 'runtime-tracer' for item in _evidence_from_edge(edge)):
        reasons.append('runtime_confirmed')
    return reasons


def _evidence_boost(evidence: Sequence[AnalysisEvidence]) -> int:
    if any(item.provider == 'runtime-tracer' for item in evidence):
        return 15
    if evidence:
        return 5
    return 0


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode('utf-8')).hexdigest()


def _git_output(project_path: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ['git', *args],
            cwd=str(project_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        return ''
    return completed.stdout.strip()


def _lookup_rank(record: dict[str, Any], needle: str) -> tuple[int, float, bool]:
    normalized = needle.strip()
    lowered = normalized.lower()
    if not lowered:
        return -1, 0.0, False

    node_id = str(record.get('node_id', ''))
    birth = str(record.get('birth_key', ''))
    current = record.get('current', {}) if isinstance(record.get('current'), dict) else {}
    current_name = str(current.get('qualified_name', ''))
    current_simple = current_name.rsplit('.', 1)[-1]
    current_path = str(current.get('path', ''))
    aliases = [
        alias
        for alias in record.get('aliases', [])
        if isinstance(alias, str)
    ]
    alias_names = [alias.partition(':')[2] for alias in aliases]

    if node_id.lower() == lowered:
        return 0, 1.0, False
    if birth.lower() == lowered:
        return 1, 1.0, birth != f'{current_path}:{current_name}'
    if current_name.lower() == lowered:
        return 2, 0.99, False
    if current_simple.lower() == lowered:
        return 3, 0.97, False
    if lowered in (alias.lower() for alias in aliases):
        return 4, 0.96, True
    if lowered in (name.lower() for name in alias_names):
        return 5, 0.95, True
    if current_path.lower() == lowered:
        return 6, 0.9, False
    return -1, 0.0, False


__all__ = [
    'ContextBundle',
    'ContextEntry',
    'KnowledgeAPI',
    'KnowledgeEnvelope',
    'KnowledgeResult',
]
