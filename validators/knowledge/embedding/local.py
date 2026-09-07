"""Local sentence-transformers embedding backend."""

from __future__ import annotations

from typing import Any

from validators.knowledge.api import _estimate_tokens
from validators.knowledge.enricher import EmbeddingRequest, EmbeddingResponse


class LocalEmbeddingBackend:
    """Embedding backend backed by an optional sentence-transformers model."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", model: Any | None = None) -> None:
        self.model_name = model_name
        self._model = model
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "LocalEmbeddingBackend requires the optional "
                    "'sentence-transformers' dependency. Install it with "
                    "`pip install \"stackmind[embeddings]\"`."
                ) from exc
            self._model = SentenceTransformer(model_name)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Return a vector embedding for the request text."""
        encoded = self._model.encode([request.text])
        vector = _first_vector(encoded)
        return EmbeddingResponse(
            vector=vector,
            dimensions=len(vector),
            tokens_used=_estimate_tokens(request.text),
            model=self.model_name,
        )


def _first_vector(encoded: Any) -> tuple[float, ...]:
    first = encoded[0] if hasattr(encoded, "__getitem__") else encoded
    if hasattr(first, "tolist"):
        first = first.tolist()
    return tuple(float(item) for item in first)


