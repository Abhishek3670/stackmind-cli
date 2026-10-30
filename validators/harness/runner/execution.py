"""The governed turn loop (run_once).

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from typing import Any, Sequence

import yaml

from cli.lock import release_lock
from validators.harness.task_contract import reconcile_modified_files
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.contract import ContractAccessDenied, ContractExpiredError

from .models import (
    HARNESS_SNAPSHOT_IGNORED,
    FileWrite,
    HarnessRunResult,
    HarnessTask,
    LLMRequest,
    LoopSafetyError,
    provider_stream_interruption_result,
)

class _ExecutionMixin:
    """The governed turn loop (run_once)."""

    def run_once(
        self,
        cancellation: Event | None = None,
        operation_id: str | None = None,
        *,
        cancel_event: Event | None = None,
        prompt: str | None = None,
        work_order_id: str | None = None,
        is_authoring: bool = False,
        is_delivery_status: bool = False,
        session_history: Sequence[dict[str, str]] | None = None,
    ) -> HarnessRunResult:
        """Run one task, cooperatively stopping at operation lifecycle boundaries."""
        if cancel_event is not None:
            if cancellation is not None and cancellation is not cancel_event:
                raise ValueError("only one cancellation event may be provided")
            cancellation = cancel_event
        try:
            tree_data = self._load_tree()
            self._ensure_protocol_citizenship(tree_data)
            if prompt and not work_order_id and not is_authoring:
                adhoc_file = self.sync_path / 'inbox' / self.agent / 'adhoc.md'
                m_tf = re.search(r"Target files?:\s*\n?\s*[-*]?\s*([a-zA-Z0-9_./\\-]+)", prompt)
                deliv_path = m_tf.group(1).strip() if m_tf else None
                task = HarnessTask(
                    kind='delivery_status' if is_delivery_status else 'adhoc',
                    is_delivery_status=is_delivery_status,
                    identifier=operation_id or 'adhoc',
                    path=adhoc_file,
                    title=prompt.strip() or 'User Prompt',
                    body=prompt.strip() or 'User Prompt',
                    query=prompt.strip() or 'User Prompt',
                    is_authoring=False,
                    work_order_type=None,
                    deliverable_type='code' if deliv_path else None,
                    deliverable_path=deliv_path,
                )
            else:
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
                        deliverable_paths=(),
                    )
            if task is None and is_authoring and prompt and prompt.strip():
                authoring_id = work_order_id or "WO-000"
                task = HarnessTask(
                    kind='work_order',
                    identifier=authoring_id,
                    path=self.sync_path / 'work-orders' / 'ACTIVE' / f"{authoring_id}.yaml",
                    title=f"Authoring: {prompt.strip()[:80]}",
                    body=prompt.strip(),
                    query=prompt.strip(),
                    work_order_id=authoring_id,
                    deliverable_path=None,
                    is_authoring=True,
                    work_order_type='RESEARCH',
                    deliverable_type=None,
                    deliverable_paths=(),
                )
            if task is None:
                return HarnessRunResult(
                    status='idle',
                    persisted=False,
                    task_id=None,
                    reason='no inbox items or assigned work orders',
                )

            if task is not None and task.kind != 'adhoc' and prompt and prompt.strip():
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
                        deliverable_paths=task.deliverable_paths,
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
            assigned_skills = list(getattr(task_contract, "skills", []) or [])
            if not assigned_skills and task.work_order_id:
                for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
                    wo_path = self.sync_path / 'work-orders' / sub / f'{task.work_order_id}.yaml'
                    if wo_path.exists():
                        try:
                            wo_data = yaml.safe_load(wo_path.read_text(encoding='utf-8'))
                            if isinstance(wo_data, dict) and wo_data.get("skills"):
                                assigned_skills = list(wo_data["skills"])
                        except Exception:
                            pass
                        break

            knowledge_api = KnowledgeAPI(self.project_path)
            query_str = f"{task.query} {task.body}".strip() if task.body else task.query
            if task.deliverable_path:
                query_str = f"{query_str} {task.deliverable_path}".strip()
            try:
                context = knowledge_api.assemble_context(
                    query_str,
                    token_budget=self.token_budget,
                    limit=self.context_limit,
                    contract=task_contract,
                    profile=self.execution_profile,
                    skills=assigned_skills or None,
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
                WorkspaceDiff,
                WorkspaceSnapshot,
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
                session_count=int(tree_data.get('agents', {}).get(self.agent, {}).get('session_count', 0)),
                task=task,
                context=context,
                retrieval=retrieval,
                on_token=self.on_token,
                cancellation=cancellation,
                session_history=session_history,
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
                import builtins
                import socket

                from validators.kernel.providers.errors import (
                    BudgetExceededError,
                    ConsecutiveToolFailureError,
                    NoProgressLoopError,
                    OperationCancelledError,
                    ProviderStreamInterruptedError,
                    TimeoutError,
                    ToolLimitExceededError,
                    ToolLoopExhaustedError,
                )
                if isinstance(exc, OperationCancelledError) or self._is_cancelled(cancellation):
                    return self._cancelled_result(task, operation_id)
                if isinstance(exc, ProviderStreamInterruptedError):
                    # The provider stopped producing before its terminal signal.
                    # Keep what the model already emitted and name the concrete
                    # failure instead of discarding it or retrying blindly.
                    return provider_stream_interruption_result(
                        task, exc, operation_id=operation_id
                    )
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
                    raw_summary = strip_internal_reasoning(str(completion.payload.get('summary') or '')).strip()
                    raw_report = strip_internal_reasoning(str(completion.payload.get('report_markdown') or '')).strip()
                    raw_thinking = (
                        completion.payload.get('thinking')
                        or completion.payload.get('reasoning')
                        or completion.payload.get('thought')
                        or completion.payload.get('reasoning_content')
                        or extract_internal_reasoning(str(completion.payload.get('summary') or ''))
                        or extract_internal_reasoning(str(completion.payload.get('report_markdown') or ''))
                    )
                    if raw_summary or raw_report:
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
                        try:
                            from validators.kernel.sync_git import commit_sync_governance
                            commit_sync_governance(
                                self.project_path,
                                f"chore(governance): record adhoc turn by {self.agent} [{task.identifier[:8]}]",
                                author=self.agent,
                            )
                        except Exception:
                            pass
                        meta: dict[str, Any] = {
                            'summary': raw_summary or raw_report,
                            'report_markdown': raw_report or raw_summary,
                        }
                        if raw_thinking:
                            meta['thinking'] = raw_thinking
                        if hasattr(completion, 'meta') and isinstance(completion.meta, dict):
                            for k in ('tool_calls_audit', 'commands_audit', 'backend_id', 'model', 'triggered_goal', 'delegated_tasks'):
                                if k in completion.meta:
                                    meta[k] = completion.meta[k]
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
                        expected_release = json.dumps(task.deliverable_path or "patch") if task.work_order_id else "null"
                        feedback_prompt = (
                            f"Your previous output decision failed schema validation with error: {exc}.\n"
                            "Please correct your response and return ONLY valid JSON matching this schema:\n"
                            "If status is 'completed':\n"
                            '{\n  "status": "completed",\n  "summary": "...",\n  "report_markdown": "...",\n  "blockers": [],\n  "release_target": ' + expected_release + '\n}\n'
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
                                if task.work_order_id and retry_payload.get('status') == 'completed' and not str(retry_payload.get('release_target') or '').strip():
                                    retry_payload['release_target'] = task.deliverable_path or 'patch'
                                elif not retry_payload.get('release_target') or str(retry_payload.get('release_target')).strip().lower() in ('none', 'null', ''):
                                    retry_payload.pop('release_target', None)
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
                    meta={
                        'error': reason_str,
                        'blockers': [reason_str],
                        'failure': {
                            'failure_code': 'HARNESS_DECISION_INVALID',
                            'canonical_message': f"Harness decision validation failed after retries: {reason_str}",
                            'work_order_id': task.work_order_id,
                            'raw_reason': reason_str,
                        },
                    },
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

            # The write_file journal is authoritative for what the turn actually
            # touched: reconcile the model's self-reported modified_files against
            # it so undeclared-but-written files are reported honestly instead of
            # failing the declaration gate, and claimed-but-never-written files
            # stay a declaration failure.
            auto_added_files: list[str] = []
            journal = getattr(tool_runtime, "boundary", None) and tool_runtime.boundary.journal
            if decision is not None and journal is not None:
                written_paths = [
                    rec.request.target.replace('workspace/', '', 1).replace('\\', '/')
                    for rec in journal.records
                    if rec.request.operation_type.value == 'write_file' and rec.result == 'written'
                ]
                decision, auto_added_files = reconcile_modified_files(decision, written_paths)

            if self.agent == 'claude' and decision is not None:
                decision, auto_added_files = self._reconcile_authored_work_orders(
                    workspace_root, decision, auto_added_files
                )

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
                'blocker_details': [dict(d) for d in (getattr(decision, 'blocker_details', ()) or ())],
                'auto_added_files': auto_added_files,
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
                    tool_written: set[str] = set()
                    if tool_runtime is not None:
                        journal = getattr(tool_runtime, "boundary", None) and tool_runtime.boundary.journal
                        if journal is not None:
                            tool_written.update(
                                Path(rec.request.target.replace('workspace/', '', 1)).as_posix().lstrip('/')
                                for rec in journal.records
                                if rec.request.operation_type.value == 'write_file' and rec.result == 'written'
                            )
                        if hasattr(tool_runtime, "gateway") and hasattr(tool_runtime.gateway, "written_files"):
                            tool_written.update(Path(p).as_posix().lstrip('/') for p in tool_runtime.gateway.written_files)
                    if not tool_written:
                        for tc in (getattr(completion, "meta", None) or {}).get("tool_calls_audit", []):
                            if isinstance(tc, dict) and tc.get("tool") == "write_file" and tc.get("path"):
                                tool_written.add(Path(str(tc["path"])).as_posix().lstrip('/'))
                    if self.agent in ("local-llm", "gitops"):
                        for rel_file in ("VERSION.md", "CHANGELOG.md"):
                            if rel_file in dec_norm and (staged_root / rel_file).is_file():
                                tool_written.add(rel_file)

                    idempotent_writes = {
                        p for p in dec_norm
                        if p in tool_written and (staged_root / p).is_file()
                    }
                    all_observed = set(diff.all_changed_files) | idempotent_writes
                    observed_task_files = tuple(
                        norm_p for path in sorted(all_observed)
                        if (norm_p := Path(path).as_posix().lstrip('/'))
                        and not (any(norm_p.startswith(prefix) for prefix in bookkeeping_prefixes) and norm_p not in dec_norm)
                        and (task_wo_rel is None or norm_p != task_wo_rel or norm_p in dec_norm)
                    )
                    phantom_files = {
                        p for p in dec_norm
                        if not (staged_root / p).is_file() and (
                            p.startswith(('_scratch', 'scratch', '.sync/runtime'))
                            or any(p.startswith(prefix) for prefix in bookkeeping_prefixes)
                        )
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
                    stage_inputs['verification_evidence'] = getattr(self, '_last_verification_evidence', {})
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
                        meta_dict: dict[str, Any] = {
                            'commands_audit': stage_inputs.get('commands_audit', []),
                            'blocker_details': stage_inputs.get('blocker_details', []),
                        }
                        if 'verification_evidence' in stage_inputs:
                            meta_dict['verification_evidence'] = stage_inputs['verification_evidence']
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
                        if not dimensions.outcome_verified and task.work_order_id and not getattr(task, 'is_authoring', False):
                            meta_dict['failure'] = {
                                'failure_code': 'OUTCOME_NOT_VERIFIED',
                                'work_order_id': task.work_order_id,
                                'canonical_message': reason_msg,
                                'verification_evidence': stage_inputs.get('verification_evidence'),
                            }
                            if task.deliverable_path:
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
                    self._apply_verified_workspace_diff(staged_root, diff, extra_paths=sorted(idempotent_writes))

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

            try:
                from validators.kernel.sync_git import commit_sync_governance
                commit_sync_governance(
                    self.project_path,
                    f"chore(governance): record {decision.status} turn by {self.agent} [{task.identifier[:8]}]",
                    author=self.agent,
                )
            except Exception:
                pass

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
                meta={
                    'failure': {
                        'failure_code': 'LOOP_GUARD_TRIPPED',
                        'canonical_message': str(exc),
                    },
                    'blockers': [str(exc)],
                },
            )
