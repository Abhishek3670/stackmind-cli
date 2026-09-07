"""Persistent content-hash cache for embedding backends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validators.knowledge.enricher import (
    EmbeddingBackend,
    EmbeddingRequest,
    EmbeddingResponse,
    embedding_cache_path,
)
from validators.knowledge.storage import canonical_json


class CachedEmbeddingBackend:
    """Cache wrapper using `EmbeddingRequest.content_hash` as the key."""

    def __init__(self, project_path: Path, backend: EmbeddingBackend) -> None:
        self.project_path = project_path.resolve()
        self.backend = backend

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Return a cached embedding, invalidating on model or dimension drift."""
        cached = self._read_cache(request)
        if cached is not None:
            return cached
        response = self.backend.embed(request)
        self._write_cache(request, response)
        return response

    def _read_cache(self, request: EmbeddingRequest) -> EmbeddingResponse | None:
        path = embedding_cache_path(self.project_path, request.content_hash)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        vector = tuple(float(item) for item in data.get("vector", []))
        dimensions = int(data.get("dimensions", len(vector)))
        if len(vector) != dimensions:
            return None
        expected_model = _backend_model(self.backend)
        if expected_model and data.get("model") != expected_model:
            return None
        expected_dimensions = _backend_dimensions(self.backend)
        if expected_dimensions is not None and dimensions != expected_dimensions:
            return None
        return EmbeddingResponse(
            vector=vector,
            dimensions=dimensions,
            tokens_used=int(data.get("tokens_used", 0)),
            model=str(data.get("model", "")),
        )

    def _write_cache(self, request: EmbeddingRequest, response: EmbeddingResponse) -> None:
        path = embedding_cache_path(self.project_path, request.content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            canonical_json(
                {
                    "content_hash": request.content_hash,
                    "dimensions": response.dimensions,
                    "model": response.model,
                    "tokens_used": response.tokens_used,
                    "vector": list(response.vector),
                }
            ),
            encoding="utf-8",
            newline="\n",
        )


def _backend_model(backend: Any) -> str:
    return str(getattr(backend, "model_name", "") or getattr(backend, "model", "") or "")


def _backend_dimensions(backend: Any) -> int | None:
    value = getattr(backend, "dimensions", None)
    return int(value) if value is not None else None
