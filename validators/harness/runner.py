"""Governed agent execution loop for StackMind Phase 8."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Protocol

import yaml
from jsonschema import Draft7Validator

from cli.lock import acquire_lock, release_lock
from cli.validate import validate as validate_runtime
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


@dataclass(frozen=True)
class LLMRequest:
    """Prompt package handed to an LLM provider."""

    agent: str
    session_count: int
    task: HarnessTask
    context: ContextBundle
    retrieval: RetrievalBatch

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
        search_provider: SearchProvider | None = None,
        retrieval_policy: RetrievalPolicy | None = None,
        token_budget: int = 1200,
        context_limit: int = 8,
        max_lock_retries: int = 3,
        backoff_seconds: float = 0.1,
        now_fn: Any | None = None,
        sleep_fn: Any | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.sync_path = self.project_path / '.sync'
        self.agent = agent
        self.backend = backend
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
        self.search_tool = SessionSearchTool(search_provider, policy=retrieval_policy)
        self.token_budget = token_budget
        self.context_limit = context_limit
        self.max_lock_retries = max_lock_retries
        self.backoff_seconds = backoff_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc).astimezone())
        self.sleep_fn = sleep_fn or time.sleep
        self.reader = ResourceReadTracker(max_reads=3)
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
    ) -> HarnessRunResult:
        """Run one task, cooperatively stopping at operation lifecycle boundaries."""
        if cancel_event is not None:
            if cancellation is not None and cancellation is not cancel_event:
                raise ValueError("only one cancellation event may be provided")
            cancellation = cancel_event
        try:
            tree_data = self._load_tree()
            self._ensure_protocol_citizenship(tree_data)
            task = self.discover_next_task(tree_data)
            if task is None and prompt:
                adhoc_file = self.sync_path / 'inbox' / self.agent / 'adhoc.md'
                task = HarnessTask(
                    kind='adhoc',
                    identifier=operation_id or 'adhoc',
                    path=adhoc_file,
                    title=prompt.strip() or 'User Prompt',
                    body=prompt.strip() or 'User Prompt',
                    query=prompt.strip() or 'User Prompt',
                )
            if task is None:
                return HarnessRunResult(
                    status='idle',
                    persisted=False,
                    task_id=None,
                    reason='no inbox items or assigned work orders',
                )

            # 1. Post-task discovery.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            run_at = self.now_fn()
            poll_started = time.monotonic()
            context = KnowledgeAPI(self.project_path).assemble_context(
                task.query,
                token_budget=self.token_budget,
                limit=self.context_limit,
            )
            poll_ms = int((time.monotonic() - poll_started) * 1000)

            # 2. Post-context assembly.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            # Pre-execution plan verification
            from validators.harness.contract_gate import verify_pre_execution
            try:
                verify_pre_execution(self.project_path, self.agent, task, context)
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

            # Phase 0: Capture runner-owned before snapshot
            before_snapshot = WorkspaceSnapshot.capture(self.project_path)

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
            )

            llm_started = time.monotonic()
            try:
                if self.backend is not None and hasattr(self.backend, 'start_operation'):
                    self.backend.start_operation(task=task, context=context, operation_id=operation_id)
                completion = self.llm_provider.complete(request)
                if self.backend is not None and hasattr(self.backend, 'report_result') and operation_id:
                    self.backend.report_result(operation_id)
            except Exception as exc:
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

            try:
                decision = self._validate_decision(task, completion.payload)
            except ValueError as exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=str(exc),
                )

            # 7. Post-decision validation.
            if self._is_cancelled(cancellation):
                return self._cancelled_result(task, operation_id)

            # Post-execution declared validation
            from validators.harness.contract_gate import verify_post_execution
            try:
                verify_post_execution(self.project_path, self.agent, task, decision)
            except Exception as exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=f'Post-execution contract validation failed: {exc}',
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
                'task': task,
                'llm_ms': llm_ms,
                'run_at': run_at,
            }
            staged_errors = self._validate_staged_state(stage_inputs)
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

                # Run all state-changing work in an isolated copy.  Nothing reaches the
                # live workspace until its observed diff has passed every verification gate.
                with tempfile.TemporaryDirectory() as tmp_dir:
                    staged_root = Path(tmp_dir) / self.project_path.name
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
                        try:
                            command_results.append(
                                subprocess.run(
                                    cmd,
                                    shell=True,
                                    cwd=str(staged_root),
                                    check=False,
                                    capture_output=True,
                                    text=True,
                                )
                            )
                        except OSError as exc:
                            command_results.append(
                                subprocess.CompletedProcess(cmd, returncode=-1, stderr=type(exc).__name__)
                            )

                    after_snapshot = WorkspaceSnapshot.capture(staged_root)
                    diff = before_snapshot.diff(after_snapshot)
                    # LLM declarations describe task changes, not harness-owned audit,
                    # inbox, and report bookkeeping under `.sync/`.
                    observed_task_files = tuple(
                        path for path in diff.all_changed_files if not path.startswith('.sync/')
                    )
                    declaration_matches = set(observed_task_files) == set(decision.modified_files)
                    mismatch_reason = (
                        None if declaration_matches
                        else f'declared {sorted(decision.modified_files)} != observed {sorted(observed_task_files)}'
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
                        has_staged_writes=(task.kind == 'inbox' or task.work_order_id is not None),
                    )
                    if not dimensions.all_passed:
                        failed = [name for name, passed in dimensions.to_dict().items() if name != 'all_passed' and not passed]
                        return HarnessRunResult(
                            status='blocked',
                            persisted=False,
                            task_id=task.identifier,
                            reason='verification gate failed: ' + ', '.join(failed),
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

    def discover_next_task(self, tree_data: dict[str, Any]) -> HarnessTask | None:
        inbox_dir = self.sync_path / 'inbox' / self.agent
        inbox_candidates = sorted(
            path
            for path in inbox_dir.iterdir()
            if path.is_file() and path.name != '.gitkeep'
        )
        if inbox_candidates:
            selected = inbox_candidates[0]
            body = self._read_text(selected)
            return HarnessTask(
                kind='inbox',
                identifier=selected.name,
                path=selected,
                title=_first_content_line(body, fallback=selected.stem),
                body=body,
                query=_first_content_line(body, fallback=selected.stem),
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
                'status': 'IDLE',
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
        errors = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}"
            for error in _OUTPUT_VALIDATOR.iter_errors(payload)
        ]
        status = str(payload.get('status', ''))
        release_target = payload.get('release_target')
        blockers = tuple(str(item) for item in payload.get('blockers', []))
        if task.work_order_id and status == 'completed' and not str(release_target or '').strip():
            errors.append('release_target is required for completed work-order tasks')
        if status == 'blocked' and not blockers:
            errors.append('blocked decisions must declare blockers')
        if errors:
            raise ValueError('invalid harness output: ' + '; '.join(errors))
        return HarnessDecision(
            status=status,
            summary=str(payload['summary']).strip(),
            report_markdown=str(payload['report_markdown']).strip(),
            blockers=blockers,
            modified_files=tuple(str(item) for item in payload.get('modified_files', [])),
            release_target=str(release_target).strip() if release_target else None,
            retrieval_queries=tuple(str(item) for item in payload.get('retrieval_queries', [])),
            uncertainty=tuple(str(item) for item in payload.get('uncertainty', [])),
            commands=tuple(str(item) for item in payload.get('commands', [])),
        )

    def _validate_staged_state(self, stage_inputs: dict[str, Any]) -> list[str]:
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
            cached_payload = stage_inputs.get('_work_order_payload')
            if cached_payload is None:
                cached_payload = self._read_yaml(task.path)
                stage_inputs['_work_order_payload'] = dict(cached_payload)
            payload = dict(cached_payload)
            log_entries = list(payload.get('log', []))
            log_entries.append(f"{now.isoformat()} harness {decision.status}: {decision.summary}")
            payload['log'] = log_entries
            payload['updated'] = now.isoformat()
            content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
            ops.append(FileWrite(task.path.relative_to(self.project_path), content))

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
        if retrieval.evidence:
            sections.extend(['', '## External Evidence'])
            sections.extend(item.render() for item in retrieval.evidence)
        if decision.blockers:
            sections.extend(['', '## Blockers'])
            sections.extend(f'- {item}' for item in decision.blockers)
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
            'cost_estimate': round(completion.cost_estimate + retrieval.cost_estimate, 6),
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
            'report_markdown': decision.report_markdown,
            'observed_changes': diff.to_dict() if diff else {},
            'prompt_tokens': completion.prompt_tokens,
            'provider': completion.provider,
            'retrieval_cap_exhausted': retrieval.cap_exhausted,
            'searches_used': retrieval.searches_used,
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
    ) -> Any:
        """Derive verification flags from the staged filesystem and command telemetry."""
        from validators.harness.contract_gate import verify_post_execution
        from validators.harness.snapshot import VerificationDimensions

        changed_files = tuple(diff.all_changed_files)
        task_changed_files = tuple(
            relative_path for relative_path in changed_files if not relative_path.startswith('.sync/')
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
        if task.work_order_id:
            contract_path = self.sync_path / 'contracts' / f'{task.work_order_id}.yaml'
            if contract_path.exists():
                raw_contract = self._read_yaml(contract_path)
                allow_rules = raw_contract.get('scope', {}).get('allow', [])
                deny_rules = raw_contract.get('scope', {}).get('deny', [])
                allow_paths = [str(rule.get('path', '')) for rule in allow_rules if isinstance(rule, dict)]
                deny_paths = [str(rule.get('path', '')) for rule in deny_rules if isinstance(rule, dict)]
                for relative_path in task_changed_files:
                    normalized = Path(relative_path).as_posix()
                    if normalized.startswith('.sync/'):
                        continue  # Harness-owned bookkeeping writes are authorized separately.
                    allowed = any(
                        normalized == rule or normalized.startswith(rule.rstrip('/') + '/')
                        for rule in allow_paths
                    )
                    denied = any(
                        normalized == rule or normalized.startswith(rule.rstrip('/') + '/')
                        for rule in deny_paths
                    )
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
                        ast.parse(candidate.read_text(encoding='utf-8'))
                    except (OSError, UnicodeDecodeError, SyntaxError):
                        code_verified = False
        for result in command_results:
            command = str(result.args).lower()
            if ('pytest' in command or 'test' in command) and result.returncode != 0:
                code_verified = False

        behavioral_verified = (
            decision.status == 'completed'
            and all(result.returncode == 0 for result in command_results)
        )

        security_verified = d025_passed and all(
            not Path(relative_path).is_absolute() and '..' not in Path(relative_path).parts
            for relative_path in changed_files
        )
        if security_verified:
            from validators.kernel.security import scan_for_credential_leaks

            for relative_path in changed_files:
                candidate = staged_root / relative_path
                if candidate.is_file():
                    try:
                        if scan_for_credential_leaks(candidate.read_text(encoding='utf-8', errors='replace')):
                            security_verified = False
                            break
                    except OSError:
                        security_verified = False
                        break

        deliverable_exists = False
        if task.deliverable_path:
            deliverable = staged_root / task.deliverable_path
            deliverable_exists = deliverable.exists()
        elif task.kind == 'inbox':
            # Inbox tasks are intentionally archived by the staged operation; the
            # task selected at discovery is itself the completed deliverable.
            deliverable_exists = True
        elif task.kind == 'adhoc':
            # Ad-hoc prompt turns produce conversational completion/report deliverables.
            deliverable_exists = bool(
                (decision.summary and decision.summary.strip())
                or (decision.report_markdown and decision.report_markdown.strip())
            )
        outcome_verified = (
            decision.status == 'completed'
            and not decision.blockers
            and (deliverable_exists or bool(changed_files) or has_staged_writes)
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
