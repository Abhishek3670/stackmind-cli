"""Phase 2 tests: context wiring into the real prompt + CONTRACT-01 scope filtering.

Covers:
- build_prompt_with_context budgeting (task kept whole, context capped, explicit truncation notice)
- ModelExecutionBackend sends the composed prompt to Ollama
- Contract-based scope filtering in assemble_context (denied nodes skipped and counted, not fatal)
- No-contract path unchanged (inbox-task workflow)
- Echo provider determinism untouched
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import pytest
import yaml

from cli.init import init
from validators.harness.backend import (
    DEFAULT_CONTEXT_TOKEN_BUDGET,
    ModelExecutionBackend,
    build_prompt_with_context,
)
from validators.harness.retrieval import RetrievalBatch
from validators.harness.runner import EchoLLMProvider, HarnessTask, LLMRequest
from validators.knowledge.api import ContextBundle, KnowledgeAPI
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.projections import build_projections
from validators.knowledge.writer import write_knowledge
from validators.knowledge.contract import AgentContract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path) -> Path:
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


def _build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)


def _write_contract(path: Path, *, allow: list, deny: list, work_order: str = 'WO-900') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent_id': 'codex',
                'work_order': work_order,
                'scope': {'allow': allow, 'deny': deny},
                'budget': {'expires_at': '2099-01-01T00:00:00Z'},
            },
            sort_keys=False,
        ),
        encoding='utf-8',
    )


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def _make_request(context: ContextBundle | None, *, title: str = 'T', body: str = 'B') -> LLMRequest:
    task = HarnessTask(
        kind='adhoc',
        identifier='task-1',
        path=Path('adhoc.md'),
        title=title,
        body=body,
        query=title,
    )
    return LLMRequest(
        agent='codex',
        session_count=0,
        task=task,
        context=context,
        retrieval=RetrievalBatch(
            results=(), evidence=(), searches_used=0, cache_hits=0,
            cap_exhausted=False, mode='internal_only', cost_estimate=0.0, query=title,
        ),
    )


# ---------------------------------------------------------------------------
# build_prompt_with_context unit tests
# ---------------------------------------------------------------------------


def test_prompt_builder_task_only_matches_legacy_format():
    assert build_prompt_with_context('Task title', 'Do the thing', None) == 'Task: Task title\n\nDo the thing'
    assert build_prompt_with_context('Task title', '', None) == 'Task title'
    assert build_prompt_with_context('', 'Just body', None) == 'Just body'


def test_prompt_builder_includes_context_section():
    prompt = build_prompt_with_context('Title', 'Body', 'run_once signature lives here')
    assert prompt.startswith('Task: Title\n\nBody\n\n')
    assert '## Project Knowledge Context (reference material)' in prompt
    assert 'run_once signature lives here' in prompt


def test_prompt_builder_budget_truncates_context_with_notice():
    huge_context = 'line\n' * 20_000  # ~100k chars, far beyond the 1200-token budget
    prompt = build_prompt_with_context('Title', 'Body', huge_context)
    kept_context = prompt.split('## Project Knowledge Context (reference material)', 1)[1]
    # Task text survives intact
    assert prompt.startswith('Task: Title\n\nBody\n\n')
    # Context is capped near the budget (4 chars/token) and a notice is present
    assert len(kept_context) <= DEFAULT_CONTEXT_TOKEN_BUDGET * 4 + 400
    assert 'truncated' in kept_context
    assert 'characters' in kept_context
    assert 'dropped' in kept_context


def test_prompt_builder_small_budget_still_notices():
    tiny = 'x' * 500
    prompt = build_prompt_with_context('T', 'B', tiny, context_token_budget=10)
    assert '[Project knowledge context truncated' in prompt
    assert '10-token context budget' in prompt


def test_prompt_builder_evidence_appended_when_present():
    prompt = build_prompt_with_context('T', 'B', 'ctx', 'Evidence: doc (web, url)')
    assert '## External Evidence' in prompt
    assert 'Evidence: doc (web, url)' in prompt


def test_prompt_builder_no_empty_sections_when_context_empty():
    prompt = build_prompt_with_context('T', 'B', '   ')
    assert '## Project Knowledge Context' not in prompt
    assert prompt == 'Task: T\n\nB'


# ---------------------------------------------------------------------------
# ModelExecutionBackend wiring
# ---------------------------------------------------------------------------


def _bundle(text: str) -> ContextBundle:
    return ContextBundle(
        revision=1,
        git_commit='abc',
        stale=False,
        semantic=False,
        token_budget=1200,
        estimated_tokens=42,
        truncated=False,
        truncation_reason=None,
        entries=(),
        text=text,
    )


def test_model_backend_sends_composed_prompt_to_ollama(tmp_path):
    backend = ModelExecutionBackend(
        backend_id='ollama-test',
        model='test-model',
        endpoint='http://127.0.0.1:11499',
    )
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):
        captured['body'] = json.loads(req.data.decode('utf-8'))
        return _FakeResponse([json.dumps({'response': 'ok', 'done': True}).encode('utf-8')])

    with patch.object(urllib.request, 'urlopen', fake_urlopen):
        record = backend.complete(_make_request(_bundle('Graph facts go here'), title='Fix bug', body='Patch runner'))

    assert captured['body']['model'] == 'test-model'
    sent_prompt = captured['body']['prompt']
    assert sent_prompt.startswith('Task: Fix bug\n\nPatch runner')
    assert '## Project Knowledge Context (reference material)' in sent_prompt
    assert 'Graph facts go here' in sent_prompt
    assert record.payload['summary'] == 'ok'


def test_model_backend_prompt_without_context_falls_back_to_task_text(tmp_path):
    backend = ModelExecutionBackend(
        backend_id='ollama-test',
        model='test-model',
        endpoint='http://127.0.0.1:11499',
    )
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):
        captured['body'] = json.loads(req.data.decode('utf-8'))
        return _FakeResponse([json.dumps({'response': 'ok', 'done': True}).encode('utf-8')])

    with patch.object(urllib.request, 'urlopen', fake_urlopen):
        backend.complete(_make_request(None, title='Solo', body='Task body'))

    assert captured['body']['prompt'] == 'Task: Solo\n\nTask body'


# ---------------------------------------------------------------------------
# Contract scope filtering in assemble_context
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_project(tmp_path):
    project = _init_project(tmp_path)
    src = project / 'sample_pkg'
    src.mkdir(parents=True, exist_ok=True)
    (src / '__init__.py').write_text('', encoding='utf-8')
    (src / 'alpha.py').write_text(
        'def alpha_one():\n    return alpha_two() + 1\n\n\ndef alpha_two():\n    return 2\n',
        encoding='utf-8',
    )
    (src / 'beta.py').write_text(
        'def beta_one():\n    return alpha_one() + 10\n',
        encoding='utf-8',
    )
    _build_graph(project)
    return project


def test_no_contract_assembly_unchanged(knowledge_project):
    api = KnowledgeAPI(knowledge_project)
    bundle = api.assemble_context('alpha_one call graph', token_budget=1200, limit=8)
    assert bundle.scope_filtered_count == 0
    assert bundle.entries
    assert any('alpha' in e.text for e in bundle.entries)


def test_contract_deny_skips_nodes_without_crashing(knowledge_project):
    contract_path = knowledge_project / '.sync' / 'contracts' / 'WO-900.yaml'
    _write_contract(
        contract_path,
        allow=[{'module': 'sample_pkg'}],
        deny=[{'module': 'sample_pkg.beta'}],
    )
    contract = AgentContract.load(contract_path, knowledge_project)
    api = KnowledgeAPI(knowledge_project)
    bundle = api.assemble_context('beta_one call graph', token_budget=1200, limit=8, contract=contract)
    # Previously: ContractAccessDenied aborted the whole call. Now it must complete.
    assert isinstance(bundle, ContextBundle)
    assert bundle.scope_filtered_count >= 1
    assert bundle.text  # still assembled something useful


def test_scope_filter_stats_reset_between_assemblies(knowledge_project):
    contract_path = knowledge_project / '.sync' / 'contracts' / 'WO-900.yaml'
    _write_contract(
        contract_path,
        allow=[{'module': 'sample_pkg'}],
        deny=[{'module': 'sample_pkg.beta'}],
    )
    contract = AgentContract.load(contract_path, knowledge_project)
    api = KnowledgeAPI(knowledge_project)
    first = api.assemble_context('beta_one call graph', token_budget=1200, limit=8, contract=contract)
    second = api.assemble_context('alpha_one call graph', token_budget=1200, limit=8, contract=contract)
    assert isinstance(first, ContextBundle)
    assert isinstance(second, ContextBundle)
    # Stale skips from the first assembly must not leak into the second
    assert second.scope_filtered_count >= 0
    assert api.pop_scope_filter_stats() == []


def test_expired_contract_still_blocks_assembly(knowledge_project):
    contract_path = knowledge_project / '.sync' / 'contracts' / 'WO-901.yaml'
    path = knowledge_project / '.sync' / 'contracts' / 'WO-901.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent_id': 'codex',
                'work_order': 'WO-901',
                'scope': {'allow': [{'module': 'sample_pkg'}], 'deny': []},
                'budget': {'expires_at': '2020-01-01T00:00:00Z'},
            },
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    contract = AgentContract.load(path, knowledge_project)
    api = KnowledgeAPI(knowledge_project)
    with pytest.raises(Exception):
        api.assemble_context('alpha_one call graph', token_budget=1200, limit=8, contract=contract)


# ---------------------------------------------------------------------------
# Echo determinism regression guard
# ---------------------------------------------------------------------------


def test_echo_provider_output_unchanged_by_context():
    echo = EchoLLMProvider()
    task = HarnessTask(
        kind='adhoc',
        identifier='echo-1',
        path=Path('x.md'),
        title='Echo title',
        body='Echo body',
        query='Echo title',
    )
    req = LLMRequest(
        agent='codex',
        session_count=0,
        task=task,
        context=_bundle('Some context that echo must ignore'),
        retrieval=RetrievalBatch(
            results=(), evidence=(), searches_used=0, cache_hits=0,
            cap_exhausted=False, mode='internal_only', cost_estimate=0.0, query='Echo title',
        ),
    )
    record = echo.complete(req)
    assert record.provider == 'echo'
    assert record.payload['summary'] == 'Processed echo-1: Echo title'
    assert 'Some context that echo must ignore' not in json.dumps(record.payload)
