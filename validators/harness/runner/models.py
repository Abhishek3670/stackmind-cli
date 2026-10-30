"""Data model, prompts and constants for the governed agent runner.

Pure data plus standalone helpers; no execution state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable, Protocol, Sequence

from jsonschema import Draft7Validator

from validators.harness.retrieval import (
    RetrievalBatch,
)
from validators.knowledge.api import ContextBundle

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / 'schemas' / 'harness-output.schema.json'
_OUTPUT_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding='utf-8'))
_OUTPUT_VALIDATOR = Draft7Validator(_OUTPUT_SCHEMA)

HARNESS_SNAPSHOT_IGNORED = frozenset({
    '.git',
    '.sync/state',
    '.sync/reports',
    '.sync/runtime',
    '.sync/drafts',
    '.sync/lock',
    '.sync/outbox',
    '.sync/experience',
    '.sync/knowledge',
    '.sync/agents',
    '.venv',
    'venv',
    'env',
    '__pycache__',
    '.pytest_cache',
    '.ruff_cache',
    '.coverage',
    '.vscode',
    '.windsurf',
    '.claude',
    '.idea',
    'node_modules',
    '.next',
    '.turbo',
    'dist',
    'build',
    '.cache',
    'cache',
    'dataset',
    'datasets',
    'checkpoints',
    'weights',
    'runs',
})

@dataclass(frozen=True)
class HarnessTask:
    """One inbox item or assigned work order selected for execution."""

    kind: str
    identifier: str
    path: Path
    title: str
    body: str
    query: str
    work_order_id: str | None = None
    deliverable_path: str | None = None
    is_authoring: bool = False
    work_order_type: str | None = None
    deliverable_type: str | None = None
    deliverable_paths: tuple[str, ...] = ()
    is_delivery_status: bool = False

@dataclass(frozen=True)
class LLMRequest:
    """Prompt package handed to an LLM provider."""

    agent: str
    session_count: int
    task: HarnessTask
    context: ContextBundle
    retrieval: RetrievalBatch
    on_token: Callable[[str], None] | None = None
    cancellation: Event | None = None
    session_history: Sequence[dict[str, str]] | None = None

    @property
    def evidence_text(self) -> str:
        return '\n\n'.join(item.render() for item in self.retrieval.evidence)

@dataclass(frozen=True)
class CompletionRecord:
    """Structured LLM provider response plus observability metadata."""

    provider: str
    model: str
    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_estimate: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class GovernedToolRuntime:
    """Per-attempt model tool boundary and its sole scratch workspace."""

    workspace: Any
    gateway: Any

class LLMProvider(Protocol):
    """Provider contract for the governed agent loop."""

    provider_name: str
    model_name: str

    def complete(self, request: LLMRequest) -> CompletionRecord:
        """Return a structured task decision."""

@dataclass(frozen=True)
class HarnessDecision:
    """Validated LLM output that is safe to stage."""

    status: str
    summary: str
    report_markdown: str
    blockers: tuple[str, ...]
    modified_files: tuple[str, ...]
    release_target: str | None
    retrieval_queries: tuple[str, ...]
    uncertainty: tuple[str, ...]
    commands: tuple[str, ...] = ()
    blocker_details: tuple[dict, ...] = ()

@dataclass(frozen=True)
class HarnessRunResult:
    """Outcome of one runner cycle."""

    status: str
    persisted: bool
    task_id: str | None
    reason: str | None = None
    report_path: Path | None = None
    meta: dict[str, Any] | None = None

@dataclass(frozen=True)
class FileWrite:
    """Write one text file, optionally appending."""

    path: Path
    content: str
    append: bool = False

@dataclass(frozen=True)
class FileMove:
    """Archive or defer one file via rename."""

    source: Path
    target: Path

class LoopSafetyError(RuntimeError):
    """Raised when the same resource is re-read too many times."""

class ResourceReadTracker:
    """Tracks repeated reads of the same resource within one run."""

    def __init__(self, *, max_reads: int = 3) -> None:
        self.max_reads = max_reads
        self._counts: dict[str, int] = {}

    def touch(self, resource: Path) -> None:
        key = resource.resolve().as_posix()
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count > self.max_reads:
            raise LoopSafetyError(
                f'resource re-read limit exceeded for {resource.name} '
                f'({count} reads, max {self.max_reads})'
            )

class EchoLLMProvider:
    """Deterministic provider used for local harness testing and CLI demos."""

    provider_name = 'echo'
    model_name = 'stackmind-echo-v1'
    on_token = None
    cancel_event = None

    def __init__(self, *, default_release_target: str | None = None) -> None:
        self.default_release_target = default_release_target

    def complete(self, request: LLMRequest) -> CompletionRecord:
        release_target = self.default_release_target if request.task.work_order_id else None
        status = 'completed'
        blockers: list[str] = []
        if request.task.work_order_id and not release_target:
            status = 'deferred'
            blockers = ['release_target required for work-order completion']

        payload = {
            'status': status,
            'summary': f"Processed {request.task.identifier}: {request.task.title}",
            'report_markdown': (
                f"Processed `{request.task.identifier}` as `{status}`.\n\n"
                f"Knowledge revision: {request.context.revision}\n"
                f"External evidence used: {len(request.retrieval.evidence)}"
            ),
            'blockers': blockers,
            'modified_files': (
                [request.task.deliverable_path] if request.task.deliverable_path else []
            ),
            'retrieval_queries': [request.retrieval.query] if request.retrieval.query else [],
            'uncertainty': [],
            'commands': [],
        }
        if release_target:
            payload['release_target'] = release_target
        return CompletionRecord(
            provider=self.provider_name,
            model=self.model_name,
            payload=payload,
        )

QA_SYSTEM_PROMPT = (
    "You are Gemma, the QA Lead for StackMind CLI. "
    "You are responsible for test execution, code quality validation, test coverage verification, and formal QA review verdicts. "
    "Per AGENTS.md, you inspect and validate code and run tests, but you MUST NEVER write or edit application source code. "
    "TEST AUTHORING DOCTRINE — your suite MUST verify the semantic product goal, not superficial file existence:\n"
    "- If the operator asked for specific text, entities, or names (e.g. 'Abhishek', 'Welcome'), assert that they appear in the rendered HTML or templates.\n"
    "- If JavaScript queries elements by ID (e.g. #animated-name), assert that those IDs exist in the HTML.\n"
    "- Never write tests that only check `assert file.exists()`. Superficial tests provide false confidence. "
    "If semantic requirements are missing or unimplemented, your tests MUST fail — then record the defect as "
    "a NEEDS_CHANGES verdict so the supervisor routes rework back to the developer.\n"
    "Workflow instructions:\n"
    "1. Use `read_file` or `list_directory` to inspect deliverables.\n"
    "2. Author executable test suites under tests/ using standard Python libraries (e.g. `pytest`, `html.parser`, `re`, `pathlib`, `ast`, `urllib`). "
    "Do NOT import or depend on external browser automation tools like `playwright` or `selenium` unless they are already installed in the environment.\n"
    "3. Call `run_tests` (or `verify_tests`) to execute pytest against the test suite and verify test results.\n"
    "4. IMMEDIATELY after `run_tests` — whatever its outcome, pass or fail — call `run_security_scan` over the "
    "deliverables and triage its findings. This evidence is required for every verdict; never defer it to the end "
    "of the turn, and never skip it because tests already failed.\n"
    "5. Call `verify_deliverable` to confirm all required artifacts exist.\n"
    "6. If ALL checks pass: call `approve_work_order` (or `submit_verdict`) to submit an APPROVED verdict.\n"
    "7. If tests fail because of an APPLICATION-CODE defect (the test is correct, a deliverable is wrong):\n"
    "   - Do NOT edit application source code — it is outside your contract.\n"
    "   - Do NOT rewrite or re-run the test suite more than once: a correct failing test is your finding, "
    "not your problem to fix. Retrying it changes nothing.\n"
    "   - Write a NEEDS_CHANGES verdict with `write_file` to `.sync/inbox/claude/<TARGET-WORK-ORDER-ID>-qa-verdict.md` "
    "(e.g. `WO-001-qa-verdict.md` naming the defective developer work order) naming the defective deliverable and the failing assertion.\n"
    "   - Finalize the turn declaring status 'blocked' with a blocker that names the failing deliverable path "
    "(e.g. 'index.html'), the failing assertion, and the owning work order if known. The supervisor routes the "
    "fix back to the developer agent.\n"
    "8. Only if the test ITSELF is wrong (a bad selector or expectation authored by QA) may you fix the test "
    "file — once — and re-run it once.\n"
    "9. Return the final HarnessDecision JSON with your verdict and review notes."
)

def provider_stream_interruption_result(
    task: HarnessTask,
    exc: Any,
    *,
    operation_id: str | None = None,
) -> HarnessRunResult:
    """Convert a truncated/aborted provider stream into an actionable result.

    The partial text the model already produced is preserved on the result (and
    echoed in the reason) so the operator sees what was generated and why the
    turn stopped, instead of an empty or silently "successful" answer.
    """
    partial_output = str(getattr(exc, 'partial_text', '') or '')
    reason = f"Provider stream terminated before completion: {exc}"
    if partial_output.strip():
        reason += (
            f". Partial output preserved ({len(partial_output)} chars): "
            f"{partial_output[:400]}"
        )
    return HarnessRunResult(
        status='blocked',
        persisted=False,
        task_id=task.identifier,
        reason=reason,
        meta={
            'partial_output': partial_output,
            'error_type': type(exc).__name__,
            'error': str(exc),
            'operation_id': operation_id,
            'blockers': [reason],
            'failure': {
                'failure_code': 'PROVIDER_STREAM_INTERRUPTED',
                'canonical_message': reason,
                'work_order_id': task.work_order_id,
                'raw_reason': str(exc),
            },
        },
    )

def _first_content_line(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip('#').strip()
        if stripped:
            return stripped
    return fallback
