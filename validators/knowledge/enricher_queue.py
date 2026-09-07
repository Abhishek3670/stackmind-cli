"""Persistent, idempotent queue state for background knowledge enrichment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cli.lock import acquire_lock, release_lock

from .storage import canonical_json, knowledge_root

ENRICHMENT_QUEUE_VERSION = 'enrichment-1'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(now: datetime | None = None) -> str:
    return (now or _utcnow()).date().isoformat()


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class EnricherJob:
    """One coalesced enrichment job keyed by NodeID."""

    node_id: str
    content_hash: str
    attempts: int = 0
    next_run_at: str = ''
    parked_reason: str | None = None
    priority: int = 0
    status: str = 'queued'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'EnricherJob':
        return cls(
            node_id=str(data['node_id']),
            content_hash=str(data['content_hash']),
            attempts=int(data.get('attempts', 0)),
            next_run_at=str(data.get('next_run_at', '')),
            parked_reason=data.get('parked_reason'),
            priority=int(data.get('priority', 0)),
            status=str(data.get('status', 'queued')),
        )

    def is_due(self, now: datetime) -> bool:
        if self.status != 'queued':
            return False
        if not self.next_run_at:
            return True
        return _parse_iso(self.next_run_at) <= now

    def to_dict(self) -> dict[str, Any]:
        return {
            'attempts': self.attempts,
            'content_hash': self.content_hash,
            'next_run_at': self.next_run_at,
            'node_id': self.node_id,
            'parked_reason': self.parked_reason,
            'priority': self.priority,
            'status': self.status,
        }


@dataclass(frozen=True)
class EnrichmentQueueState:
    """Persisted enrichment queue state."""

    budget_day: str
    daily_call_cap: int
    daily_token_cap: int
    calls_used: int = 0
    tokens_used: int = 0
    processed_jobs: int = 0
    cache_hits: int = 0
    paused: bool = False
    pause_reason: str | None = None
    jobs: tuple[EnricherJob, ...] = ()
    schema_version: str = ENRICHMENT_QUEUE_VERSION

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        default_day: str,
        daily_call_cap: int,
        daily_token_cap: int,
    ) -> 'EnrichmentQueueState':
        jobs = tuple(
            sorted(
                (
                    EnricherJob.from_dict(item)
                    for item in data.get('jobs', [])
                    if isinstance(item, dict)
                ),
                key=lambda item: (item.status, -item.priority, item.node_id),
            )
        )
        return cls(
            budget_day=str(data.get('budget_day', default_day)),
            daily_call_cap=int(data.get('daily_call_cap', daily_call_cap)),
            daily_token_cap=int(data.get('daily_token_cap', daily_token_cap)),
            calls_used=int(data.get('calls_used', 0)),
            tokens_used=int(data.get('tokens_used', 0)),
            processed_jobs=int(data.get('processed_jobs', 0)),
            cache_hits=int(data.get('cache_hits', 0)),
            paused=bool(data.get('paused', False)),
            pause_reason=data.get('pause_reason'),
            jobs=jobs,
            schema_version=str(data.get('schema_version', ENRICHMENT_QUEUE_VERSION)),
        )

    @property
    def parked_jobs(self) -> int:
        return sum(1 for job in self.jobs if job.status == 'parked')

    @property
    def queued_jobs(self) -> int:
        return sum(1 for job in self.jobs if job.status == 'queued')

    def to_dict(self) -> dict[str, Any]:
        jobs = sorted(self.jobs, key=lambda item: (item.status, -item.priority, item.node_id))
        return {
            'budget_day': self.budget_day,
            'cache_hits': self.cache_hits,
            'calls_used': self.calls_used,
            'daily_call_cap': self.daily_call_cap,
            'daily_token_cap': self.daily_token_cap,
            'jobs': [job.to_dict() for job in jobs],
            'pause_reason': self.pause_reason,
            'paused': self.paused,
            'processed_jobs': self.processed_jobs,
            'schema_version': self.schema_version,
            'tokens_used': self.tokens_used,
        }


class EnricherQueue:
    """Crash-safe queue with NodeID coalescing and daily budget counters."""

    def __init__(
        self,
        project_path: Path,
        *,
        agent: str = 'codex',
        daily_call_cap: int = 500,
        daily_token_cap: int = 50_000,
        max_attempts: int = 3,
        base_backoff_seconds: float = 60.0,
    ) -> None:
        self.project_path = project_path.resolve()
        self.agent = agent
        self.daily_call_cap = daily_call_cap
        self.daily_token_cap = daily_token_cap
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    @property
    def path(self) -> Path:
        return knowledge_root(self.project_path) / 'enrichment' / 'queue.json'

    def load(self) -> EnrichmentQueueState:
        state = self._load_raw()
        rolled = self._rollover_if_needed(state, _utcnow())
        if rolled != state:
            self.save(rolled)
        return rolled

    def enqueue(
        self,
        node_id: str,
        content_hash: str,
        *,
        priority: int = 0,
        now: datetime | None = None,
    ) -> bool:
        current = now or _utcnow()
        state = self._rollover_if_needed(self._load_raw(), current)
        jobs = {job.node_id: job for job in state.jobs}
        existing = jobs.get(node_id)
        attempts = (
            0
            if existing is None or existing.content_hash != content_hash
            else existing.attempts
        )
        queued = EnricherJob(
            node_id=node_id,
            content_hash=content_hash,
            attempts=attempts,
            next_run_at='',
            parked_reason=None,
            priority=max(priority, existing.priority if existing is not None else priority),
            status='queued',
        )
        if existing == queued:
            return False
        jobs[node_id] = queued
        self.save(self._with_jobs(state, jobs.values()))
        return True

    def due_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[EnricherJob, ...]:
        state = self.load()
        if state.paused:
            return ()
        current = now or _utcnow()
        due = [job for job in state.jobs if job.is_due(current)]
        due.sort(key=lambda item: (-item.priority, item.node_id))
        if limit is not None:
            due = due[:limit]
        return tuple(due)

    def can_start_job(
        self,
        *,
        required_calls: int = 0,
        required_tokens: int = 0,
        now: datetime | None = None,
    ) -> bool:
        state = self._rollover_if_needed(self._load_raw(), now or _utcnow())
        if state.paused:
            return False
        if state.calls_used + required_calls > state.daily_call_cap:
            return False
        if state.tokens_used + required_tokens > state.daily_token_cap:
            return False
        return True

    def mark_success(
        self,
        node_id: str,
        *,
        calls_used: int = 0,
        tokens_used: int = 0,
        cache_hit: bool = False,
        now: datetime | None = None,
    ) -> EnrichmentQueueState:
        current = now or _utcnow()
        state = self._rollover_if_needed(self._load_raw(), current)
        jobs = {job.node_id: job for job in state.jobs}
        jobs.pop(node_id, None)
        updated = EnrichmentQueueState(
            budget_day=state.budget_day,
            daily_call_cap=state.daily_call_cap,
            daily_token_cap=state.daily_token_cap,
            calls_used=state.calls_used + calls_used,
            tokens_used=state.tokens_used + tokens_used,
            processed_jobs=state.processed_jobs + 1,
            cache_hits=state.cache_hits + (1 if cache_hit else 0),
            paused=state.paused,
            pause_reason=state.pause_reason,
            jobs=tuple(),
            schema_version=state.schema_version,
        )
        updated = self._with_jobs(updated, jobs.values())
        if updated.calls_used >= updated.daily_call_cap:
            updated = EnrichmentQueueState(
                **{**updated.__dict__, 'paused': True, 'pause_reason': 'daily_call_cap_exhausted'}
            )
        elif updated.tokens_used >= updated.daily_token_cap:
            updated = EnrichmentQueueState(
                **{**updated.__dict__, 'paused': True, 'pause_reason': 'daily_token_cap_exhausted'}
            )
        self.save(updated)
        return updated

    def mark_retry(
        self,
        node_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        current = now or _utcnow()
        state = self._rollover_if_needed(self._load_raw(), current)
        jobs = {job.node_id: job for job in state.jobs}
        job = jobs.get(node_id)
        if job is None:
            return False
        attempts = job.attempts + 1
        if attempts >= self.max_attempts:
            jobs[node_id] = EnricherJob(
                node_id=job.node_id,
                content_hash=job.content_hash,
                attempts=attempts,
                next_run_at='',
                parked_reason=reason,
                priority=job.priority,
                status='parked',
            )
            self.save(self._with_jobs(state, jobs.values()))
            return True
        backoff = self.base_backoff_seconds * (2 ** (attempts - 1))
        jobs[node_id] = EnricherJob(
            node_id=job.node_id,
            content_hash=job.content_hash,
            attempts=attempts,
            next_run_at=_iso(current + timedelta(seconds=backoff)),
            parked_reason=reason,
            priority=job.priority,
            status='queued',
        )
        self.save(self._with_jobs(state, jobs.values()))
        return False

    def mark_dropped(
        self,
        node_id: str,
        *,
        now: datetime | None = None,
    ) -> EnrichmentQueueState:
        state = self._rollover_if_needed(self._load_raw(), now or _utcnow())
        jobs = {job.node_id: job for job in state.jobs}
        jobs.pop(node_id, None)
        updated = self._with_jobs(state, jobs.values())
        self.save(updated)
        return updated

    def pause(
        self,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> EnrichmentQueueState:
        state = self._rollover_if_needed(self._load_raw(), now or _utcnow())
        updated = EnrichmentQueueState(
            **{**state.__dict__, 'paused': True, 'pause_reason': reason}
        )
        self.save(updated)
        return updated

    def resume(self, *, now: datetime | None = None) -> EnrichmentQueueState:
        state = self._rollover_if_needed(self._load_raw(), now or _utcnow())
        if not state.paused and state.pause_reason is None:
            return state
        updated = EnrichmentQueueState(
            **{**state.__dict__, 'paused': False, 'pause_reason': None}
        )
        self.save(updated)
        return updated

    def snapshot(self) -> dict[str, Any]:
        state = self.load()
        return {
            'budget_day': state.budget_day,
            'cache_hits': state.cache_hits,
            'calls_used': state.calls_used,
            'daily_call_cap': state.daily_call_cap,
            'daily_token_cap': state.daily_token_cap,
            'jobs': state.queued_jobs,
            'parked_jobs': state.parked_jobs,
            'pause_reason': state.pause_reason,
            'paused': state.paused,
            'processed_jobs': state.processed_jobs,
            'tokens_used': state.tokens_used,
        }

    def _default_state(self, day: str) -> EnrichmentQueueState:
        return EnrichmentQueueState(
            budget_day=day,
            daily_call_cap=self.daily_call_cap,
            daily_token_cap=self.daily_token_cap,
        )

    def _load_raw(self) -> EnrichmentQueueState:
        if not self.path.exists():
            return self._default_state(_day_key())
        data = json.loads(self.path.read_text(encoding='utf-8'))
        return EnrichmentQueueState.from_dict(
            data,
            default_day=_day_key(),
            daily_call_cap=self.daily_call_cap,
            daily_token_cap=self.daily_token_cap,
        )

    def _rollover_if_needed(
        self,
        state: EnrichmentQueueState,
        now: datetime,
    ) -> EnrichmentQueueState:
        day = _day_key(now)
        if state.budget_day == day:
            return state
        return EnrichmentQueueState(
            budget_day=day,
            daily_call_cap=state.daily_call_cap,
            daily_token_cap=state.daily_token_cap,
            jobs=state.jobs,
            schema_version=state.schema_version,
        )

    def _with_jobs(
        self,
        state: EnrichmentQueueState,
        jobs: Any,
    ) -> EnrichmentQueueState:
        return EnrichmentQueueState(
            budget_day=state.budget_day,
            daily_call_cap=state.daily_call_cap,
            daily_token_cap=state.daily_token_cap,
            calls_used=state.calls_used,
            tokens_used=state.tokens_used,
            processed_jobs=state.processed_jobs,
            cache_hits=state.cache_hits,
            paused=state.paused,
            pause_reason=state.pause_reason,
            jobs=tuple(
                sorted(jobs, key=lambda item: (item.status, -item.priority, item.node_id))
            ),
            schema_version=state.schema_version,
        )

    def save(self, state: EnrichmentQueueState) -> None:
        self._write_json_locked(self.path, state.to_dict())

    def _write_json_locked(self, path: Path, document: dict[str, Any]) -> None:
        sync_path = self.project_path / '.sync'
        ok, message = acquire_lock(sync_path, self.agent, session_id='enrichment-queue')
        if not ok:
            raise RuntimeError(message)
        try:
            payload = canonical_json(document)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + '.tmp')
            try:
                temporary.write_text(payload, encoding='utf-8', newline='\n')
                if os.name == 'nt' and path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        finally:
            release_lock(sync_path, self.agent)


def read_enrichment_status(project_path: Path) -> dict[str, Any]:
    """Return queue/budget/cache counters for graph stats."""
    queue = EnricherQueue(project_path)
    snapshot = queue.snapshot()
    cache_root = knowledge_root(project_path.resolve()) / 'cache' / 'embeddings'
    cache_entries = len(list(cache_root.glob('*/*.json'))) if cache_root.exists() else 0
    return {
        'enrichment_cache_entries': cache_entries,
        'enrichment_cache_hits': snapshot['cache_hits'],
        'enrichment_calls_used': snapshot['calls_used'],
        'enrichment_daily_call_cap': snapshot['daily_call_cap'],
        'enrichment_daily_token_cap': snapshot['daily_token_cap'],
        'enrichment_jobs': snapshot['jobs'],
        'enrichment_parked': snapshot['parked_jobs'],
        'enrichment_pause_reason': snapshot['pause_reason'] or 'none',
        'enrichment_paused': snapshot['paused'],
        'enrichment_processed': snapshot['processed_jobs'],
        'enrichment_tokens_used': snapshot['tokens_used'],
    }


__all__ = [
    'ENRICHMENT_QUEUE_VERSION',
    'EnricherJob',
    'EnricherQueue',
    'EnrichmentQueueState',
    'read_enrichment_status',
]
