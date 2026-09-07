"""Async, AI-only background enrichment for knowledge nodes."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from cli.lock import acquire_lock, release_lock

from .enricher_queue import EnricherQueue
from .projections.reverse_index import lookup_reverse_edges
from .storage import canonical_json, knowledge_root

PRIVACY_MODES = {'full', 'signatures', 'local', 'off'}
DEFAULT_PROMPT_VERSION = 'enrich-v1'
SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|token|secret|password|passwd|private[_-]?key)\s*[:=]\s*["\'][^"\']+["\']'),
    re.compile(r'(?i)(authorization:\s*bearer\s+)[a-z0-9._-]+'),
    re.compile(r'(?i)(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})'),
    re.compile(r'(?i)(aws_access_key_id|aws_secret_access_key)\s*[:=]\s*["\'][^"\']+["\']'),
    re.compile(r'-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----'),
    re.compile(r'(?i)(postgres|mysql|mongodb|redis):\/\/[^:\s]+:[^@\s]+@[^\s]+'),
]


@dataclass(frozen=True)
class EnricherConfig:
    """Runtime config for Stage-5 background enrichment."""

    privacy_mode: str = 'full'
    prompt_version: str = DEFAULT_PROMPT_VERSION
    daily_call_cap: int = 500
    daily_token_cap: int = 50_000
    max_attempts: int = 3
    base_backoff_seconds: float = 60.0
    summary_model: str = 'summary-mock-1'
    embedding_model: str = 'embedding-mock-1'

    @classmethod
    def from_project(cls, project_path: Path) -> 'EnricherConfig':
        config_path = project_path.resolve() / '.sync' / 'config' / 'enricher.yaml'
        if not config_path.exists():
            return cls()
        data = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        if not isinstance(data, dict):
            return cls()
        return cls(
            privacy_mode=str(data.get('privacy_mode', 'full')),
            prompt_version=str(data.get('prompt_version', DEFAULT_PROMPT_VERSION)),
            daily_call_cap=int(data.get('daily_call_cap', 500)),
            daily_token_cap=int(data.get('daily_token_cap', 50_000)),
            max_attempts=int(data.get('max_attempts', 3)),
            base_backoff_seconds=float(data.get('base_backoff_seconds', 60.0)),
            summary_model=str(data.get('summary_model', 'summary-mock-1')),
            embedding_model=str(data.get('embedding_model', 'embedding-mock-1')),
        )


@dataclass(frozen=True)
class SummaryRequest:
    node_id: str
    kind: str
    path: str
    qualified_name: str
    signature: str
    content_hash: str
    privacy_mode: str
    content: str


@dataclass(frozen=True)
class SummaryResponse:
    summary: str
    confidence: float
    purpose: str | None = None
    risk: str | None = None
    tokens_used: int = 0
    model: str = ''


@dataclass(frozen=True)
class EmbeddingRequest:
    node_id: str
    content_hash: str
    privacy_mode: str
    text: str


@dataclass(frozen=True)
class EmbeddingResponse:
    vector: tuple[float, ...]
    dimensions: int
    tokens_used: int = 0
    model: str = ''


@dataclass(frozen=True)
class EnrichmentRunResult:
    admitted: int = 0
    processed: int = 0
    skipped: int = 0
    parked: int = 0
    paused: bool = False
    queue_depth: int = 0


class SummaryBackend(Protocol):
    def summarize(self, request: SummaryRequest) -> SummaryResponse:
        """Return a tagged summary for one node."""


class EmbeddingBackend(Protocol):
    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Return an embedding vector for one node payload."""


