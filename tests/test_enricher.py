from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.enricher import (
    EmbeddingResponse,
    EnricherConfig,
    KnowledgeEnricher,
    SummaryResponse,
    embedding_cache_path,
    is_ai_only_delta,
    is_ai_stale,
)
from validators.knowledge.enricher_queue import EnricherQueue
from validators.knowledge.storage import read_ir
from validators.knowledge.writer import write_knowledge


class MockSummaryBackend:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.requests = []

    def summarize(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError('summary failed')
        return SummaryResponse(
            summary=f'summary for {request.qualified_name}',
            confidence=0.9,
            purpose='test',
            risk=None,
            tokens_used=7,
            model='mock-summary-v1',
        )


class MockEmbeddingBackend:
    def __init__(self):
        self.requests = []

    def embed(self, request):
        self.requests.append(request)
        return EmbeddingResponse(
            vector=(0.1, 0.2, 0.3),
            dimensions=3,
            tokens_used=5,
            model='mock-embedding-v1',
        )


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


def put(project, name, text):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def node_doc(project, node_id):
    nodes_root = project / '.sync' / 'knowledge' / 'nodes'
    path = next(nodes_root.glob(f'*/*/{node_id}.json'))
    return path, json.loads(path.read_text(encoding='utf-8'))


def target_node_id(project, qualified_name='target'):
    ir = read_ir(project)
    return next(symbol.node_id for symbol in ir.symbols if symbol.qualified_name == qualified_name)


def build_graph(project):
    write_knowledge(project, compile_project(project), built_at='fixed')
    from validators.knowledge.projections import build_projections

    build_projections(project)


def test_enricher_only_updates_ai_block(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)
    node_id = target_node_id(fresh_project)
    _, before = node_doc(fresh_project, node_id)

    summary = MockSummaryBackend()
    embedding = MockEmbeddingBackend()
    enricher = KnowledgeEnricher(
        fresh_project,
        summary,
        embedding,
        config=EnricherConfig(),
    )
    result = enricher.run_once()
    _, after = node_doc(fresh_project, node_id)

    assert result.processed >= 1
    assert is_ai_only_delta(before, after)
    assert after['ai']['enriched_hash'] == after['deterministic']['content_hash']
    assert after['ai']['summary'] == 'summary for target'
    assert after['ai']['privacy_mode'] == 'full'


def test_signatures_mode_strips_function_body(fresh_project):
    put(
        fresh_project,
        'app.py',
        'def target(value):\n    marker = "SECRET_BODY"\n    return value + 1\n',
    )
    build_graph(fresh_project)

    summary = MockSummaryBackend()
    embedding = MockEmbeddingBackend()
    enricher = KnowledgeEnricher(
        fresh_project,
        summary,
        embedding,
        config=EnricherConfig(privacy_mode='signatures'),
    )
    enricher.run_once()

    assert summary.requests
    target_request = next(
        request for request in summary.requests if request.qualified_name == 'target'
    )
    payload = target_request.content
    assert 'signature: def target(value)' in payload
    assert 'SECRET_BODY' not in payload
    assert 'source:' not in payload


def test_enricher_off_sends_nothing_and_pauses_queue(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)

    summary = MockSummaryBackend()
    embedding = MockEmbeddingBackend()
    enricher = KnowledgeEnricher(
        fresh_project,
        summary,
        embedding,
        config=EnricherConfig(privacy_mode='off'),
    )
    result = enricher.run_once()

    assert result.paused
    assert not summary.requests
    assert not embedding.requests
    assert EnricherQueue(fresh_project).snapshot()['pause_reason'] == 'privacy_mode_off'


def test_staleness_detected_after_incremental_change(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)
    node_id = target_node_id(fresh_project)

    enricher = KnowledgeEnricher(
        fresh_project,
        MockSummaryBackend(),
        MockEmbeddingBackend(),
        config=EnricherConfig(),
    )
    enricher.run_once()
    _, enriched = node_doc(fresh_project, node_id)
    assert not is_ai_stale(enriched)

    put(fresh_project, 'app.py', 'def target():\n    return 2\n')
    incremental_update(fresh_project)
    _, updated = node_doc(fresh_project, node_id)
    assert is_ai_stale(updated)


def test_queue_coalesces_latest_hash(fresh_project):
    queue = EnricherQueue(fresh_project)
    queue.enqueue('FUNC-aaaaaaaaaaaaaaaa', 'old-hash')
    queue.enqueue('FUNC-aaaaaaaaaaaaaaaa', 'new-hash')
    jobs = queue.load().jobs

    assert len(jobs) == 1
    assert jobs[0].content_hash == 'new-hash'
    assert jobs[0].attempts == 0


def test_budget_exhaustion_pauses_and_graph_stats_reports_it(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)

    enricher = KnowledgeEnricher(
        fresh_project,
        MockSummaryBackend(),
        MockEmbeddingBackend(),
        config=EnricherConfig(daily_call_cap=0),
    )
    result = enricher.run_once()
    runner = CliRunner()
    stats = runner.invoke(cli, ['graph', 'stats', '--project', str(fresh_project)])

    assert result.paused
    assert stats.exit_code == 0
    assert 'enrichment_paused: True' in stats.output
    assert 'enrichment_pause_reason: daily_call_cap_exhausted' in stats.output


def test_embedding_cache_hits_after_rename(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)
    node_id = target_node_id(fresh_project)
    summary = MockSummaryBackend()
    embedding = MockEmbeddingBackend()
    enricher = KnowledgeEnricher(
        fresh_project,
        summary,
        embedding,
        config=EnricherConfig(),
    )
    enricher.run_once()
    first_embed_calls = sum(1 for request in embedding.requests if request.node_id == node_id)

    put(fresh_project, 'app.py', 'def renamed_target():\n    return 1\n')
    incremental_update(fresh_project)
    renamed_id = target_node_id(fresh_project, qualified_name='renamed_target')
    path, node = node_doc(fresh_project, renamed_id)
    node['ai'] = {}
    path.write_text(json.dumps(node, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    EnricherQueue(fresh_project).enqueue(renamed_id, node['deterministic']['content_hash'])
    enricher.run_once()

    assert renamed_id == node_id
    assert (
        sum(1 for request in embedding.requests if request.node_id == renamed_id)
        == first_embed_calls
    )
    assert embedding_cache_path(fresh_project, node['deterministic']['content_hash']).exists()


def test_failures_backoff_then_park_without_touching_deterministic_block(fresh_project):
    put(fresh_project, 'app.py', 'def target():\n    return 1\n')
    build_graph(fresh_project)
    node_id = target_node_id(fresh_project)
    _, before = node_doc(fresh_project, node_id)
    enricher = KnowledgeEnricher(
        fresh_project,
        MockSummaryBackend(fail=True),
        MockEmbeddingBackend(),
        config=EnricherConfig(base_backoff_seconds=0.0),
    )

    enricher.run_once()
    enricher.run_once()
    enricher.run_once()
    _, after = node_doc(fresh_project, node_id)
    jobs = EnricherQueue(fresh_project).load().jobs

    assert before['deterministic'] == after['deterministic']
    assert any(job.status == 'parked' for job in jobs)
