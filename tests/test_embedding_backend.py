from __future__ import annotations

import json

import pytest

from cli.init import init
from cli.lock import acquire_lock, release_lock
from validators.knowledge.compiler.ir import CompilerIR, SymbolIR
from validators.knowledge.embedding import (
    CachedEmbeddingBackend,
    EmbeddingRequest,
    EmbeddingResponse,
    LocalEmbeddingBackend,
)
from validators.knowledge.enricher import embedding_cache_path
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.registry import SymbolRegistry
from validators.knowledge.storage import build_node_documents, canonical_json, node_path


class FakeSentenceModel:
    def encode(self, values):
        text = values[0]
        if "payments" in text:
            return [[1.0, 0.0, 0.0]]
        if "database" in text or "commit" in text:
            return [[0.0, 1.0, 0.0]]
        return [[0.0, 0.0, 1.0]]


class FakeBackend:
    def __init__(self, model_name: str = "fake-1", dimensions: int = 3) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.calls += 1
        if "database" in request.text or "commit" in request.text:
            vector = (0.0, 1.0, 0.0)
        elif "payments" in request.text:
            vector = (1.0, 0.0, 0.0)
        else:
            vector = (0.0, 0.0, 1.0)
        return EmbeddingResponse(
            vector=vector,
            dimensions=self.dimensions,
            tokens_used=1,
            model=self.model_name,
        )


class BrokenBackend:
    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raise RuntimeError("backend unavailable")


def _project(tmp_path):
    project = tmp_path / "project"
    init(project, name="Project", no_git=True)
    return project


def _request(content_hash: str = "abc123", text: str = "database commit") -> EmbeddingRequest:
    return EmbeddingRequest(
        node_id="node",
        content_hash=content_hash,
        privacy_mode="local",
        text=text,
    )


def _symbol(node_id: str, qualified_name: str, content_hash: str) -> SymbolIR:
    return SymbolIR(
        node_id=node_id,
        kind="Function",
        path="app.py",
        qualified_name=qualified_name,
        signature=f"def {qualified_name}()",
        location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
        content_hash=content_hash,
    )


def test_local_embedding_backend_uses_sentence_model_dimensions():
    backend = LocalEmbeddingBackend(model_name="fake-local", model=FakeSentenceModel())

    response = backend.embed(_request(text="database commit"))

    assert response.vector == (0.0, 1.0, 0.0)
    assert response.dimensions == 3
    assert response.model == "fake-local"
    assert response.tokens_used > 0


def test_local_embedding_backend_import_error_is_clear(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match="sentence-transformers"):
        LocalEmbeddingBackend()


def test_cached_embedding_backend_miss_then_hit(tmp_path):
    project = _project(tmp_path)
    backend = FakeBackend()
    cached = CachedEmbeddingBackend(project, backend)
    request = _request(content_hash="feedface")

    first = cached.embed(request)
    second = cached.embed(request)

    assert first == second
    assert backend.calls == 1
    cache_path = embedding_cache_path(project, "feedface")
    assert cache_path.exists()
    assert json.loads(cache_path.read_text(encoding="utf-8"))["model"] == "fake-1"


def test_cached_embedding_backend_uses_content_hash_stability(tmp_path):
    project = _project(tmp_path)
    backend = FakeBackend()
    cached = CachedEmbeddingBackend(project, backend)

    cached.embed(_request(content_hash="samehash", text="database commit"))
    cached.embed(_request(content_hash="samehash", text="payments flow"))

    assert backend.calls == 1


def test_cached_embedding_backend_invalidates_on_model_change(tmp_path):
    project = _project(tmp_path)
    backend = FakeBackend(model_name="fake-1")
    cached = CachedEmbeddingBackend(project, backend)
    request = _request(content_hash="change-model")

    cached.embed(request)
    backend.model_name = "fake-2"
    cached.embed(request)

    assert backend.calls == 2
    data = json.loads(embedding_cache_path(project, "change-model").read_text(encoding="utf-8"))
    assert data["model"] == "fake-2"


def test_cached_embedding_backend_invalidates_on_dimension_change(tmp_path):
    project = _project(tmp_path)
    backend = FakeBackend(dimensions=3)
    cached = CachedEmbeddingBackend(project, backend)
    request = _request(content_hash="change-dim")

    cached.embed(request)
    backend.dimensions = 2
    cached.embed(request)

    assert backend.calls == 2


def test_knowledge_api_search_falls_back_when_backend_unavailable(tmp_path, monkeypatch):
    project = _project(tmp_path)

    def fake_search_symbols(project_path, query, *, limit):
        return [{"node_id": "FUNC-1111111111111111", "score": 1}]

    monkeypatch.setattr("validators.knowledge.api.search_symbols", fake_search_symbols)
    api = KnowledgeAPI(project, embedding_backend=BrokenBackend())
    monkeypatch.setattr(
        api,
        "_lookup_node_by_id",
        lambda node_id, contract=None: {
            "node_id": node_id,
            "kind": "Function",
            "deterministic": {
                "path": "app.py",
                "qualified_name": "fallback",
                "signature": "def fallback()",
            },
            "ai": {},
        },
    )

    envelope = api.search("fallback", limit=1)

    assert not envelope.semantic
    assert envelope.results[0].qualified_name == "fallback"


def test_knowledge_api_search_uses_backend_query_embedding(tmp_path):
    project = _project(tmp_path)
    registry = SymbolRegistry(project, agent="codex")
    acquire_lock(project / ".sync", "codex", session_id=1)
    try:
        payments_record = registry.get_or_create(
            kind="Function",
            path="app.py",
            qualified_name="payments_flow",
        )
        database_record = registry.get_or_create(
            kind="Function",
            path="app.py",
            qualified_name="database_commit",
        )
    finally:
        release_lock(project / ".sync", "codex")
    payments = _symbol(payments_record.node_id, "payments_flow", "hash-payments")
    database = _symbol(database_record.node_id, "database_commit", "hash-database")

    for node_id, document in build_node_documents(
        CompilerIR(revision_inputs={}, symbols=[payments, database], edges=[])
    ).items():
        symbol = payments if node_id == payments.node_id else database
        path = node_path(project, symbol.kind, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(document), encoding="utf-8")

    backend = FakeBackend()
    CachedEmbeddingBackend(project, backend).embed(
        _request(content_hash="hash-payments", text="payments flow")
    )
    CachedEmbeddingBackend(project, backend).embed(
        _request(content_hash="hash-database", text="database commit")
    )

    envelope = KnowledgeAPI(project, embedding_backend=FakeBackend()).search("database commit", limit=1)

    assert envelope.semantic
    assert envelope.results[0].qualified_name == "database_commit"
    assert envelope.results[0].reason == "semantic-search"
