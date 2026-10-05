"""Governed agent execution loop for StackMind Phase 8."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable, Protocol

import yaml
from jsonschema import Draft7Validator

from cli.lock import acquire_lock, release_lock
from cli.validate import validate as validate_runtime
from validators.knowledge.contract import ContractAccessDenied, ContractExpiredError
from validators.harness.retrieval import (
    RetrievalBatch,
    RetrievalPolicy,
    SearchProvider,
    SessionSearchTool,
)
from validators.knowledge.api import ContextBundle, KnowledgeAPI

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / 'schemas' / 'harness-output.schema.json'
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
    '__pycache__',
    '.pytest_cache',
    '.ruff_cache',
    '.coverage',
    '.vscode',
    '.windsurf',
    '.claude',
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


class AgentRunner:
    """Governed worker execution loop with staged verification."""

    def __init__(
        self,
        project_path: Path,
        agent: str,
        *,
        backend: Any | None = None,
        llm_provider: LLMProvider | None = None,
        provider_adapter: Any | None = None,
        search_provider: SearchProvider | None = None,
        retrieval_policy: RetrievalPolicy | None = None,
        token_budget: int = 1200,
        context_limit: int = 8,
        max_tool_calls: int | None = None,
        max_turns: int | None = None,
        max_lock_retries: int = 3,
        backoff_seconds: float = 0.1,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.sync_path = self.project_path / '.sync'
        self.agent = agent
        self.backend = backend
        self.provider_adapter = provider_adapter
        if backend is not None:
            self.backend_id = getattr(backend, 'backend_id', 'custom-backend')
            self.backend_model = getattr(backend, 'model', None) or 'default'
            if llm_provider is not None:
                self.llm_provider = llm_provider
            elif hasattr(backend, 'complete'):
                self.llm_provider = backend
            else:
                self.llm_provider = EchoLLMProvider()
        else:
            self.llm_provider = llm_provider or EchoLLMProvider()
            self.backend_id = getattr(self.llm_provider, 'provider_name', 'echo')
            self.backend_model = getattr(self.llm_provider, 'model_name', 'stackmind-echo-v1')
        # Native provider adapters use ProviderGateway.  Keep the established
        # LLMProvider protocol intact for deterministic legacy providers.
        if self.provider_adapter is None:
            from validators.kernel.providers.adapter import ProviderAdapter
            if isinstance(self.llm_provider, ProviderAdapter):
                self.provider_adapter = self.llm_provider
        self.search_tool = SessionSearchTool(search_provider, policy=retrieval_policy)
        self.token_budget = token_budget
        self.context_limit = context_limit
        from validators.kernel.providers.gateway import DEFAULT_MAX_TURNS
        self.max_tool_calls = max_tool_calls
        self.max_turns = max_turns if max_turns is not None else DEFAULT_MAX_TURNS
        self.max_lock_retries = max_lock_retries
        self.backoff_seconds = backoff_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc).astimezone())
        self.sleep_fn = sleep_fn or time.sleep
        self.reader = ResourceReadTracker(max_reads=3)
        self.on_token = on_token
        self._tree_cache: dict[str, Any] | None = None

    @staticmethod
    def _is_cancelled(cancellation: Event | None) -> bool:
        return cancellation is not None and cancellation.is_set()

    @staticmethod
    def _cancelled_result(task: HarnessTask, operation_id: str | None) -> HarnessRunResult:
        return HarnessRunResult(
            status='cancelled',
            persisted=False,
            task_id=task.identifier,
            reason='operation cancelled',
            meta={'operation_id': operation_id} if operation_id else None,
        )

    def run_once(
        self,
        cancellation: Event | None = None,
        operation_id: str | None = None,
        *,
        cancel_event: Event | None = None,
        prompt: str | None = None,
        work_order_id: str | None = None,
        is_authoring: bool = False,
    ) -> HarnessRunResult:
        """Run one task, cooperatively stopping at operation lifecycle boundaries."""
        if cancel_event is not None:
            if cancellation is not None and cancellation is not cancel_event:
                raise ValueError("only one cancellation event may be provided")
            cancellation = cancel_event
        try:
            tree_data = self._load_tree()
            self._ensure_protocol_citizenship(tree_data)
            task = self.discover_next_task(tree_data, work_order_id=work_order_id)
            if task is not None and is_authoring:
                task = HarnessTask(
                    kind=task.kind,
                    identifier=task.identifier,
                    path=task.path,
                    title=task.title,
                    body=task.body,
                    query=task.query,
                    work_order_id=task.work_order_id,
                    deliverable_path=None,
                    is_authoring=True,
                    work_order_type=task.work_order_type,
                    deliverable_type=task.deliverable_type,
                )
            if task is None and prompt:
                adhoc_file = self.sync_path / 'inbox' / self.agent / 'adhoc.md'
                task = HarnessTask(
                    kind='adhoc',
                    identifier=operation_id or 'adhoc',
                    path=adhoc_file,
                    title=prompt.strip() or 'User Prompt',
                    body=prompt.strip() or 'User Prompt',
                    query=prompt.strip() or 'User Prompt',
                    is_authoring=is_authoring,
                    work_order_type=None,
                    deliverable_type=None,
                )
            if task is None:
                return HarnessRunResult(
                    status='idle',
                    persisted=False,
                    task_id=None,
                    reason='no inbox items or assigned work orders',
                )

            if task is not None and prompt and prompt.strip():
                p_str = prompt.strip()
                if p_str != task.title.strip() and p_str != task.body.strip():
                    task = HarnessTask(
                        kind=task.kind,
                        identifier=task.identifier,
                        path=task.path,
                        title=task.title,
                        body=f"{task.body}\n\nTurn Instructions:\n{p_str}" if task.body else p_str,
                        query=task.query,
                        work_order_id=task.work_order_id,
                        deliverable_path=task.deliverable_path,
                        is_authoring=task.is_authoring,
                        work_order_type=task.work_order_type,
                        deliverable_type=task.deliverable_type,
                    )

            # 1. Post-task discovery.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            run_at = self.now_fn()
            poll_started = time.monotonic()
            # CONTRACT-01: assemble context under the agent's active contract so
            # scope filtering is genuinely applied to retrieved knowledge. For
            # inbox/adhoc tasks (no work_order_id) load_harness_contract falls
            # back to the per-agent contract (.sync/agents/<agent>.contract.yaml)
            # and returns None when neither exists — assemble_context treats
            # None exactly as before (unfiltered), so this never blocks the turn.
            from validators.harness.contract_gate import load_harness_contract
            task_contract = load_harness_contract(
                self.project_path, self.agent, task.work_order_id
            )
            knowledge_api = KnowledgeAPI(self.project_path)
            try:
                context = knowledge_api.assemble_context(
                    task.query,
                    token_budget=self.token_budget,
                    limit=self.context_limit,
                    contract=task_contract,
                )
            except (ContractAccessDenied, ContractExpiredError) as contract_error:
                # Fail closed, gracefully: the pre-execution gate would block
                # this task anyway; surface the same governance failure without
                # masking it as an infrastructure crash.
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=f'Pre-execution contract validation failed: {contract_error}',
                )
            scope_skips = knowledge_api.pop_scope_filter_stats()
            poll_ms = int((time.monotonic() - poll_started) * 1000)

            # 2. Post-context assembly.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            # Pre-execution plan verification
            from validators.harness.contract_gate import verify_pre_execution
            from validators.harness.d024_gate import D024ViolationError
            try:
                verify_pre_execution(self.project_path, self.agent, task, context)
            except D024ViolationError as d024_exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=str(d024_exc),
                )
            except Exception as exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=f'Pre-execution contract validation failed: {exc}',
                )

            # 3. Post-pre-execution contract verification.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            from validators.harness.snapshot import (
                WorkspaceSnapshot,
                WorkspaceDiff,
                VerificationDimensions,
                TrustLevel,
                evaluate_learning_eligibility,
            )

            # Phase A: model-facing tools and all subsequent observation share
            # exactly one scratch workspace.  The live project is never passed
            # to a model-facing tool.
            tool_runtime = self._build_tool_runtime(task_contract, task, knowledge_api)
            workspace_root = (
                tool_runtime.workspace.root if tool_runtime is not None else self.project_path
            )
            before_snapshot = WorkspaceSnapshot.capture(
                workspace_root, ignored_patterns=HARNESS_SNAPSHOT_IGNORED,
            )

            # 4. Post-workspace snapshot.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            retrieval_started = time.monotonic()
            retrieval = self.search_tool.search(task.query, limit=3)
            retrieval_ms = int((time.monotonic() - retrieval_started) * 1000)

            # 5. Post-retrieval, before provider execution.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            request = LLMRequest(
                agent=self.agent,
                session_count=int(tree_data['agents'][self.agent].get('session_count', 0)),
                task=task,
                context=context,
                retrieval=retrieval,
                on_token=self.on_token,
                cancellation=cancellation,
            )

            llm_started = time.monotonic()
            try:
                if self.backend is not None and hasattr(self.backend, 'start_operation'):
                    self.backend.start_operation(task=task, context=context, operation_id=operation_id)
                if self.backend is not None and hasattr(self.backend, 'on_token'):
                    self.backend.on_token = self.on_token
                if self.backend is not None and hasattr(self.backend, 'cancel_event'):
                    self.backend.cancel_event = cancellation
                completion = self._complete_request(request, tool_runtime)
                if self.backend is not None and hasattr(self.backend, 'report_result') and operation_id:
                    self.backend.report_result(operation_id)
            except Exception as exc:
                from validators.kernel.providers.errors import (
                    BudgetExceededError,
                    ConsecutiveToolFailureError,
                    NoProgressLoopError,
                    OperationCancelledError,
                    TimeoutError,
                    ToolLimitExceededError,
                    ToolLoopExhaustedError,
                )
                import builtins
                import socket
                if isinstance(exc, OperationCancelledError) or self._is_cancelled(cancellation):
                    return self._cancelled_result(task, operation_id)
                if isinstance(exc, (
                    BudgetExceededError,
                    ToolLimitExceededError,
                    ToolLoopExhaustedError,
                    NoProgressLoopError,
                    ConsecutiveToolFailureError,
                    TimeoutError,
                    builtins.TimeoutError,
                    socket.timeout,
                )):
                    return HarnessRunResult(
                        status='blocked',
                        persisted=False,
                        task_id=task.identifier,
                        reason=f"Governed tool loop halted: {exc}",
                        meta={
                            'error_type': type(exc).__name__,
                            'error': str(exc),
                        },
                    )
                return HarnessRunResult(
                    status='failed',
                    persisted=False,
                    task_id=task.identifier,
                    reason=f'Backend execution error: {exc}',
                    meta={
                        'backend_id': self.backend_id,
                        'model': self.backend_model,
                        'operation_id': operation_id,
                        'error': str(exc),
                    },
                )
            llm_ms = completion.latency_ms or int((time.monotonic() - llm_started) * 1000)

            # 6. Post-provider completion.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            # Streamline conversational adhoc turns (pure chat with no files and no commands)
            if task.kind == 'adhoc':
                has_modifications = bool(completion.payload.get('modified_files'))
                has_commands = bool(completion.payload.get('commands'))
                if not has_modifications and not has_commands:
                    from cli.tui.chat import extract_internal_reasoning, strip_internal_reasoning
                    raw_summary = strip_internal_reasoning(str(completion.payload.get('summary') or ''))
                    raw_report = strip_internal_reasoning(str(completion.payload.get('report_markdown') or ''))
                    raw_thinking = (
                        completion.payload.get('thinking')
                        or completion.payload.get('reasoning')
                        or completion.payload.get('thought')
                        or completion.payload.get('reasoning_content')
                        or extract_internal_reasoning(str(completion.payload.get('summary') or ''))
                        or extract_internal_reasoning(str(completion.payload.get('report_markdown') or ''))
                    )
                    if not raw_summary and not raw_report:
                        return HarnessRunResult(
                            status='blocked',
                            persisted=False,
                            task_id=task.identifier,
                            reason='verification gate failed: outcome_verified',
                        )

                    outbox_dir = self.sync_path / 'outbox' / self.agent
                    outbox_dir.mkdir(parents=True, exist_ok=True)
                    now_str = run_at.isoformat().replace(':', '-')
                    out_path = outbox_dir / f'harness-{now_str}.md'
                    thinking_section = f'## Thinking\n{raw_thinking}\n\n' if raw_thinking else ''
                    report_content = (
                        f'# Harness Report: {task.identifier}\n\n'
                        f'- agent: `{self.agent}`\n'
                        f'- task: `{task.identifier}`\n'
                        f'- kind: `adhoc`\n'
                        f'- status: `completed`\n'
                        f'- recorded_at: `{run_at.isoformat()}`\n\n'
                        f'{thinking_section}'
                        f'## Summary\n{raw_summary or raw_report}\n\n'
                        f'## Report\n{raw_report or raw_summary}\n'
                    )
                    out_path.write_text(report_content, encoding='utf-8')
                    summary = raw_summary or raw_report
                    meta: dict[str, Any] = {'summary': summary}
                    if raw_thinking:
                        meta['thinking'] = raw_thinking
                    return HarnessRunResult(
                        status='completed',
                        persisted=True,
                        task_id=task.identifier,
                        report_path=out_path,
                        meta=meta,
                    )

            # Decision validation with feedback and bounded retry (Requirement 3)
            max_validation_retries = 2
            current_payload = completion.payload
            decision = None
            last_val_exc = None

            for retry_idx in range(max_validation_retries + 1):
                try:
                    decision = self._validate_decision(task, current_payload)
                    break
                except ValueError as exc:
                    last_val_exc = exc
                    if retry_idx < max_validation_retries and self.provider_adapter is not None:
                        feedback_prompt = (
                            f"Your previous output decision failed schema validation with error: {exc}.\n"
                            "Please correct your response and return ONLY valid JSON matching this schema:\n"
                            "If status is 'completed':\n"
                            '{\n  "status": "completed",\n  "summary": "...",\n  "report_markdown": "...",\n  "blockers": []\n}\n'
                            "If status is 'blocked':\n"
                            '{\n  "status": "blocked",\n  "summary": "...",\n  "report_markdown": "...",\n  "blockers": ["<non-empty blocker description>"]\n}\n'
                            "CRITICAL: If status is 'blocked', the 'blockers' field MUST be a non-empty JSON array of strings."
                        )
                        try:
                            from validators.kernel.providers.models import Message
                            retry_messages = [
                                Message.system("You are a governed agent. You must output your final decision strictly conforming to JSON schema."),
                                Message.user(feedback_prompt),
                            ]
                            retry_resp = self.provider_adapter.complete(
                                retry_messages,
                                cancellation_token=cancellation,
                            )
                            raw_retry_text = (retry_resp.message.content or '').strip()
                            retry_payload = self._parse_json_payload(raw_retry_text)
                            if retry_payload and isinstance(retry_payload, dict):
                                if 'report_markdown' not in retry_payload and 'summary' in retry_payload:
                                    retry_payload['report_markdown'] = retry_payload['summary']
                                current_payload = retry_payload
                                continue
                        except Exception:
                            pass
                    break

            if decision is None:
                # Retries exhausted: fail closed with clear reason immediately
                reason_str = str(last_val_exc or "invalid harness output")
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=f"Harness decision validation failed after retries: {reason_str}",
                    meta={'error': reason_str, 'blockers': [reason_str]},
                )

            # 7. Post-decision validation.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            # Post-execution declared validation
            from validators.harness.contract_gate import verify_post_execution
            from validators.harness.d024_gate import D024ViolationError
            try:
                observed_files_for_gate: tuple[str, ...] | None = None
                if tool_runtime is not None:
                    curr_snap = WorkspaceSnapshot.capture(
                        workspace_root, ignored_patterns=HARNESS_SNAPSHOT_IGNORED,
                    )
                    curr_diff = before_snapshot.diff(curr_snap)
                    bookkeeping_prefixes = ('.sync/inbox/', '.sync/state/', '.sync/reports/', '.sync/runtime/')
                    observed_files_for_gate = tuple(
                        norm_p for path in curr_diff.all_changed_files
                        if (norm_p := Path(path).as_posix().lstrip('/'))
                        and not any(norm_p.startswith(prefix) for prefix in bookkeeping_prefixes)
                    )
                verify_post_execution(
                    self.project_path,
                    self.agent,
                    task,
                    decision,
                    observed_files=observed_files_for_gate,
                )
            except D024ViolationError as d024_exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=str(d024_exc),
                )
            except Exception as exc:
                # Durable worker-blocker evidence: preserve the exact observed
                # file set, declaration mismatch, budget, contract hash, and a
                # stable failure_code before the scratch workspace is discarded.
                from validators.harness.contract_gate import (
                    build_contract_failure_evidence,
                    persist_blocker_evidence,
                )
                evidence = build_contract_failure_evidence(
                    self.project_path,
                    self.agent,
                    task,
                    exc,
                    observed_files=observed_files_for_gate,
                    decision=decision,
                    operation_id=operation_id,
                )
                evidence['evidence_path'] = persist_blocker_evidence(self.project_path, evidence)
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=evidence['canonical_message'],
                    meta={
                        'failure': evidence,
                        'blockers': [evidence['canonical_message']],
                    },
                )

            # 8. Post-post-execution contract verification.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            stage_inputs = {
                'completion': completion,
                'context': context,
                'decision': decision,
                'poll_ms': poll_ms,
                'retrieval': retrieval,
                'retrieval_ms': retrieval_ms,
                'scope_skips': scope_skips,
                'task': task,
                'llm_ms': llm_ms,
                'run_at': run_at,
                'tool_calls_audit': (completion.meta or {}).get('tool_calls_audit', []),
            }
            staged_errors = self._validate_staged_state(stage_inputs, source_root=workspace_root)
            if staged_errors:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason='staged stackmind validate failed: ' + '; '.join(staged_errors),
                )

            # 9. Post-staged validation, before lock acquisition.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            ok, message, lock_wait_ms = self._acquire_runtime_lock()
            if not ok:
                return HarnessRunResult(
                    status='deferred',
                    persisted=False,
                    task_id=task.identifier,
                    reason=message,
                    meta={
                        'benchmark_mode': retrieval.mode,
                        'retrieval_cap_exhausted': retrieval.cap_exhausted,
                    },
                )

            hold_started = time.monotonic()
            diff = WorkspaceDiff()
            declaration_matches = True
            mismatch_reason = None
            try:
                # 10. Post-lock acquisition, before any persistent write.
                if self._is_cancelled(cancellation):
                    return self._cancelled_result(task, operation_id)
                from validators.harness.d025_gate import D025Gate
                gate = D025Gate()
                gate_decision = gate.evaluate_sequence(decision.commands)
                if not gate_decision.passed:
                    return HarnessRunResult(
                        status='blocked',
                        persisted=False,
                        task_id=task.identifier,
                        reason=f'D025 validation failed: {gate_decision.reason}',
                    )

                if self.agent in ("local-llm", "gitops") and task.work_order_id:
                    from validators.harness.d024_gate import D024Gate
                    d024_gate = D024Gate()
                    d024_decision = d024_gate.evaluate_work_order(self.project_path, task.work_order_id)
                    if not d024_decision.passed:
                        return HarnessRunResult(
                            status='blocked',
                            persisted=False,
                            task_id=task.identifier,
                            reason=f'D024 QA Gate Blocked: {d024_decision.reason}',
                            meta={'d024_decision': d024_decision.to_dict()},
                        )

                # For native tool providers, tools have already changed the
                # governed scratch workspace.  Legacy decision providers retain
                # the historical disposable staging copy behavior.
                staging_context = (
                    nullcontext(workspace_root)
                    if tool_runtime is not None
                    else tempfile.TemporaryDirectory()
                )
                with staging_context as staging_path:
                    staged_root = Path(staging_path) if tool_runtime is not None else Path(staging_path) / self.project_path.name
                    if tool_runtime is None:
                        shutil.copytree(
                            self.project_path,
                            staged_root,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.ruff_cache'),
                        )
                    self._apply_non_report_writes(stage_inputs, base_path=staged_root)
                    command_results: list[subprocess.CompletedProcess[str]] = []
                    for cmd in decision.commands:
                        if self._is_cancelled(cancellation):
                            return self._cancelled_result(task, operation_id)
                        command_results.append(self._execute_governed_command(
                            cmd, staged_root, tool_runtime,
                        ))

                    after_snapshot = WorkspaceSnapshot.capture(
                        staged_root, ignored_patterns=HARNESS_SNAPSHOT_IGNORED,
                    )
                    diff = before_snapshot.diff(after_snapshot)
                    # LLM declarations describe task changes, not harness-owned audit,
                    # inbox, and report bookkeeping under `.sync/`.
                    bookkeeping_prefixes = ('.sync/inbox/', '.sync/state/', '.sync/reports/')
                    task_wo_rel = (
                        task.path.relative_to(self.project_path).as_posix()
                        if task.work_order_id and task.path and task.path.is_relative_to(self.project_path)
                        else None
                    )
                    dec_norm = {Path(p).as_posix().lstrip('/') for p in decision.modified_files if p}
                    observed_task_files = tuple(
                        norm_p for path in diff.all_changed_files
                        if (norm_p := Path(path).as_posix().lstrip('/'))
                        and not (any(norm_p.startswith(prefix) for prefix in bookkeeping_prefixes) and norm_p not in dec_norm)
                        and (task_wo_rel is None or norm_p != task_wo_rel or norm_p in dec_norm)
                    )
                    phantom_files = {
                        p for p in dec_norm
                        if not (staged_root / p).is_file() and p.startswith(('_scratch', 'scratch', '.sync/state', '.sync/runtime'))
                    }
                    dec_effective = dec_norm - phantom_files
                    # Trust direction: a turn must WRITE everything it
                    # declares — claimed-but-missing work is the failure mode.
                    # Extra in-scope writes are authorized by the contract
                    # scope gates (tool-time and post-execution), not by
                    # declaration equality. Authoring turns are exempt
                    # entirely (AuthoringGate + readiness gate are the
                    # controls there).
                    declared_missing = dec_effective - set(observed_task_files)
                    declaration_matches = not declared_missing
                    if getattr(task, 'is_authoring', False):
                        declaration_matches = True
                    mismatch_reason = (
                        None if declaration_matches
                        else f"declared but not written: {sorted(declared_missing)} "
                             f"(observed {sorted(observed_task_files)})"
                    )
                    dimensions = self._evaluate_verification_dimensions(
                        task=task,
                        decision=decision,
                        diff=diff,
                        before_snapshot=before_snapshot,
                        after_snapshot=after_snapshot,
                        staged_root=staged_root,
                        staged_errors=staged_errors,
                        declaration_matches=declaration_matches,
                        command_results=command_results,
                        d025_passed=gate_decision.passed,
                        has_staged_writes=(task.kind == 'inbox'),
                        tool_runtime=tool_runtime,
                    )
                    # Audit trail (Phase 5): persist every declared command and
                    # its outcome — the ORIGINAL string the model asked to run,
                    # never internal shim argv — so the gate verdict and the
                    # command telemetry are attributable after the fact.
                    stage_inputs['commands_audit'] = [
                        {
                            'command': r.args,
                            'returncode': r.returncode,
                            'denied': 'command denied' in (r.stderr or ''),
                            'stderr_tail': (r.stderr or '')[-200:],
                        }
                        for r in command_results
                    ]
                    if not dimensions.all_passed:
                        failed = [name for name, passed in dimensions.to_dict().items() if name != 'all_passed' and not passed]
                        reason_msg = 'verification gate failed: ' + ', '.join(failed)
                        if staged_errors:
                            reason_msg += f" ({'; '.join(staged_errors)})"
                        meta_dict: dict[str, Any] = {'commands_audit': stage_inputs.get('commands_audit', [])}
                        if not dimensions.scope_verified:
                            # Scope evidence: make every scope-gate block
                            # diagnosable without re-running the turn.
                            evidence = {
                                'declared': sorted(decision.modified_files),
                                'observed': sorted(set(observed_task_files)),
                                'mismatch_reason': mismatch_reason,
                            }
                            meta_dict['scope_evidence'] = evidence
                            reason_msg += (
                                f" (declared {evidence['declared']}; "
                                f"observed {evidence['observed']})"
                            )
                        if not dimensions.outcome_verified and task.work_order_id and task.deliverable_path and not getattr(task, 'is_authoring', False):
                            norm_del = Path(task.deliverable_path).as_posix().lstrip('/')
                            stg_added = {Path(p).as_posix().lstrip('/') for p in diff.added}
                            stg_mod = {Path(p).as_posix().lstrip('/') for p in diff.modified}
                            if norm_del not in stg_added and norm_del not in stg_mod:
                                diag = f"declared deliverable '{task.deliverable_path}' was not added or modified in this turn"
                                reason_msg += f" ({diag})"
                                meta_dict['blocker'] = diag
                        return HarnessRunResult(
                            status='blocked',
                            persisted=False,
                            task_id=task.identifier,
                            reason=reason_msg,
                            meta=meta_dict,
                        )
                    self._apply_verified_workspace_diff(staged_root, diff)

                # `.sync` is excluded from workspace snapshots, so commit the
                # harness-owned bookkeeping only after the staged verification passes.
                self._apply_non_report_writes(stage_inputs)

                write_ms = int((time.monotonic() - hold_started) * 1000)
                hold_ms = write_ms
                trust_level = evaluate_learning_eligibility(
                    decision_status=decision.status,
                    dimensions=dimensions,
                    declaration_matches=declaration_matches,
                    has_unhandled_blockers=bool(decision.blockers),
                )

                from validators.experience.recorder import ExperienceRecorder
                experience_record = ExperienceRecorder.capture_from_stage_inputs(
                    self.project_path,
                    self.agent,
                    stage_inputs,
                    diff=diff,
                    dimensions=dimensions,
                    trust_level=trust_level,
                    duration_ms=int((time.monotonic() - poll_started) * 1000),
                )

                stage_inputs['diff'] = diff
                stage_inputs['declaration_matches'] = declaration_matches
                stage_inputs['dimensions'] = dimensions
                stage_inputs['trust_level'] = trust_level
                stage_inputs['experience_record'] = experience_record

                exp_write = FileWrite(
                    Path('.sync') / 'experience' / 'records' / f'{experience_record.experience_id}.json',
                    json.dumps(experience_record.to_dict(), indent=2, sort_keys=True),
                )

                report_write = self._build_report_write(
                    stage_inputs,
                    lock_wait_ms=lock_wait_ms,
                    lock_hold_ms=hold_ms,
                    write_ms=write_ms,
                )
                self._apply_ops(self.project_path, [report_write])
                hold_ms = int((time.monotonic() - hold_started) * 1000)
                final_report = self._build_report_write(
                    stage_inputs,
                    lock_wait_ms=lock_wait_ms,
                    lock_hold_ms=hold_ms,
                    write_ms=hold_ms,
                )
                events_write = self._build_events_write(stage_inputs, hold_ms, lock_wait_ms)
                self._apply_ops(self.project_path, [final_report, events_write, exp_write])
            finally:
                release_lock(self.sync_path, self.agent)

            return HarnessRunResult(
                status=decision.status,
                persisted=True,
                task_id=task.identifier,
                report_path=(self.project_path / final_report.path),
                meta=self._meta_payload(
                    stage_inputs,
                    lock_wait_ms=lock_wait_ms,
                    lock_hold_ms=hold_ms,
                    write_ms=hold_ms,
                ),
            )
        except LoopSafetyError as exc:
            return HarnessRunResult(
                status='blocked',
                persisted=False,
                task_id=None,
                reason=str(exc),
            )

    def discover_next_task(
        self, tree_data: dict[str, Any], work_order_id: str | None = None
    ) -> HarnessTask | None:
        if work_order_id:
            path = self.sync_path / 'work-orders' / 'ACTIVE' / f'{work_order_id}.yaml'
            if path.exists():
                payload = self._read_yaml(path)
                deliverable = payload.get('deliverable', {}) if isinstance(payload, dict) else {}
                return HarnessTask(
                    kind='work_order',
                    identifier=work_order_id,
                    path=path,
                    title=str(payload.get('title', work_order_id)),
                    body=str(payload.get('description', '')),
                    query=str(payload.get('title', work_order_id)),
                    work_order_id=work_order_id,
                    deliverable_path=deliverable.get('path') if isinstance(deliverable, dict) else None,
                    work_order_type=str(payload.get('type')) if isinstance(payload, dict) and payload.get('type') else None,
                    deliverable_type=str(deliverable.get('type')) if isinstance(deliverable, dict) and deliverable.get('type') else None,
                )

        inbox_dir = self.sync_path / 'inbox' / self.agent
        inbox_candidates = (
            sorted(
                path
                for path in inbox_dir.iterdir()
                if path.is_file() and path.name != '.gitkeep'
            )
            if inbox_dir.exists()
            else []
        )
        if inbox_candidates:
            selected = inbox_candidates[0]
            body = self._read_text(selected)
            ref_wo_id = None
            ref_wo_type = None
            m = re.search(r'\b(WO-\d+)\b', selected.name) or re.search(r'\b(WO-\d+)\b', body)
            if m:
                ref_wo_id = m.group(1)
                for sub in ('ACTIVE', 'COMPLETED'):
                    wpath = self.sync_path / 'work-orders' / sub / f'{ref_wo_id}.yaml'
                    if wpath.exists():
                        wpayload = self._read_yaml(wpath)
                        if isinstance(wpayload, dict):
                            ref_wo_type = wpayload.get('type')
                        break
            return HarnessTask(
                kind='inbox',
                identifier=selected.name,
                path=selected,
                title=_first_content_line(body, fallback=selected.stem),
                body=body,
                query=_first_content_line(body, fallback=selected.stem),
                work_order_id=ref_wo_id,
                work_order_type=str(ref_wo_type) if ref_wo_type else None,
            )

        assigned = (
            tree_data.get('agents', {}).get(self.agent, {}).get('assigned_work_orders', []) or []
        )
        for wo_id in assigned:
            path = self.sync_path / 'work-orders' / 'ACTIVE' / f'{wo_id}.yaml'
            if not path.exists():
                continue
            payload = self._read_yaml(path)
            deliverable = payload.get('deliverable', {}) if isinstance(payload, dict) else {}
            return HarnessTask(
                kind='work_order',
                identifier=wo_id,
                path=path,
                title=str(payload.get('title', wo_id)),
                body=str(payload.get('description', '')),
                query=str(payload.get('title', wo_id)),
                work_order_id=wo_id,
                deliverable_path=deliverable.get('path') if isinstance(deliverable, dict) else None,
                work_order_type=str(payload.get('type')) if isinstance(payload, dict) and payload.get('type') else None,
                deliverable_type=str(deliverable.get('type')) if isinstance(deliverable, dict) and deliverable.get('type') else None,
            )
        return None

    def _ensure_protocol_citizenship(self, tree_data: dict[str, Any]) -> None:
        boot_file = self.sync_path / 'runtime' / 'boot' / f'{self.agent}.boot.yaml'
        contract_file = self.sync_path / 'agents' / f'{self.agent}.agent.md'
        inbox_dir = self.sync_path / 'inbox' / self.agent
        inbox_read_dir = inbox_dir / '_read'
        outbox_dir = self.sync_path / 'outbox' / self.agent

        if 'agents' not in tree_data or not isinstance(tree_data.get('agents'), dict):
            tree_data['agents'] = {}
        if self.agent not in tree_data['agents']:
            tree_data['agents'][self.agent] = {
                'session_count': 0,
                'status': 'idle',
                'assigned_work_orders': [],
            }
            tree_file = self.sync_path / 'runtime' / 'TREE.yaml'
            tree_file.parent.mkdir(parents=True, exist_ok=True)
            tree_file.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding='utf-8')

        if not boot_file.exists():
            boot_file.parent.mkdir(parents=True, exist_ok=True)
            boot_payload = {
                'agent': self.agent,
                'role': 'Backend Developer' if self.agent == 'codex' else self.agent.capitalize(),
                'schema_version': 1,
                'release': '3.1.0',
                'session_count': 0,
                'status': 'IDLE',
                'assigned_work_orders': [],
                'blockers': [],
            }
            boot_file.write_text(yaml.safe_dump(boot_payload, sort_keys=False), encoding='utf-8')

        if not contract_file.exists():
            contract_file.parent.mkdir(parents=True, exist_ok=True)
            contract_file.write_text(
                f"# Agent: {self.agent}\n\nRole: Protocol Citizen\nStatus: Active\n",
                encoding='utf-8',
            )

        inbox_read_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)

    def _load_tree(self) -> dict[str, Any]:
        tree_file = self.sync_path / 'runtime' / 'TREE.yaml'
        if not tree_file.exists():
            tree_file.parent.mkdir(parents=True, exist_ok=True)
            initial_tree: dict[str, Any] = {
                'schema_version': 1,
                'tree_version': 1,
                'release': '3.1.0',
                'agents': {
                    self.agent: {
                        'session_count': 0,
                        'status': 'IDLE',
                        'assigned_work_orders': [],
                    }
                },
            }
            tree_file.write_text(yaml.safe_dump(initial_tree, sort_keys=False), encoding='utf-8')
            self._tree_cache = initial_tree
            return self._tree_cache
        if self._tree_cache is None:
            self._tree_cache = self._read_yaml(tree_file)
        return self._tree_cache

    def _read_text(self, path: Path) -> str:
        self.reader.touch(path)
        return path.read_text(encoding='utf-8')

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        self.reader.touch(path)
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError(f'Expected YAML mapping in {path}')
        return data

    def _validate_decision(self, task: HarnessTask, payload: dict[str, Any]) -> HarnessDecision:
        cleaned_payload = dict(payload)
        if 'report_markdown' not in cleaned_payload and 'summary' in cleaned_payload:
            cleaned_payload['report_markdown'] = cleaned_payload['summary']
        if 'summary' not in cleaned_payload and 'report_markdown' in cleaned_payload:
            cleaned_payload['summary'] = cleaned_payload['report_markdown']
        if 'blockers' not in cleaned_payload:
            cleaned_payload['blockers'] = []
        if 'modified_files' not in cleaned_payload:
            cleaned_payload['modified_files'] = []

        allowed_keys = {
            'status',
            'summary',
            'report_markdown',
            'blockers',
            'modified_files',
            'release_target',
            'retrieval_queries',
            'uncertainty',
            'commands',
        }
        filtered_payload = {k: v for k, v in cleaned_payload.items() if k in allowed_keys}

        errors = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}"
            for error in _OUTPUT_VALIDATOR.iter_errors(filtered_payload)
        ]
        status = str(filtered_payload.get('status', ''))
        release_target = filtered_payload.get('release_target')
        blockers = tuple(str(item) for item in filtered_payload.get('blockers', []))
        if task.work_order_id and task.deliverable_path and status == 'completed' and not str(release_target or '').strip():
            errors.append('release_target is required for completed work-order tasks')
        if status == 'blocked' and not blockers:
            errors.append('blocked decisions must declare blockers')
        if errors:
            raise ValueError('invalid harness output: ' + '; '.join(errors))
        if task.work_order_id and status == 'completed' and not str(release_target or '').strip():
            release_target = task.deliverable_path or 'patch'
        return HarnessDecision(
            status=status,
            summary=str(filtered_payload['summary']).strip(),
            report_markdown=str(filtered_payload['report_markdown']).strip(),
            blockers=blockers,
            modified_files=tuple(str(item) for item in filtered_payload.get('modified_files', [])),
            release_target=str(release_target).strip() if release_target else None,
            retrieval_queries=tuple(str(item) for item in filtered_payload.get('retrieval_queries', [])),
            uncertainty=tuple(str(item) for item in filtered_payload.get('uncertainty', [])),
            commands=tuple(str(item) for item in filtered_payload.get('commands', [])),
        )

    def _build_tool_runtime(
        self, task_contract: Any, task: HarnessTask, knowledge_api: KnowledgeAPI,
    ) -> GovernedToolRuntime | None:
        """Compose the existing kernel boundaries for one native provider attempt."""
        if self.provider_adapter is None:
            return None
        if task_contract is None:
            return None

        from validators.kernel.boundary import RuntimeBoundary
        from validators.kernel.contract import AgentContract as KernelContract
        from validators.kernel.identity import AuthorizationPolicy, get_role_policy
        from validators.kernel.operations import OperationJournal
        from validators.kernel.tools import ToolGateway
        from validators.kernel.workspace import ScratchWorkspace

        def workspace_rules(rules: Any) -> tuple[str, ...]:
            converted: list[str] = []
            for rule in rules:
                val = None
                if isinstance(rule, dict):
                    val = rule.get('module') or rule.get('path') or rule.get('target')
                elif isinstance(rule, str):
                    val = rule
                if not val or not isinstance(val, str):
                    continue
                val = val.strip().replace('\\', '/')
                if val.startswith('workspace/'):
                    val = val[len('workspace/'):]
                elif val.startswith('./'):
                    val = val[2:]

                has_sep = '/' in val
                has_wildcard = '*' in val or '?' in val
                has_file_ext = any(val.endswith(ext) for ext in ('.md', '.yaml', '.yml', '.json', '.py', '.txt', '.toml', '.ini', '.cfg', '.lock'))
                is_dotted_path = val.startswith('.')

                if has_sep or has_wildcard or has_file_ext or is_dotted_path:
                    if has_file_ext or has_wildcard:
                        converted.append(f'workspace/{val}')
                    else:
                        clean_dir = val.rstrip('/')
                        converted.append(f'workspace/{clean_dir}')
                        converted.append(f'workspace/{clean_dir}/**')
                else:
                    mod_path = val.replace('.', '/').rstrip('/')
                    converted.append(f'workspace/{mod_path}')
                    converted.append(f'workspace/{mod_path}/**')
            return tuple(converted)

        attempt_id = f'harness-{self.agent}-{int(time.time() * 1000)}'
        workspace = ScratchWorkspace.create(self.project_path, attempt_id)
        allow_rules = list(workspace_rules(task_contract.allow_rules))
        if not any(r.startswith('graph') for r in allow_rules):
            allow_rules.append('graph/**')
        contract = KernelContract(
            agent_id=task_contract.agent_id,
            work_order=task_contract.work_order,
            allow=tuple(allow_rules),
            deny=workspace_rules(task_contract.deny_rules),
            write_mode=task_contract.write_mode,
            budget=task_contract.budget,
        ).freeze()
        policy = get_role_policy(self.agent)
        boundary = RuntimeBoundary(OperationJournal())

        def graph_query(query: str) -> dict[str, Any]:
            bundle = knowledge_api.assemble_context(
                query, token_budget=self.token_budget, limit=self.context_limit, contract=task_contract,
            )
            return {
                'revision': bundle.revision,
                'text': bundle.text,
                'entries': [entry.text for entry in bundle.entries],
            }

        gateway = ToolGateway(
            workspace=workspace, boundary=boundary, contract=contract, policy=policy,
            session_id=f'harness-{self.agent}-{task.identifier}', attempt_id=attempt_id,
            actor_id=self.agent, provider_id=self.backend_id, graph_query=graph_query,
        )
        return GovernedToolRuntime(workspace=workspace, gateway=gateway)

    @staticmethod
    def _parse_json_payload(raw_text: str) -> dict[str, Any] | None:
        """Extract and normalize a JSON dictionary payload from text."""
        raw_text = (raw_text or '').strip()
        payload = None

        # 1. Try markdown code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(1).strip())
            except Exception:
                pass

        # 2. Try raw json loads
        if payload is None:
            try:
                payload = json.loads(raw_text)
            except Exception:
                pass

        # 3. Try raw_decode from first {
        if payload is None and '{' in raw_text:
            idx = raw_text.find('{')
            try:
                obj, _ = json.JSONDecoder().raw_decode(raw_text, idx)
                if isinstance(obj, dict):
                    payload = obj
            except Exception:
                pass

        # If payload was emitted as a wrapped dictionary or tool call format:
        if isinstance(payload, dict):
            if 'decision' in payload and isinstance(payload['decision'], dict):
                payload = payload['decision']
            elif 'arguments' in payload and isinstance(payload['arguments'], dict) and 'status' not in payload:
                payload = payload['arguments']
            if 'status' in payload and isinstance(payload['status'], str):
                st = payload['status'].lower().strip()
                if st in ('completed', 'blocked', 'deferred'):
                    payload['status'] = st
                elif st in ('success', 'passed', 'done', 'approved', 'ok'):
                    payload['status'] = 'completed'
                elif st in ('failed', 'failure', 'error', 'rejected'):
                    payload['status'] = 'blocked'

        return payload if isinstance(payload, dict) else None

    def _complete_request(
        self, request: LLMRequest, tool_runtime: GovernedToolRuntime | None,
    ) -> CompletionRecord:
        """Run legacy completions or the existing ProviderGateway tool loop."""
        if tool_runtime is None:
            if (
                self.provider_adapter is not None
                and (
                    not hasattr(self.llm_provider, 'complete')
                    or isinstance(self.llm_provider, EchoLLMProvider)
                )
            ):
                from validators.kernel.providers.models import Message
                task_text = f'Task: {request.task.title}\n\n{request.task.body}\n\n'
                prompt_text = f"{task_text}Context:\n{getattr(request.context, 'text', '')}"
                messages = [Message.user(prompt_text)]
                response = self.provider_adapter.complete(
                    messages,
                    cancellation_token=request.cancellation,
                )
                raw_text = (response.message.content or '').strip()
                return CompletionRecord(
                    provider=getattr(self.provider_adapter, 'provider_name', self.backend_id),
                    model=getattr(self.provider_adapter, 'model_name', self.backend_model),
                    payload={
                        'status': 'completed',
                        'summary': raw_text,
                        'report_markdown': raw_text,
                        'blockers': [],
                        'modified_files': [],
                        'commands': [],
                        'retrieval_queries': [],
                        'uncertainty': [],
                    },
                    prompt_tokens=response.usage.prompt_tokens if getattr(response, "usage", None) else 0,
                    completion_tokens=response.usage.completion_tokens if getattr(response, "usage", None) else 0,
                )
            return self.llm_provider.complete(request)

        from validators.kernel.providers.gateway import (
            ProviderGateway,
            get_curated_tools_for_phase,
            get_tools_for_role,
        )
        from validators.kernel.providers.models import Message
        from validators.kernel.session import Attempt

        kernel_contract = tool_runtime.gateway.contract
        attempt = Attempt(tool_runtime.workspace.attempt_id, kernel_contract)
        task_text = f'Task: {request.task.title}\n\n{request.task.body}\n\n'
        full_task_desc = f"{request.task.title}\n{request.task.body}"
        is_architecture = (self.agent == 'claude')
        is_authoring_task = (
            is_architecture
            and any(
                kw in full_task_desc.lower()
                for kw in (
                    "author the implementation work orders",
                    "author child work orders",
                    "has been approved by the operator",
                    "authoring work orders and contracts",
                )
            )
        )
        is_integration_review = (
            is_architecture
            and any(
                kw in full_task_desc.lower()
                for kw in (
                    "integration review",
                    "final integration review",
                    "perform integration review",
                )
            )
        )
        is_plan_task = (
            not is_authoring_task
            and not is_integration_review
            and (
                request.task.deliverable_path == 'PLAN.md'
                or 'PLAN.md' in request.task.title
                or 'PLAN.md' in (request.task.body or '')
            )
        )
        task_phase = (
            "planning" if is_plan_task
            else "authoring" if is_authoring_task
            else "integration" if is_integration_review
            else None
        )
        gateway = ProviderGateway(
            self.provider_adapter,
            tool_runtime.gateway,
            attempt=attempt,
            contract=kernel_contract,
            tools=get_curated_tools_for_phase(
                self.agent,
                phase=task_phase,
                backend_id=self.backend_id,
            ),
        )
        if is_plan_task:
            from validators.harness.plan import PLAN_GENERATION_INSTRUCTIONS
            task_text += f"\nImportant PLAN.md formatting requirement:\n{PLAN_GENERATION_INSTRUCTIONS}\n"

        if is_architecture and is_plan_task:
            from validators.harness.plan import (
                ARCHITECTURE_RESEARCH_INSTRUCTIONS,
                WORK_ORDER_SCHEMA_TEMPLATE,
                CONTRACT_SCHEMA_TEMPLATE,
            )
            task_text += f"\n{ARCHITECTURE_RESEARCH_INSTRUCTIONS}\n"
            task_text += f"\nGoverned Artifact Authoring Schemas (for reference when planning tasks):\n"
            task_text += f"{WORK_ORDER_SCHEMA_TEMPLATE}\n{CONTRACT_SCHEMA_TEMPLATE}\n"

        if is_architecture and is_authoring_task:
            from validators.harness.plan import (
                WORK_ORDER_SCHEMA_TEMPLATE,
                CONTRACT_SCHEMA_TEMPLATE,
            )
            task_text += f"\nGoverned Artifact Authoring Schemas (REQUIRED for writing child work orders and contracts):\n"
            task_text += f"{WORK_ORDER_SCHEMA_TEMPLATE}\n{CONTRACT_SCHEMA_TEMPLATE}\n"

        if is_architecture and is_integration_review:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI. "
                "All implementation work orders and QA verifications have completed. "
                "Your task is to perform the final integration review of all declared deliverables against requirements. "
                "You operate in a governed environment with a strictly read-only contract. "
                "You may inspect deliverables and test files using `read_file` or `list_directory`. "
                "Do NOT attempt to write or edit any files. "
                "When your review is complete, return your final decision strictly as JSON conforming to the requested schema."
            )
        elif is_architecture and is_authoring_task:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI. "
                "The operator has approved the architecture plan in PLAN.md. "
                "Your task now is to author the implementation Work Orders and Contracts for the tasks in PLAN.md. "
                "You operate in a governed environment where all file I/O MUST be performed through provided tools (read_file, write_file). "
                "WORKFLOW RULE: Call `write_file` to write each Work Order YAML to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
                "and each Contract YAML to .sync/contracts/<WO-ID>.yaml conforming to the provided schemas. "
                "Only worker roles may be assigned to implementation work orders (e.g. codex for backend, gemini for frontend, gemma for qa, local-llm for gitops). "
                "Architects must NEVER assign implementation tasks to claude. "
                "When all work orders and contracts are written, return the final HarnessDecision as JSON."
            )
        elif is_architecture:
            system_msg = (
                "You are Claude, the Senior Architect for StackMind CLI. "
                "You are responsible for codebase research, architecture planning, and decomposing product goals into actionable work orders and contracts. "
                "You decide which specialist agent (codex for backend, gemini for frontend, gemma for qa, local-llm for gitops) executes each milestone, indicating `(Agent: <agent>)` in each milestone title. "
                "You operate in a governed environment where all file I/O and graph queries MUST be performed through provided tools (query_graph, read_file, write_file). "
                "WORKFLOW RULE: In your first action, you MUST call the `query_graph` tool to inspect existing project architecture. "
                "Only after receiving the graph research results should you call `write_file` to write PLAN.md. "
                "Return the final HarnessDecision as JSON."
            )
        elif self.agent in ('gemma', 'qa'):
            system_msg = (
                "You are Gemma, the QA Lead for StackMind CLI. "
                "You are responsible for test execution, code quality validation, test coverage verification, and formal QA review verdicts. "
                "Per AGENTS.md, you inspect and validate code and run tests, but you MUST NEVER write or edit application source code. "
                "Workflow instructions:\n"
                "1. Use `read_file` or `list_directory` to inspect deliverables.\n"
                "2. Call `run_tests` (or `verify_tests`) to execute pytest against the test suite and verify test results.\n"
                "3. Call `verify_deliverable` to confirm all required artifacts exist.\n"
                "4. When validation passes, call `approve_work_order` (or `submit_verdict`) to submit your QA verdict.\n"
                "5. Return the final HarnessDecision JSON with your verdict and review notes."
            )
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
        elif self.agent in ('local-llm', 'gitops'):
            system_msg = (
                "You are Local-LLM, the GitOps & Release Lead for StackMind CLI. "
                "You are responsible for versioning hygiene, release documentation, and changelog maintenance. "
                "The supervisor automatically creates the git commit upon completion. "
                "Workflow instructions:\n"
                "1. If VERSION.md does not exist, use `write_file` to write VERSION.md (e.g. '0.1.0').\n"
                "2. If CHANGELOG.md does not exist, use `write_file` to write CHANGELOG.md documenting the release deliverables.\n"
                "3. Immediately return the final HarnessDecision JSON declaring status 'completed'."
            )
            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = raw_context
        else:
            scope_hints: list[str] = []
            if kernel_contract is not None:
                for r in getattr(kernel_contract, "allow", ()):
                    clean_r = str(r).replace("workspace/", "").replace("workspace\\", "")
                    if clean_r and not clean_r.startswith(".sync") and clean_r != "PLAN.md":
                        scope_hints.append(clean_r)
            scope_line = f" Allowed write scope: {', '.join(scope_hints)}." if scope_hints else ""
            target_line = f" Target deliverable file: '{request.task.deliverable_path}'." if request.task.deliverable_path else ""
            action_line = (
                f" WORKFLOW REQUIREMENT: You MUST author the declared deliverable '{request.task.deliverable_path}' using the `write_file` tool in this session. "
                "Do NOT output plain text planning or commentary without tool calls; you MUST execute tool calls to create the file before completing."
                if request.task.deliverable_path else ""
            )
            system_msg = (
                'You are a governed StackMind worker. Use tools for all file I/O. '
                f'Code execution is unavailable; the harness verifies after the final decision.{scope_line}{target_line}{action_line} '
                'Environment notice: Virtual environments (.venv), dependencies, and tests are managed externally by the harness and QA lead. '
                'Do not inspect, search for, or wait for .venv or shell execution. Your sole job is to author the code directly in your assigned deliverable files using write_file. '
                'Only create or modify files permitted by your contract scope. '
                'Once your files are written, return the final HarnessDecision as JSON.'
            )

            existing_files: list[str] = []
            try:
                target_dir = tool_runtime.workspace.root if tool_runtime is not None else self.project_path
                for p in target_dir.rglob("*"):
                    if p.is_file() and not any(part in (".git", ".sync", "__pycache__", ".venv", "node_modules") for part in p.parts):
                        existing_files.append(p.relative_to(target_dir).as_posix())
            except Exception:
                pass
            files_context = ""
            if existing_files:
                files_context = f"\nExisting workspace files:\n" + "\n".join(f"- {f}" for f in sorted(existing_files)[:30]) + "\n"

            raw_context = getattr(request.context, 'text', '') if request.context else ''
            full_context = f"{raw_context}\n{files_context}" if raw_context else files_context

        if request.task.deliverable_path and not is_architecture:
            task_text += (
                f"\nDELIVERABLE INSTRUCTION:\nYou MUST create or update '{request.task.deliverable_path}' by calling the `write_file` tool. "
                "Do NOT just explain your plan or describe the solution in plain text. You must call `write_file` directly to author the code into the file.\n"
            )

        messages = [
            Message.system(system_msg),
            Message.user(
                f'{task_text}Context:\n{full_context if not is_architecture else getattr(request.context, "text", "")}'
            ),
        ]
        from validators.kernel.providers.errors import (
            ConsecutiveToolFailureError,
            NoProgressLoopError,
            ToolLimitExceededError,
            ToolLoopExhaustedError,
        )

        guard_trip: str | None = None
        decision_source: str = "model"

        try:
            history = gateway.run_loop(
                messages,
                max_turns=self.max_turns,
                max_tool_calls=self.max_tool_calls,
                cancellation_token=request.cancellation,
                required_deliverable=request.task.deliverable_path if not is_architecture else None,
            )
        except (
            ToolLimitExceededError,
            ToolLoopExhaustedError,
            NoProgressLoopError,
            ConsecutiveToolFailureError,
        ) as exc:
            guard_trip = type(exc).__name__
            # Check what files have been written so far
            written_files_so_far: list[str] = list(getattr(gateway, "written_files", []))
            if not written_files_so_far:
                for msg in messages:
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            if tc.name == 'write_file' and 'path' in tc.arguments:
                                p = str(tc.arguments['path'])
                                if p not in written_files_so_far:
                                    written_files_so_far.append(p)

            # Hard-block only if there are no writes (for writing roles)
            is_non_writing_role = (self.agent in ('gemma', 'qa', 'claude')) or (
                self.agent in ('local-llm', 'gitops') and (
                    (self.project_path / "VERSION.md").is_file() or (self.project_path / "CHANGELOG.md").is_file()
                )
            )
            if not written_files_so_far and not is_non_writing_role:
                raise

            # Exactly one tool-less finalization turn
            fin_prompt = (
                f"Execution guard '{guard_trip}' was triggered: {exc}. "
                "Tool execution is now closed. Based on the files you have written, "
                "please output your final HarnessDecision JSON now."
            )
            messages.append(Message.user(fin_prompt))
            try:
                fin_resp = gateway.adapter.complete(messages, tools=[])
                gateway._record_and_check_budget(fin_resp.usage)
                messages.append(fin_resp.message)
            except Exception:
                pass
            history = messages

        assistant_msgs = [m for m in history if m.role == 'assistant']
        if not assistant_msgs:
            if guard_trip:
                assistant_msgs = [Message.assistant("")]
            else:
                raise ValueError('provider tool loop ended without an assistant message')
        final = next((m for m in reversed(assistant_msgs) if m.content and m.content.strip()), assistant_msgs[-1])
        raw_text = (final.content or '').strip()
        payload = self._parse_json_payload(raw_text)

        written_files: list[str] = []
        for msg in history:
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.name == 'write_file' and 'path' in tc.arguments:
                        p = str(tc.arguments['path'])
                        if p not in written_files:
                            written_files.append(p)
        for wf in getattr(gateway, "written_files", []):
            if wf not in written_files:
                written_files.append(wf)

        # Executed-tool audit for the governed loop: deterministic evidence of
        # what the model actually ran (e.g. run_tests for QA work orders).
        tool_calls_audit: list[dict[str, Any]] = []
        for msg in history:
            for tc in getattr(msg, 'tool_calls', ()) or ():
                entry: dict[str, Any] = {'tool': tc.name}
                tc_args = getattr(tc, 'arguments', None) or {}
                for key in ('path', 'command', 'argv', 'cmd', 'pattern'):
                    if key in tc_args:
                        entry[key] = tc_args[key]
                tool_calls_audit.append(entry)

        # Plan deliverable fallback: if is_plan_task and PLAN.md was not written via tool call,
        # extract it from raw text / assistant messages / payload report and persist it to staged root.
        if is_plan_task and not any(Path(p).name == "PLAN.md" for p in written_files):
            from validators.harness.plan import extract_plan_from_text, validate_plan_structure
            staged_plan = (
                tool_runtime.workspace.root / "PLAN.md"
                if tool_runtime and getattr(tool_runtime, "workspace", None)
                else self.project_path / "PLAN.md"
            )
            candidates = [raw_text]
            if isinstance(payload, dict):
                if payload.get("report_markdown"):
                    candidates.append(str(payload["report_markdown"]))
                if payload.get("summary"):
                    candidates.append(str(payload["summary"]))
            for msg in reversed(assistant_msgs):
                if getattr(msg, "content", None) and msg.content.strip():
                    candidates.append(msg.content.strip())

            for c_text in candidates:
                extracted = extract_plan_from_text(
                    c_text, default_title=request.task.title or "Project Architecture"
                )
                if extracted:
                    is_val, _ = validate_plan_structure(extracted)
                    if is_val:
                        staged_plan.parent.mkdir(parents=True, exist_ok=True)
                        staged_plan.write_text(extracted, encoding="utf-8")
                        if "PLAN.md" not in written_files:
                            written_files.append("PLAN.md")
                        break

        if not isinstance(payload, dict):
            if guard_trip:
                decision_source = "synthesized_after_loop_guard"

            # Check if this was an unfulfilled writing task
            is_unfulfilled_deliverable = False
            if request.task.deliverable_path and not written_files:
                deliverable_exists = False
                try:
                    target_dir = tool_runtime.workspace.root if tool_runtime is not None else self.project_path
                    deliverable_exists = (target_dir / request.task.deliverable_path).exists()
                except Exception:
                    pass
                if not deliverable_exists:
                    is_unfulfilled_deliverable = True

            wo_type_upper = str(getattr(request.task, 'work_order_type', '') or '').upper()
            is_validation_or_review = (
                wo_type_upper == 'VALIDATION'
                or request.task.kind == 'validation'
                or (wo_type_upper not in ('RESEARCH', 'AUDIT') and (
                    'integration review' in (request.task.title or '').lower()
                    or 'validation review' in (request.task.title or '').lower()
                    or 'review' in (request.task.title or '').lower()
                    or 'validation' in (request.task.title or '').lower()
                ))
            )

            if is_unfulfilled_deliverable:
                payload = {
                    'status': 'blocked',
                    'summary': f"Worker ended turn without authoring declared deliverable '{request.task.deliverable_path}': {raw_text[:200]}",
                    'report_markdown': f"Worker ended turn without authoring declared deliverable '{request.task.deliverable_path}'. Raw output: {raw_text}",
                    'modified_files': written_files,
                    'blockers': [f"declared deliverable '{request.task.deliverable_path}' was not added or modified in this turn"],
                }
            elif is_validation_or_review:
                blocker_detail = raw_text[:200] if raw_text else (f"Validation tripped loop guard: {guard_trip}" if guard_trip else "Validation review ended without structured approval")
                payload = {
                    'status': 'blocked',
                    'summary': f"Validation review requires resolution: {blocker_detail}",
                    'report_markdown': raw_text or f"Validation review ended without structured approval ({guard_trip or 'fallback'}).",
                    'modified_files': written_files,
                    'blockers': [blocker_detail],
                }
            else:
                payload = {
                    'status': 'completed',
                    'summary': raw_text[:200] or (f'Completed task via tools (synthesized after {guard_trip})' if guard_trip else 'Completed task via tools'),
                    'report_markdown': raw_text or (f'Completed task via tools (synthesized after {guard_trip})' if guard_trip else 'Completed task via tools'),
                    'modified_files': written_files,
                    'blockers': [],
                }
        else:
            if guard_trip:
                decision_source = "model"
            payload.setdefault('status', 'completed')
            payload.setdefault('summary', 'Completed task via tools')
            payload.setdefault('report_markdown', payload.get('summary', 'Execution completed via governed tools.'))
            payload.setdefault('blockers', [])
            if not payload.get('modified_files') and written_files:
                payload['modified_files'] = written_files
            elif written_files:
                current_mods = {Path(p).as_posix().lstrip('/') for p in payload.get('modified_files', [])}
                for wf in written_files:
                    if Path(wf).as_posix().lstrip('/') not in current_mods:
                        payload.setdefault('modified_files', []).append(wf)

        # Prune internal scratch/decision paths and non-existent phantom files from modified_files
        is_read_only = (
            (kernel_contract is not None and getattr(kernel_contract, 'write_mode', None) == 'read-only')
            or is_integration_review
        )
        if is_read_only:
            payload['modified_files'] = []
        elif payload.get('modified_files'):
            pruned_mods: list[str] = []
            for p in payload['modified_files']:
                norm_p = str(p).replace('\\', '/').lstrip('/')
                if norm_p.startswith(('_scratch', 'scratch', '.sync/state', '.sync/runtime')):
                    continue
                if tool_runtime is not None:
                    is_written = norm_p in written_files or any(Path(wf).as_posix().lstrip('/') == norm_p for wf in written_files)
                    if not is_written:
                        continue
                pruned_mods.append(p)
            payload['modified_files'] = pruned_mods

        if request.task.work_order_id and payload.get('status') == 'completed' and not payload.get('release_target'):
            payload['release_target'] = request.task.deliverable_path or 'patch'

        allowed_keys = {
            'status',
            'summary',
            'report_markdown',
            'blockers',
            'modified_files',
            'release_target',
            'retrieval_queries',
            'uncertainty',
            'commands',
        }
        payload = {k: v for k, v in payload.items() if k in allowed_keys}
        completion_meta: dict[str, Any] = {}
        if guard_trip:
            completion_meta['guard_trip'] = guard_trip
            completion_meta['decision_source'] = decision_source
        completion_meta['tool_calls_audit'] = tool_calls_audit

        return CompletionRecord(
            provider=getattr(self.provider_adapter, 'provider_name', self.backend_id),
            model=getattr(self.provider_adapter, 'model_name', self.backend_model),
            payload=payload,
            prompt_tokens=gateway.total_usage.prompt_tokens,
            completion_tokens=gateway.total_usage.completion_tokens,
            meta=completion_meta,
        )

    def _execute_governed_command(
        self, cmd: str, staged_root: Path, tool_runtime: GovernedToolRuntime | None,
    ) -> subprocess.CompletedProcess[str]:
        if tool_runtime is None:
            return self._execute_sandboxed_command(cmd, staged_root)
        try:
            argv = shlex.split(cmd, posix=os.name != 'nt')
        except ValueError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f'unparseable command: {exc}')
        if not argv:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr='empty command')
        try:
            result = tool_runtime.gateway.run_command(argv)
            return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)
        except Exception as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f'command denied: {exc}')

    def _execute_sandboxed_command(
        self, cmd: str, staged_root: Path
    ) -> subprocess.CompletedProcess[str]:
        """Execute one LLM-declared command string through the hardened sandbox.

        Phase 5 of the sandbox hardening: the runner's former raw
        subprocess.run(cmd, shell=True) bypassed every protection that
        ToolGateway.run_command enforces. This helper routes the same command
        through the hardened path so it inherits, in order:

        1. shlex tokenization (shell=False, no shell interpolation). A string
           that cannot be tokenized (unbalanced quote) is refused outright.
        2. The Phase 2 hard interpreter denylist (bash -c / python -c / ...).
        3. The Phase 1 scrubbed environment (no daemon secrets) via
           ProcessSandbox._child_env, including env_extra (picked up by the
           POSIX limits shim; ignored on Windows).
        4. The Phase 3 resource limits (512 MB / 60 s by default; per-call
           overrides remain possible).

        The process tree is killed and marked failed when either the sandbox's
        wall-clock timeout or a hard limit terminates it; the turn continues
        (the command result is journaled and visible in the report) exactly as
        the raw path did for nonzero exits.
        """
        from validators.kernel.interpreter_denylist import check_command
        from validators.kernel.sandbox import ProcessSandbox
        from validators.kernel.workspace import ScratchWorkspace, WorkspaceEscapeError

        try:
            if os.name == "nt":
                # Windows: shlex posix mode mangles backslash paths (\U -> U),
                # and posix=False preserves quote characters inside tokens.
                # Windows quoting rules are delimiter-only, so strip the
                # surrounding quotes from each token manually.
                argv = [
                    tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] == '"' else tok
                    for tok in shlex.split(cmd, posix=False)
                ]
            else:
                argv = shlex.split(cmd, posix=True)
        except ValueError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"unparseable command: {exc}")
        if not argv:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr="empty command")

        denial = check_command(argv)
        if denial is not None:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"command denied: {denial}")

        # Read-only scratch mirror of the staged tree: commands run against a
        # pristine copy of project HEAD (not the live working tree), and the
        # scratch workspace enforces path containment (no ../escape targets).
        scratch = ScratchWorkspace.create(staged_root, attempt_id=f"harness-{self.agent}")
        sandbox = ProcessSandbox(scratch)
        try:
            result = sandbox.run(
                argv,
                timeout=120.0,
                env_extra={
                    "STACKMIND_STAGE_ROOT": str(staged_root),
                    "STACKMIND_AGENT": self.agent,
                },
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                cmd, returncode=-9, stderr="command killed: wall-clock timeout (120s), process tree swept"
            )
        except WorkspaceEscapeError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"command refused: {exc}")
        except OSError as exc:
            return subprocess.CompletedProcess(cmd, returncode=-1, stderr=f"{type(exc).__name__}: {exc}")
        return subprocess.CompletedProcess(cmd, result.returncode, result.stdout, result.stderr)

    def _validate_staged_state(
        self, stage_inputs: dict[str, Any], *, source_root: Path | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            staged_root = Path(tmp_dir) / self.project_path.name
            shutil.copytree(
                self.project_path,
                staged_root,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    '.git',
                    '__pycache__',
                    '.pytest_cache',
                    '.ruff_cache',
                ),
            )
            if source_root is not None and source_root.resolve() != self.project_path.resolve():
                shutil.copytree(
                    source_root,
                    staged_root,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        '.git',
                        '.sync',
                        '__pycache__',
                        '.pytest_cache',
                        '.ruff_cache',
                    ),
                )
                if self.agent == 'claude':
                    for sub in ('work-orders', 'contracts'):
                        src_sub = source_root / '.sync' / sub
                        if src_sub.is_dir():
                            dst_sub = staged_root / '.sync' / sub
                            shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)
                elif self.agent == 'gemma':
                    for sub in ('inbox', 'reviews', 'qa'):
                        src_sub = source_root / '.sync' / sub
                        if src_sub.is_dir():
                            dst_sub = staged_root / '.sync' / sub
                            shutil.copytree(src_sub, dst_sub, dirs_exist_ok=True)
                self._sync_index_and_tree_yaml(staged_root)
            self._apply_non_report_writes(stage_inputs, base_path=staged_root)
            report_write = self._build_report_write(
                stage_inputs,
                lock_wait_ms=0,
                lock_hold_ms=0,
                write_ms=0,
            )
            events_write = self._build_events_write(stage_inputs, 0, 0)
            self._apply_ops(staged_root, [report_write, events_write])
            validation = validate_runtime(staged_root)
        return [issue.message for issue in validation.errors]

    def _apply_non_report_writes(
        self,
        stage_inputs: dict[str, Any],
        *,
        base_path: Path | None = None,
    ) -> None:
        base = base_path or self.project_path
        self._apply_ops(base, self._non_report_ops(stage_inputs))

    def _non_report_ops(self, stage_inputs: dict[str, Any]) -> list[FileWrite | FileMove]:
        task: HarnessTask = stage_inputs['task']
        decision: HarnessDecision = stage_inputs['decision']
        now: datetime = stage_inputs['run_at']
        ops: list[FileWrite | FileMove] = []

        if task.kind == 'inbox':
            archived = Path('.sync') / 'inbox' / self.agent / '_read' / task.path.name
            ops.append(FileMove(task.path.relative_to(self.project_path), archived))

        if task.work_order_id:
            if task.kind == 'work_order':
                wo_log_path = task.path
            else:
                # Inbox items are markdown notices, not YAML. Append the run
                # log to the referenced work order file instead of parsing
                # the notice itself.
                wo_log_path = None
                for sub in ('ACTIVE', 'COMPLETED'):
                    candidate = self.sync_path / 'work-orders' / sub / f'{task.work_order_id}.yaml'
                    if candidate.exists():
                        wo_log_path = candidate
                        break
            if wo_log_path is not None:
                cached_payload = stage_inputs.get('_work_order_payload')
                if cached_payload is None:
                    cached_payload = self._read_yaml(wo_log_path)
                    stage_inputs['_work_order_payload'] = dict(cached_payload)
                payload = dict(cached_payload)
                log_entries = list(payload.get('log', []))
                log_entries.append(f"{now.isoformat()} harness {decision.status}: {decision.summary}")
                payload['log'] = log_entries
                payload['updated'] = now.isoformat()
                content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
                ops.append(FileWrite(wo_log_path.relative_to(self.project_path), content))

                if decision.status == 'completed':
                    stamp = now.date().isoformat()
                    review_name = f'{stamp}_{self.agent}_{task.work_order_id}-review.md'
                    completion_name = f'{stamp}_{self.agent}_{task.work_order_id}-complete.md'
                    ops.append(
                        FileWrite(
                            Path('.sync') / 'inbox' / 'gemma' / review_name,
                            self._render_review_request(task, decision, now),
                        )
                    )
                    ops.append(
                        FileWrite(
                            Path('.sync') / 'inbox' / 'claude' / completion_name,
                            self._render_completion_notice(task, decision, now),
                        )
                    )

        return ops

    def _build_report_write(
        self,
        stage_inputs: dict[str, Any],
        *,
        lock_wait_ms: int,
        lock_hold_ms: int,
        write_ms: int,
    ) -> FileWrite:
        now: datetime = stage_inputs['run_at']
        timestamp = now.isoformat().replace(':', '-')
        return FileWrite(
            Path('.sync') / 'outbox' / self.agent / f'harness-{timestamp}.md',
            self._render_report(stage_inputs, lock_wait_ms, lock_hold_ms, write_ms, now),
        )

    def _build_events_write(
        self,
        stage_inputs: dict[str, Any],
        lock_hold_ms: int,
        lock_wait_ms: int,
    ) -> FileWrite:
        trust_level = stage_inputs.get('trust_level')
        diff = stage_inputs.get('diff')
        dimensions = stage_inputs.get('dimensions')
        exp_rec = stage_inputs.get('experience_record')
        event = {
            'agent': self.agent,
            'benchmark_mode': stage_inputs['retrieval'].mode,
            'commands_audit': stage_inputs.get('commands_audit', []),
            'context_revision': stage_inputs['context'].revision,
            'declaration_matches': stage_inputs.get('declaration_matches', True),
            'event': 'harness.run',
            'experience_id': exp_rec.experience_id if exp_rec else None,
            'learning_eligible': exp_rec.learning_eligible if exp_rec else False,
            'lock_hold_ms': lock_hold_ms,
            'lock_wait_ms': lock_wait_ms,
            'observed_changes_count': len(diff.all_changed_files) if diff else 0,
            'provider': stage_inputs['completion'].provider,
            'status': stage_inputs['decision'].status,
            'task_id': stage_inputs['task'].identifier,
            'timestamp': stage_inputs['run_at'].isoformat(),
            'trust_level': trust_level.value if hasattr(trust_level, 'value') else str(trust_level or 'OBSERVABLE'),
            'verification_passed': dimensions.all_passed if dimensions else True,
            'write_ms': lock_hold_ms,
        }
        return FileWrite(
            Path('.sync') / 'state' / 'harness' / 'events.jsonl',
            json.dumps(event, sort_keys=True) + '\n',
            append=True,
        )

    def _render_report(
        self,
        stage_inputs: dict[str, Any],
        lock_wait_ms: int,
        lock_hold_ms: int,
        write_ms: int,
        now: datetime,
    ) -> str:
        task: HarnessTask = stage_inputs['task']
        decision: HarnessDecision = stage_inputs['decision']
        retrieval: RetrievalBatch = stage_inputs['retrieval']
        meta = self._meta_payload(stage_inputs, lock_wait_ms, lock_hold_ms, write_ms)
        sections = [
            f'# Harness Report: {task.identifier}',
            '',
            f'- agent: `{self.agent}`',
            f'- task: `{task.identifier}`',
            f'- kind: `{task.kind}`',
            f'- status: `{decision.status}`',
            f'- recorded_at: `{now.isoformat()}`',
            '',
            '## Summary',
            decision.summary,
            '',
            '## Report',
            decision.report_markdown,
        ]
        scope_skips = stage_inputs.get('scope_skips', ()) or ()
        if scope_skips:
            sections.extend([
                '',
                '## Contract-Scope Filtered Context',
                'Knowledge nodes excluded from the context bundle by the active '
                'contract (CONTRACT-01):',
            ])
            sections.extend(f'- {node_id}: {reason}' for node_id, reason in scope_skips)
        if retrieval.evidence:
            sections.extend(['', '## External Evidence'])
            sections.extend(item.render() for item in retrieval.evidence)
        if decision.blockers:
            sections.extend(['', '## Blockers'])
            sections.extend(f'- {item}' for item in decision.blockers)
        commands_audit = stage_inputs.get('commands_audit') or []
        if commands_audit:
            sections.extend(['', '## Commands'])
            for item in commands_audit:
                status = 'DENIED' if item.get('denied') else f"rc={item.get('returncode')}"
                sections.append(f"- `{item.get('command')}` → {status}")
        sections.extend([
            '',
            '## Meta',
            '```yaml',
            yaml.safe_dump(meta, sort_keys=False, allow_unicode=False).rstrip(),
            '```',
            '',
        ])
        return '\n'.join(sections)

    def _meta_payload(
        self,
        stage_inputs: dict[str, Any],
        lock_wait_ms: int,
        lock_hold_ms: int,
        write_ms: int,
    ) -> dict[str, Any]:
        completion: CompletionRecord = stage_inputs['completion']
        context: ContextBundle = stage_inputs['context']
        decision: HarnessDecision = stage_inputs['decision']
        retrieval: RetrievalBatch = stage_inputs['retrieval']
        trust_level = stage_inputs.get('trust_level')
        dimensions = stage_inputs.get('dimensions')
        diff = stage_inputs.get('diff')
        exp_rec = stage_inputs.get('experience_record')
        return {
            'agent': self.agent,
            'benchmark_mode': retrieval.mode,
            'cache_hits': retrieval.cache_hits,
            'completion_tokens': completion.completion_tokens,
            'confidence': max(0.0, 1.0 - (0.1 * len(decision.uncertainty))),
            'context_revision': context.revision,
            'context_stale': context.stale,
            'context_scope_filtered': (
                getattr(context, 'scope_filtered_count', 0)
                or len(stage_inputs.get('scope_skips', ()) or ())
            ),
            'cost_estimate': round(completion.cost_estimate + retrieval.cost_estimate, 6),
            'commands_audit': stage_inputs.get('commands_audit', []),
            'tool_calls_audit': stage_inputs.get('tool_calls_audit', []),
            'declaration_matches': stage_inputs.get('declaration_matches', True),
            'experience_id': exp_rec.experience_id if exp_rec else None,
            'knowledge_git_commit': context.git_commit,
            'latency_ms': {
                'llm': stage_inputs['llm_ms'],
                'poll': stage_inputs['poll_ms'],
                'retrieval': stage_inputs['retrieval_ms'],
                'write': write_ms,
            },
            'learning_eligible': exp_rec.learning_eligible if exp_rec else False,
            'lock_hold_ms': lock_hold_ms,
            'lock_wait_ms': lock_wait_ms,
            'backend_id': getattr(self, 'backend_id', completion.provider),
            'model': completion.model,
            'summary': decision.summary,
            'blockers': list(decision.blockers) if decision.blockers else [],
            'modified_files': list(decision.modified_files),
            'report_markdown': decision.report_markdown,
            'observed_changes': diff.to_dict() if diff else {},
            'prompt_tokens': completion.prompt_tokens,
            'provider': completion.provider,
            'retrieval_cap_exhausted': retrieval.cap_exhausted,
            'searches_used': retrieval.searches_used,
            'scope_filtered_nodes': [
                {'node_id': node_id, 'reason': reason}
                for node_id, reason in (stage_inputs.get('scope_skips', ()) or ())
            ],
            'guard_trip': completion.meta.get('guard_trip') if hasattr(completion, 'meta') else None,
            'decision_source': (
                completion.meta.get('decision_source', 'model')
                if hasattr(completion, 'meta')
                else 'model'
            ),
            'trust_level': trust_level.value if hasattr(trust_level, 'value') else str(trust_level or 'OBSERVABLE'),
            'uncertainty': list(decision.uncertainty),
            'verification_dimensions': dimensions.to_dict() if dimensions else {},
        }

    def _evaluate_verification_dimensions(
        self,
        *,
        task: HarnessTask,
        decision: HarnessDecision,
        diff: Any,
        before_snapshot: Any,
        after_snapshot: Any,
        staged_root: Path,
        staged_errors: list[str],
        declaration_matches: bool,
        command_results: list[subprocess.CompletedProcess[str]],
        d025_passed: bool,
        has_staged_writes: bool,
        tool_runtime: Any = None,
    ) -> Any:
        """Derive verification flags from the staged filesystem and command telemetry."""
        from validators.harness.contract_gate import verify_post_execution
        from validators.harness.snapshot import VerificationDimensions

        changed_files = tuple(Path(p).as_posix().lstrip('/') for p in diff.all_changed_files)
        bookkeeping_prefixes = ('.sync/inbox/', '.sync/state/', '.sync/reports/')
        dec_norm = {Path(p).as_posix().lstrip('/') for p in decision.modified_files if p}
        task_changed_files = tuple(
            relative_path for relative_path in changed_files
            if not (any(relative_path.startswith(prefix) for prefix in bookkeeping_prefixes) and relative_path not in dec_norm)
            and not (relative_path.startswith('.sync/work-orders/') and relative_path not in dec_norm)
        )
        scope_verified = declaration_matches
        if task_changed_files:
            try:
                verify_post_execution(
                    self.project_path,
                    self.agent,
                    task,
                    decision,
                    observed_files=task_changed_files,
                )
            except Exception:
                scope_verified = False

        # Runtime contracts use path rules; validate those directly when present.
        raw_contract = None
        if task.work_order_id:
            contract_path = self.sync_path / 'contracts' / f'{task.work_order_id}.yaml'
            if contract_path.exists():
                raw_contract = self._read_yaml(contract_path)
                allow_rules = raw_contract.get('scope', {}).get('allow', [])
                deny_rules = raw_contract.get('scope', {}).get('deny', [])

                def _rule_to_path(rule: Any) -> str:
                    if isinstance(rule, dict):
                        p = rule.get('path') or rule.get('target') or rule.get('module')
                        return str(p or '')
                    if isinstance(rule, str):
                        return rule
                    return ''

                def _path_matches_rule(target_path: str, rule_str: str) -> bool:
                    r = rule_str.replace('\\', '/')
                    if r.startswith('./'):
                        r = r[2:]
                    if (
                        target_path == r
                        or target_path.startswith(r.rstrip('/') + '/')
                        or fnmatch.fnmatch(target_path, r)
                        or (r.endswith('/**') and fnmatch.fnmatch(target_path, r))
                    ):
                        return True
                    r_slash = r.replace('.', '/')
                    if (
                        target_path == r_slash
                        or target_path == (r_slash + '.py')
                        or target_path.startswith(r_slash.rstrip('/') + '/')
                        or fnmatch.fnmatch(target_path, r_slash)
                    ):
                        return True
                    return False

                allow_paths = [p for rule in allow_rules if (p := _rule_to_path(rule))]
                deny_paths = [p for rule in deny_rules if (p := _rule_to_path(rule))]
                for relative_path in task_changed_files:
                    normalized = Path(relative_path).as_posix()
                    if normalized.startswith('.sync/') and relative_path not in decision.modified_files:
                        continue  # Harness-owned bookkeeping writes are authorized separately.
                    allowed = not allow_paths or any(_path_matches_rule(normalized, rule) for rule in allow_paths)
                    denied = any(_path_matches_rule(normalized, rule) for rule in deny_paths)
                    if not allowed or denied:
                        scope_verified = False

        state_verified = not staged_errors
        for relative_path in changed_files:
            before = before_snapshot.files.get(relative_path)
            after = after_snapshot.files.get(relative_path)
            if after is not None and not after.content_hash:
                state_verified = False
            if before is not None and after is not None and before.content_hash == after.content_hash:
                state_verified = False
            if after is None and before is None:
                state_verified = False

        code_verified = True
        for relative_path in changed_files:
            if relative_path.endswith('.py'):
                candidate = staged_root / relative_path
                if candidate.exists():
                    try:
                        source_code = candidate.read_text(encoding='utf-8')
                        ast.parse(source_code)
                        from validators.harness.dependency_gate import check_import_satisfiability
                        sat_result = check_import_satisfiability(
                            candidate,
                            project_root=self.project_path,
                            staged_root=staged_root,
                            source_code=source_code,
                            contract=raw_contract,
                        )
                        if not sat_result.passed:
                            code_verified = False
                            staged_errors.append(sat_result.diagnostic or f"import satisfiability failed for {relative_path}")
                    except (OSError, UnicodeDecodeError, SyntaxError):
                        code_verified = False

            # Phase C: PLAN.md structural validation
            norm_rel = Path(relative_path).as_posix()
            if norm_rel == "PLAN.md" or norm_rel.endswith("/PLAN.md"):
                candidate = staged_root / relative_path
                if candidate.exists():
                    from validators.harness.plan import validate_plan_structure
                    try:
                        plan_valid, plan_errs = validate_plan_structure(candidate.read_text(encoding='utf-8'))
                        if not plan_valid:
                            code_verified = False
                            state_verified = False
                    except Exception:
                        code_verified = False
                        state_verified = False

            # Phase D: Governed YAML authoring gate (.sync/work-orders and .sync/contracts)
            if (norm_rel.startswith('.sync/work-orders/') or norm_rel.startswith('.sync/contracts/')) and norm_rel in dec_norm:
                candidate = staged_root / relative_path
                if candidate.is_file() and (norm_rel.endswith('.yaml') or norm_rel.endswith('.yml')):
                    from validators.harness.authoring_gate import AuthoringGate
                    gate = AuthoringGate(project_root=self.project_path)
                    gate_decision = gate.validate_staged_artifact(
                        staged_root, relative_path, agent=self.agent, project_root=self.project_path
                    )
                    if not gate_decision.passed:
                        code_verified = False
                        state_verified = False

        for result in command_results:
            command = str(result.args).lower()
            if ('pytest' in command or 'test' in command) and result.returncode != 0:
                code_verified = False

        behavioral_verified = (
            (decision.status == 'completed' or (decision.status == 'blocked' and bool(decision.blockers)))
            and all(result.returncode == 0 for result in command_results)
        )

        d024_passed = True
        if self.agent in ("local-llm", "gitops") and task.work_order_id:
            from validators.harness.d024_gate import D024Gate
            d024_passed = D024Gate().evaluate_work_order(self.project_path, task.work_order_id).passed

        security_verified = d025_passed and d024_passed and all(
            not Path(relative_path).is_absolute() and '..' not in Path(relative_path).parts
            for relative_path in changed_files
        )
        if security_verified:
            from validators.kernel.security import scan_for_credential_leaks

            for relative_path in changed_files:
                candidate = staged_root / relative_path
                if candidate.is_file():
                    try:
                        content = candidate.read_text(encoding='utf-8', errors='replace')
                        if scan_for_credential_leaks(content, file_path=relative_path):
                            security_verified = False
                            break
                    except OSError:
                        security_verified = False
                        break

        deliverable_touched = False
        if task.deliverable_path:
            norm_deliv = Path(task.deliverable_path).as_posix().lstrip('/')
            staged_added = {Path(p).as_posix().lstrip('/') for p in diff.added}
            staged_modified = {Path(p).as_posix().lstrip('/') for p in diff.modified}
            deliverable_touched = (norm_deliv in staged_added or norm_deliv in staged_modified)

        authored_artifacts = any(
            (p.startswith('.sync/work-orders/') or p.startswith('.sync/contracts/'))
            for p in task_changed_files
        )

        if decision.status == 'blocked':
            outcome_verified = bool(decision.blockers) and bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
        elif getattr(task, 'is_authoring', False):
            # Authoring turns produce governed work orders and contracts (or synthesis).
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    authored_artifacts
                    or bool(task_changed_files)
                    or bool(decision.summary and decision.summary.strip())
                    or bool(decision.report_markdown and decision.report_markdown.strip())
                )
            )
        elif task.kind == 'inbox':
            # Inbox tasks are intentionally archived by the staged operation; the
            # task selected at discovery is itself the completed deliverable.
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
            )
        elif task.kind == 'adhoc':
            # Ad-hoc prompt turns produce conversational completion/report deliverables.
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and bool(
                    (decision.summary and decision.summary.strip())
                    or (decision.report_markdown and decision.report_markdown.strip())
                )
            )
        elif self.agent in ('gemma', 'qa'):
            # QA Lead: outcome verified by tool execution/testing, inbox verdict write, or non-empty QA report/verdict
            has_tool_activity = bool(
                tool_runtime
                and getattr(tool_runtime, 'gateway', None)
                and (
                    getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                    or getattr(tool_runtime.gateway, '_todo_list', None)
                )
            )
            has_report = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    has_report
                    or has_tool_activity
                    or bool(changed_files)
                    or bool(task_changed_files)
                    or any(r.returncode == 0 for r in command_results)
                )
            )
        elif self.agent in ('local-llm', 'gitops'):
            # GitOps Lead: outcome verified by release activity, git operations, or non-empty report
            has_tool_activity = bool(
                tool_runtime
                and getattr(tool_runtime, 'gateway', None)
                and (
                    getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                    or getattr(tool_runtime.gateway, '_todo_list', None)
                )
            )
            has_report = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    has_report
                    or has_tool_activity
                    or bool(changed_files)
                    or bool(task_changed_files)
                    or any(r.returncode == 0 for r in command_results)
                )
            )
        elif task.work_order_id and task.deliverable_path:
            if tool_runtime is not None:
                deliverable = staged_root / task.deliverable_path
                outcome_verified = (
                    decision.status == 'completed'
                    and not decision.blockers
                    and (deliverable_touched or (authored_artifacts and deliverable.exists()))
                )
            else:
                deliverable = staged_root / task.deliverable_path
                outcome_verified = (
                    decision.status == 'completed'
                    and not decision.blockers
                    and (deliverable_touched or deliverable.exists() or bool(changed_files) or has_staged_writes)
                )
        elif task.work_order_id and not task.deliverable_path:
            is_non_code_task = (
                getattr(task, 'deliverable_type', None) in ('doc', 'verification', 'review', 'release', 'research')
                or any(kw in (task.title or '').lower() for kw in ('qa', 'test', 'verification', 'audit', 'review', 'release', 'doc', 'plan'))
            )
            has_tool_activity = bool(
                tool_runtime
                and getattr(tool_runtime, 'gateway', None)
                and (
                    getattr(getattr(tool_runtime.gateway, 'boundary', None), 'journal', None)
                    or getattr(tool_runtime.gateway, '_todo_list', None)
                )
            )
            has_report = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (
                    bool(task_changed_files)
                    or bool(changed_files)
                    or any(r.returncode == 0 for r in command_results)
                    or (is_non_code_task and (has_report or has_tool_activity))
                )
            )
        else:
            outcome_verified = (
                decision.status == 'completed'
                and not decision.blockers
                and (bool(changed_files) or has_staged_writes)
            )
        return VerificationDimensions(
            scope_verified=scope_verified,
            state_verified=state_verified,
            code_verified=code_verified,
            behavioral_verified=behavioral_verified,
            security_verified=security_verified,
            outcome_verified=outcome_verified,
        )

    def _apply_verified_workspace_diff(self, staged_root: Path, diff: Any) -> None:
        """Commit only the already-verified staged diff to the live workspace."""
        for relative_path in (*diff.added, *diff.modified):
            relative = Path(relative_path)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'unsafe staged path: {relative_path}')
            source = staged_root / relative
            target = self.project_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for relative_path in diff.deleted:
            relative = Path(relative_path)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'unsafe staged path: {relative_path}')
            target = self.project_path / relative
            if target.exists():
                target.unlink()
        self._sync_index_and_tree_yaml(self.project_path)

    @staticmethod
    def _sync_index_and_tree_yaml(base_dir: Path) -> None:
        """Ensure .sync/work-orders/INDEX.yaml and .sync/runtime/TREE.yaml reflect all active work order files."""
        sync_dir = base_dir / ".sync"
        active_dir = sync_dir / "work-orders" / "ACTIVE"
        index_file = sync_dir / "work-orders" / "INDEX.yaml"
        if not active_dir.is_dir() or not index_file.is_file():
            return
        try:
            index_data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
            if not isinstance(index_data, dict):
                return
            index_data.setdefault("next_id", 1)
            orders = index_data.setdefault("orders", [])
            existing_orders = {
                o.get("id"): o for o in orders if isinstance(o, dict) and "id" in o
            }
            updated_index = False
            assigned_per_agent: dict[str, list[str]] = {}

            for wo_file in sorted(active_dir.glob("*.yaml")):
                wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                if not isinstance(wo_data, dict):
                    continue
                wo_id = wo_data.get("id") or wo_file.stem
                agents = wo_data.get("assigned_agents", [])
                if isinstance(agents, list):
                    for ag in agents:
                        if isinstance(ag, str) and ag:
                            assigned_per_agent.setdefault(ag.lower().strip(), []).append(wo_id)
                elif isinstance(agents, str) and agents:
                    assigned_per_agent.setdefault(agents.lower().strip(), []).append(wo_id)

                if wo_id not in existing_orders:
                    from datetime import datetime, timezone
                    now_iso = datetime.now(timezone.utc).isoformat()
                    created_val = wo_data.get("created") or now_iso
                    updated_val = wo_data.get("updated") or now_iso
                    new_entry = {
                        "id": wo_id,
                        "type": wo_data.get("type", "FEATURE"),
                        "title": wo_data.get("title", f"Work order {wo_id}"),
                        "status": wo_data.get("status", "ACTIVE"),
                        "priority": wo_data.get("priority", "P0"),
                        "assigned_agents": wo_data.get("assigned_agents", ["codex"]),
                        "dependencies": wo_data.get("dependencies", []),
                        "created": str(created_val),
                        "updated": str(updated_val),
                        "file": f"work-orders/ACTIVE/{wo_file.name}",
                    }
                    if wo_data.get("deliverable"):
                        new_entry["deliverable"] = wo_data["deliverable"]
                    orders.append(new_entry)
                    existing_orders[wo_id] = new_entry
                    updated_index = True
                else:
                    entry = existing_orders[wo_id]
                    if entry.get("status") != wo_data.get("status"):
                        entry["status"] = wo_data.get("status", "ACTIVE")
                        updated_index = True

            if updated_index:
                index_file.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

            # Also ensure assigned_work_orders in TREE.yaml are updated
            tree_file = sync_dir / "runtime" / "TREE.yaml"
            if tree_file.is_file():
                tree_data = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
                if isinstance(tree_data, dict) and "agents" in tree_data and isinstance(tree_data["agents"], dict):
                    updated_tree = False
                    for ag_name, wo_ids in assigned_per_agent.items():
                        if ag_name in tree_data["agents"]:
                            ag_entry = tree_data["agents"][ag_name]
                            if isinstance(ag_entry, dict):
                                curr_assigned = ag_entry.setdefault("assigned_work_orders", [])
                                for w in wo_ids:
                                    if w not in curr_assigned:
                                        curr_assigned.append(w)
                                        updated_tree = True
                    if updated_tree:
                        tree_file.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")
        except Exception:
            pass

    def _acquire_runtime_lock(self) -> tuple[bool, str, int]:
        waited_ms = 0
        for attempt in range(self.max_lock_retries):
            started = time.monotonic()
            ok, message = acquire_lock(self.sync_path, self.agent, session_id='harness')
            waited_ms += int((time.monotonic() - started) * 1000)
            if ok:
                return True, message, waited_ms
            if attempt == self.max_lock_retries - 1:
                return False, message, waited_ms
            self.sleep_fn(self.backoff_seconds * (2 ** attempt))
        return False, 'lock acquisition failed', waited_ms

    def _apply_ops(self, base_path: Path, ops: list[FileWrite | FileMove]) -> None:
        for op in ops:
            if isinstance(op, FileMove):
                source = base_path / op.source
                target = base_path / op.target
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                if source.exists():
                    source.rename(target)
                continue

            target = base_path / op.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if op.append and target.exists():
                existing = target.read_text(encoding='utf-8')
                target.write_text(existing + op.content, encoding='utf-8', newline='\n')
            else:
                target.write_text(op.content, encoding='utf-8', newline='\n')

    def _render_review_request(
        self,
        task: HarnessTask,
        decision: HarnessDecision,
        now: datetime,
    ) -> str:
        modified = '\n'.join(f'- {item}' for item in decision.modified_files) or '- none declared'
        return (
            f"# Review Request: {task.work_order_id}\n\n"
            f"from: {self.agent}\n"
            f"to: gemma\n"
            f"date: \"{now.isoformat()}\"\n\n"
            f"WO ID: {task.work_order_id}\n"
            f"Modified files:\n{modified}\n\n"
            f"Summary:\n{decision.summary}\n"
        )

    def _render_completion_notice(
        self,
        task: HarnessTask,
        decision: HarnessDecision,
        now: datetime,
    ) -> str:
        modified = '\n'.join(f'- {item}' for item in decision.modified_files) or '- none declared'
        return (
            f"# Completion Notice: {task.work_order_id}\n\n"
            f"from: {self.agent}\n"
            f"to: claude\n"
            f"date: \"{now.isoformat()}\"\n"
            f"release_target: {decision.release_target}\n\n"
            f"WO ID: {task.work_order_id}\n"
            f"Status: {decision.status}\n\n"
            f"Summary:\n{decision.summary}\n\n"
            f"Modified files:\n{modified}\n"
        )


def _first_content_line(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip('#').strip()
        if stripped:
            return stripped
    return fallback


__all__ = [
    'AgentRunner',
    'EchoLLMProvider',
    'HarnessRunResult',
    'HarnessTask',
    'LLMProvider',
]
