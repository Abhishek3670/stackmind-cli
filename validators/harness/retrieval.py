"""Retrieval helpers for the governed harness runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    """Normalized external retrieval hit."""

    title: str
    url: str
    snippet: str
    source: str
    published_at: str | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            'confidence': self.confidence,
            'published_at': self.published_at,
            'snippet': self.snippet,
            'source': self.source,
            'title': self.title,
            'url': self.url,
        }


@dataclass(frozen=True)
class EvidenceSnippet:
    """Sanitized evidence derived from retrieval results."""

    title: str
    url: str
    source: str
    published_at: str | None
    quoted: str
    sanitized: bool
    dropped_lines: int

    def render(self) -> str:
        date_part = f", {self.published_at}" if self.published_at else ""
        return (
            f"Evidence: {self.title} ({self.source}, {self.url}{date_part})\n"
            f"Quoted evidence: \"{self.quoted}\""
        )

    def to_dict(self) -> dict[str, object]:
        return {
            'dropped_lines': self.dropped_lines,
            'published_at': self.published_at,
            'quoted': self.quoted,
            'sanitized': self.sanitized,
            'source': self.source,
            'title': self.title,
            'url': self.url,
        }


class SearchProvider(Protocol):
    """Provider contract for external retrieval."""

    name: str

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        """Return normalized hits for a query."""


@dataclass(frozen=True)
class RetrievalPolicy:
    """Controls retrieval gating, caching, and budget caps."""

    enabled: bool = True
    max_searches: int = 2
    cost_cap: float = 1.0
    cost_per_search: float = 0.25
    max_snippet_chars: int = 280


@dataclass(frozen=True)
class RetrievalBatch:
    """One retrieval attempt plus observability counters."""

    results: tuple[SearchResult, ...]
    evidence: tuple[EvidenceSnippet, ...]
    searches_used: int
    cache_hits: int
    cap_exhausted: bool
    mode: str
    cost_estimate: float
    query: str | None = None


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_INSTRUCTION_PATTERNS = (
    'ignore previous',
    'ignore all previous',
    'system prompt',
    'developer message',
    'assistant:',
    'user:',
    'follow these instructions',
    'you are chatgpt',
    'act as',
)


class SessionSearchTool:
    """Bounded retrieval tool with session-local caching and cost caps."""

    def __init__(
        self,
        provider: SearchProvider | None = None,
        *,
        policy: RetrievalPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy or RetrievalPolicy(enabled=provider is not None)
        self._cache: dict[str, tuple[SearchResult, ...]] = {}
        self.searches_used = 0
        self.cache_hits = 0
        self.cost_estimate = 0.0
        self.cap_exhausted = False

    def search(self, query: str, *, limit: int = 5) -> RetrievalBatch:
        normalized = query.strip()
        if not normalized or not self.policy.enabled or self.provider is None:
            return RetrievalBatch(
                results=(),
                evidence=(),
                searches_used=self.searches_used,
                cache_hits=self.cache_hits,
                cap_exhausted=self.cap_exhausted,
                mode='internal_only',
                cost_estimate=round(self.cost_estimate, 6),
                query=normalized or None,
            )

        cache_key = normalized.lower()
        if cache_key in self._cache:
            self.cache_hits += 1
            cached = self._cache[cache_key][:limit]
            evidence = sanitize_search_results(
                cached,
                max_snippet_chars=self.policy.max_snippet_chars,
            )
            return RetrievalBatch(
                results=cached,
                evidence=evidence,
                searches_used=self.searches_used,
                cache_hits=self.cache_hits,
                cap_exhausted=self.cap_exhausted,
                mode='augmented',
                cost_estimate=round(self.cost_estimate, 6),
                query=normalized,
            )

        next_cost = self.cost_estimate + self.policy.cost_per_search
        if self.searches_used >= self.policy.max_searches or next_cost > self.policy.cost_cap:
            self.cap_exhausted = True
            return RetrievalBatch(
                results=(),
                evidence=(),
                searches_used=self.searches_used,
                cache_hits=self.cache_hits,
                cap_exhausted=True,
                mode='internal_only',
                cost_estimate=round(self.cost_estimate, 6),
                query=normalized,
            )

        results = tuple(self.provider.search(normalized, limit=limit))
        self._cache[cache_key] = results
        self.searches_used += 1
        self.cost_estimate = round(next_cost, 6)
        evidence = sanitize_search_results(
            results,
            max_snippet_chars=self.policy.max_snippet_chars,
        )
        return RetrievalBatch(
            results=results,
            evidence=evidence,
            searches_used=self.searches_used,
            cache_hits=self.cache_hits,
            cap_exhausted=self.cap_exhausted,
            mode='augmented',
            cost_estimate=round(self.cost_estimate, 6),
            query=normalized,
        )


def sanitize_search_results(
    results: tuple[SearchResult, ...] | list[SearchResult],
    *,
    max_snippet_chars: int = 280,
) -> tuple[EvidenceSnippet, ...]:
    """Neutralize instruction-like external content into quoted evidence."""

    evidence: list[EvidenceSnippet] = []
    for result in results:
        sanitized = _CODE_FENCE_RE.sub(' [code removed] ', result.snippet)
        kept_lines: list[str] = []
        dropped_lines = 0
        for raw_line in sanitized.splitlines():
            lowered = raw_line.strip().lower()
            if lowered and any(marker in lowered for marker in _INSTRUCTION_PATTERNS):
                dropped_lines += 1
                continue
            kept_lines.append(raw_line.strip())
        quoted = _WHITESPACE_RE.sub(' ', ' '.join(kept_lines)).strip()
        if len(quoted) > max_snippet_chars:
            quoted = quoted[: max_snippet_chars - 3].rstrip() + '...'
        if not quoted:
            quoted = '[snippet removed during sanitization]'
        evidence.append(
            EvidenceSnippet(
                title=result.title,
                url=result.url,
                source=result.source,
                published_at=result.published_at,
                quoted=quoted,
                sanitized=(quoted != result.snippet.strip()) or dropped_lines > 0,
                dropped_lines=dropped_lines,
            )
        )
    return tuple(evidence)


__all__ = [
    'EvidenceSnippet',
    'RetrievalBatch',
    'RetrievalPolicy',
    'SearchProvider',
    'SearchResult',
    'SessionSearchTool',
    'sanitize_search_results',
]