class KnowledgeEnricher:
    """Queue-driven Stage-5 enricher that only mutates node `ai` blocks."""

    def __init__(
        self,
        project_path: Path,
        summary_backend: SummaryBackend | None,
        embedding_backend: EmbeddingBackend | None,
        *,
        agent: str = 'codex',
        config: EnricherConfig | None = None,
        queue: EnricherQueue | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.summary_backend = summary_backend
        self.embedding_backend = embedding_backend
        self.agent = agent
        self.config = config or EnricherConfig.from_project(self.project_path)
        if self.config.privacy_mode not in PRIVACY_MODES:
            raise ValueError(f'Unsupported privacy mode: {self.config.privacy_mode}')
        self.queue = queue or EnricherQueue(
            self.project_path,
            agent=agent,
            daily_call_cap=self.config.daily_call_cap,
            daily_token_cap=self.config.daily_token_cap,
            max_attempts=self.config.max_attempts,
            base_backoff_seconds=self.config.base_backoff_seconds,
        )

    def admit_stale_nodes(self) -> int:
        if self.config.privacy_mode == 'off':
            self.queue.pause('privacy_mode_off')
            return 0
        count = 0
        for _, node in _iter_nodes(self.project_path):
            if not self._needs_enrichment(node):
                continue
            deterministic = node.get('deterministic', {})
            if self.queue.enqueue(
                str(node['node_id']),
                str(deterministic.get('content_hash', '')),
                priority=self._priority(node),
            ):
                count += 1
        return count

    def run_once(
        self,
        *,
        max_jobs: int | None = None,
        now: datetime | None = None,
    ) -> EnrichmentRunResult:
        current = now or datetime.now(timezone.utc)
        admitted = self.admit_stale_nodes()
        if self.config.privacy_mode == 'off':
            return EnrichmentRunResult(
                admitted=admitted,
                paused=True,
                queue_depth=read_queue_depth(self.project_path),
            )
        jobs = self.queue.due_jobs(now=current, limit=max_jobs)
        processed = 0
        skipped = 0
        parked = 0
        paused = False

        for job in jobs:
            loaded = _load_node(self.project_path, job.node_id)
            if loaded is None:
                self.queue.mark_dropped(job.node_id, now=current)
                skipped += 1
                continue
            node_path, node = loaded
            if node.get('status', 'active') != 'active':
                self.queue.mark_dropped(job.node_id, now=current)
                skipped += 1
                continue
            deterministic = node.get('deterministic', {})
            content_hash = str(deterministic.get('content_hash', ''))
            if content_hash != job.content_hash:
                self.queue.enqueue(job.node_id, content_hash, priority=job.priority, now=current)
                skipped += 1
                continue
            if not self._needs_enrichment(node):
                self.queue.mark_dropped(job.node_id, now=current)
                skipped += 1
                continue

            cache_hit = embedding_cache_path(self.project_path, content_hash).exists()
            required_calls = 1 if cache_hit else 2
            if not self.queue.can_start_job(required_calls=required_calls, now=current):
                self.queue.pause('daily_call_cap_exhausted', now=current)
                paused = True
                break

            try:
                summary = self._summarize(node)
                embedding, cache_hit = self._embedding_for(node, summary, cache_hit=cache_hit)
                ai_block = {
                    'confidence': summary.confidence,
                    'embedding_dimensions': embedding.dimensions,
                    'embedding_model': embedding.model or self.config.embedding_model,
                    'enriched_at': current.isoformat(),
                    'enriched_hash': content_hash,
                    'model': summary.model or self.config.summary_model,
                    'privacy_mode': self.config.privacy_mode,
                    'prompt_version': self.config.prompt_version,
                    'purpose': summary.purpose,
                    'risk': summary.risk,
                    'summary': summary.summary,
                }
                _patch_ai_block(
                    self.project_path,
                    node_path,
                    node,
                    ai_block,
                    agent=self.agent,
                )
                state = self.queue.mark_success(
                    job.node_id,
                    calls_used=required_calls,
                    tokens_used=summary.tokens_used + embedding.tokens_used,
                    cache_hit=cache_hit,
                    now=current,
                )
                processed += 1
                if state.paused:
                    paused = True
                    break
            except Exception as exc:  # pragma: no cover
                if self.queue.mark_retry(job.node_id, reason=str(exc), now=current):
                    parked += 1

        return EnrichmentRunResult(
            admitted=admitted,
            processed=processed,
            skipped=skipped,
            parked=parked,
            paused=paused,
            queue_depth=read_queue_depth(self.project_path),
        )

    def _needs_enrichment(self, node: dict[str, Any]) -> bool:
        if node.get('status', 'active') != 'active':
            return False
        ai = node.get('ai', {})
        if not isinstance(ai, dict) or not ai:
            return True
        deterministic = node.get('deterministic', {})
        return (
            ai.get('enriched_hash') != deterministic.get('content_hash')
            or ai.get('privacy_mode') != self.config.privacy_mode
            or ai.get('prompt_version') != self.config.prompt_version
        )

    def _priority(self, node: dict[str, Any]) -> int:
        deterministic = node.get('deterministic', {})
        try:
            inbound = lookup_reverse_edges(
                self.project_path,
                str(node.get('node_id')),
                relation='CALLS',
            )
        except FileNotFoundError:
            inbound = []
        base = 10 if str(node.get('kind')) in {'Class', 'Module'} else 0
        if str(deterministic.get('path', '')).startswith('.sync/'):
            base += 100
        return base + len(inbound)

    def _summarize(self, node: dict[str, Any]) -> SummaryResponse:
        if self.summary_backend is None:
            raise RuntimeError('Summary backend is required for enrichment runs')
        deterministic = node.get('deterministic', {})
        request = SummaryRequest(
            node_id=str(node['node_id']),
            kind=str(node.get('kind', '')),
            path=str(deterministic.get('path', '')),
            qualified_name=str(deterministic.get('qualified_name', '')),
            signature=str(deterministic.get('signature', '')),
            content_hash=str(deterministic.get('content_hash', '')),
            privacy_mode=self.config.privacy_mode,
            content=self._payload_for(node),
        )
        return self.summary_backend.summarize(request)

    def _embedding_for(
        self,
        node: dict[str, Any],
        summary: SummaryResponse,
        *,
        cache_hit: bool,
    ) -> tuple[EmbeddingResponse, bool]:
        deterministic = node.get('deterministic', {})
        content_hash = str(deterministic.get('content_hash', ''))
        cache_path = embedding_cache_path(self.project_path, content_hash)
        if cache_hit and cache_path.exists():
            data = json.loads(cache_path.read_text(encoding='utf-8'))
            vector = tuple(float(item) for item in data.get('vector', []))
            return (
                EmbeddingResponse(
                    vector=vector,
                    dimensions=int(data.get('dimensions', len(vector))),
                    model=str(data.get('model', self.config.embedding_model)),
                ),
                True,
            )
        if self.embedding_backend is None:
            raise RuntimeError('Embedding backend is required for enrichment runs')
        request = EmbeddingRequest(
            node_id=str(node['node_id']),
            content_hash=content_hash,
            privacy_mode=self.config.privacy_mode,
            text=self._payload_for(node) + f'\nsummary: {summary.summary}',
        )
        response = self.embedding_backend.embed(request)
        _write_embedding_cache(
            self.project_path,
            content_hash,
            {
                'content_hash': content_hash,
                'dimensions': response.dimensions,
                'model': response.model or self.config.embedding_model,
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'vector': list(response.vector),
            },
            agent=self.agent,
        )
        return response, False

    def _payload_for(self, node: dict[str, Any]) -> str:
        deterministic = node.get('deterministic', {})
        header = [
            f"kind: {node.get('kind')}",
            f"path: {deterministic.get('path')}",
            f"qualified_name: {deterministic.get('qualified_name')}",
            f"signature: {deterministic.get('signature')}",
        ]
        if self.config.privacy_mode == 'signatures':
            return '\n'.join(header)
        if self.config.privacy_mode in {'full', 'local'}:
            excerpt = _source_excerpt(self.project_path, node)
            if excerpt:
                header.extend(['source:', excerpt])
        return '\n'.join(header)


def enqueue_stale_nodes(
    project_path: Path,
    *,
    agent: str = 'codex',
    config: EnricherConfig | None = None,
) -> int:
    """Populate the enrichment queue without performing any LLM work."""
    enricher = KnowledgeEnricher(
        project_path,
        summary_backend=None,
        embedding_backend=None,
        agent=agent,
        config=config,
    )
    return enricher.admit_stale_nodes()


def embedding_cache_path(project_path: Path, content_hash: str) -> Path:
    cache_root = knowledge_root(project_path.resolve()) / 'cache' / 'embeddings'
    return cache_root / content_hash[:2] / f'{content_hash}.json'


def is_ai_stale(node: dict[str, Any]) -> bool:
    ai = node.get('ai', {})
    deterministic = node.get('deterministic', {})
    if not isinstance(ai, dict) or not ai:
        return True
    return ai.get('enriched_hash') != deterministic.get('content_hash')


def is_ai_only_delta(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return _without_ai(before) == _without_ai(after)


def read_queue_depth(project_path: Path) -> int:
    queue = EnricherQueue(project_path)
    return int(queue.snapshot()['jobs'])


def _iter_nodes(project_path: Path):
    root = knowledge_root(project_path.resolve()) / 'nodes'
    if not root.exists():
        return
    for path in sorted(root.glob('*/*/*.json')):
        yield path, json.loads(path.read_text(encoding='utf-8'))


def _load_node(project_path: Path, node_id: str) -> tuple[Path, dict[str, Any]] | None:
    root = knowledge_root(project_path.resolve()) / 'nodes'
    matches = sorted(root.glob(f'*/*/{node_id}.json'))
    if not matches:
        return None
    path = matches[0]
    return path, json.loads(path.read_text(encoding='utf-8'))


def _patch_ai_block(
    project_path: Path,
    path: Path,
    before: dict[str, Any],
    ai_block: dict[str, Any],
    *,
    agent: str,
) -> None:
    after = deepcopy(before)
    after['ai'] = ai_block
    if not is_ai_only_delta(before, after):
        raise RuntimeError('Stage-5 diff touched fields outside ai')
    _write_json_locked(
        project_path.resolve(),
        path,
        after,
        agent=agent,
        session_id='enrichment-node',
    )


def _without_ai(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k != 'ai'}


def _source_excerpt(project_path: Path, node: dict[str, Any]) -> str:
    deterministic = node.get('deterministic', {})
    rel_path = deterministic.get('path')
    location = deterministic.get('location', {})
    if not isinstance(rel_path, str):
        return ''
    base_dir = project_path.resolve()
    source_path = (base_dir / rel_path).resolve()
    try:
        source_path.relative_to(base_dir)
    except ValueError:
        return ''  # Block path traversal outside project root
    if not source_path.exists():
        return ''
    lines = source_path.read_text(encoding='utf-8').splitlines()
    start = max(int(location.get('line', 1)) - 1, 0)
    end = max(int(location.get('end_line', location.get('line', 1))), start + 1)
    excerpt = '\n'.join(lines[start:end])
    excerpt = _redact_secrets(excerpt)
    return excerpt[:4000]


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups > 0:
            redacted = pattern.sub(r'\1***', redacted)
        else:
            redacted = pattern.sub(r'***', redacted)
    return redacted


def _write_embedding_cache(
    project_path: Path,
    content_hash: str,
    document: dict[str, Any],
    *,
    agent: str,
) -> None:
    path = embedding_cache_path(project_path, content_hash)
    _write_json_locked(
        project_path.resolve(),
        path,
        document,
        agent=agent,
        session_id='embedding-cache',
    )


def _write_json_locked(
    project_path: Path,
    path: Path,
    document: dict[str, Any],
    *,
    agent: str,
    session_id: str,
) -> None:
    sync_path = project_path / '.sync'
    ok, message = acquire_lock(sync_path, agent, session_id=session_id)
    if not ok:
        raise RuntimeError(message)
    try:
        payload = canonical_json(document)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        try:
            temporary.write_text(payload, encoding='utf-8', newline='\n')
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    finally:
        release_lock(sync_path, agent)


__all__ = [
    'DEFAULT_PROMPT_VERSION',
    'EmbeddingBackend',
    'EmbeddingRequest',
    'EmbeddingResponse',
    'EnricherConfig',
    'EnrichmentRunResult',
    'KnowledgeEnricher',
    'SummaryBackend',
    'SummaryRequest',
    'SummaryResponse',
    'embedding_cache_path',
    'enqueue_stale_nodes',
    'is_ai_only_delta',
    'is_ai_stale',
    'read_queue_depth',
]
