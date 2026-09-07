"""Governed agent execution loop for StackMind Phase 8."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
        self.llm_provider = llm_provider or EchoLLMProvider()
        self.search_tool = SessionSearchTool(search_provider, policy=retrieval_policy)
        self.token_budget = token_budget
        self.context_limit = context_limit
        self.max_lock_retries = max_lock_retries
        self.backoff_seconds = backoff_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc).astimezone())
        self.sleep_fn = sleep_fn or time.sleep
        self.reader = ResourceReadTracker(max_reads=3)
        self._tree_cache: dict[str, Any] | None = None

    def run_once(self) -> HarnessRunResult:
        try:
            tree_data = self._load_tree()
            self._ensure_protocol_citizenship(tree_data)
            task = self.discover_next_task(tree_data)
            if task is None:
                return HarnessRunResult(
                    status='idle',
                    persisted=False,
                    task_id=None,
                    reason='no inbox items or assigned work orders',
                )

            run_at = self.now_fn()
            poll_started = time.monotonic()
            context = KnowledgeAPI(self.project_path).assemble_context(
                task.query,
                token_budget=self.token_budget,
                limit=self.context_limit,
            )
            poll_ms = int((time.monotonic() - poll_started) * 1000)

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

            from validators.harness.snapshot import (
                WorkspaceSnapshot,
                WorkspaceDiff,
                VerificationDimensions,
                TrustLevel,
                evaluate_learning_eligibility,
            )

            # Phase 0: Capture runner-owned before snapshot
            before_snapshot = WorkspaceSnapshot.capture(self.project_path)

            retrieval_started = time.monotonic()
            retrieval = self.search_tool.search(task.query, limit=3)
            retrieval_ms = int((time.monotonic() - retrieval_started) * 1000)

            request = LLMRequest(
                agent=self.agent,
                session_count=int(tree_data['agents'][self.agent].get('session_count', 0)),
                task=task,
                context=context,
                retrieval=retrieval,
            )

            llm_started = time.monotonic()
            completion = self.llm_provider.complete(request)
            llm_ms = completion.latency_ms or int((time.monotonic() - llm_started) * 1000)
            try:
                decision = self._validate_decision(task, completion.payload)
            except ValueError as exc:
                return HarnessRunResult(
                    status='blocked',
                    persisted=False,
                    task_id=task.identifier,
                    reason=str(exc),
                )

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
                self._apply_non_report_writes(stage_inputs)
                write_ms = int((time.monotonic() - hold_started) * 1000)
                hold_ms = write_ms

                # Execute bash commands after applying ops
                if decision.commands:
                    from validators.harness.d025_gate import D025Gate, D025ViolationError
                    gate = D025Gate()
                    gate_decision = gate.evaluate_sequence(decision.commands)
                    gate.log_decision(self.project_path, self.agent, gate_decision, task_id=task.identifier)
                    if not gate_decision.passed:
                        raise D025ViolationError(
                            f"Command sequence triggered D025 Destructive Operations Safeguard: {gate_decision.reason}"
                        )
                    import subprocess
                    for cmd in decision.commands:
                        subprocess.run(cmd, shell=True, cwd=str(self.project_path), check=True)

                # Phase 0: Capture runner-owned after snapshot & derive authoritative diff
                after_snapshot = WorkspaceSnapshot.capture(self.project_path)
                diff = before_snapshot.diff(after_snapshot)
                declaration_matches, mismatch_reason = diff.matches_declaration(decision.modified_files)

                # Enforce contract on observed changes if modifications occurred
                if diff.all_changed_files:
                    verify_post_execution(
                        self.project_path,
                        self.agent,
                        task,
                        decision,
                        observed_files=diff.all_changed_files,
                    )

                dimensions = VerificationDimensions(
                    scope_verified=True,
                    state_verified=len(staged_errors) == 0,
                    code_verified=True,
                    behavioral_verified=decision.status == 'completed',
                    security_verified=True,
                    outcome_verified=decision.status == 'completed' and not decision.blockers,
                )
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
        outbox_dir = self.sync_path / 'outbox' / self.agent
        if self.agent not in tree_data.get('agents', {}):
            raise ValueError(f"Agent '{self.agent}' missing from TREE.yaml")
        for path in (boot_file, contract_file):
            if not path.exists():
                raise ValueError(
                    f"Agent '{self.agent}' is not protocol-registered ({path.name} missing)"
                )
        for path in (inbox_dir, outbox_dir):
            if not path.is_dir():
                raise ValueError(f"Agent '{self.agent}' is missing runtime directory {path.name}")

    def _load_tree(self) -> dict[str, Any]:
        if self._tree_cache is None:
            self._tree_cache = self._read_yaml(self.sync_path / 'runtime' / 'TREE.yaml')
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
            payload = self._read_yaml(task.path).copy()
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
            'model': completion.model,
            'observed_changes': diff.to_dict() if diff else {},
            'prompt_tokens': completion.prompt_tokens,
            'provider': completion.provider,
            'retrieval_cap_exhausted': retrieval.cap_exhausted,
            'searches_used': retrieval.searches_used,
            'trust_level': trust_level.value if hasattr(trust_level, 'value') else str(trust_level or 'OBSERVABLE'),
            'uncertainty': list(decision.uncertainty),
            'verification_dimensions': dimensions.to_dict() if dimensions else {},
        }

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
