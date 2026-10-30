"""Staged-state validation, write staging, report/events rendering.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from cli.validate import validate as validate_runtime
from validators.harness.retrieval import (
    RetrievalBatch,
)
from validators.knowledge.api import ContextBundle

from .models import (
    CompletionRecord,
    FileMove,
    FileWrite,
    HarnessDecision,
    HarnessTask,
)

class _StagingMixin:
    """Staged-state validation, write staging, report/events rendering."""

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
                if self._is_architect_agent():
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
                if self._is_architect_agent():
                    self._reconcile_staged_work_order_collisions(staged_root)
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
            self._sync_index_and_tree_yaml(staged_root)
            from cli.init import ensure_canonical_tree, ensure_workspace_protocol_scaffolding
            ensure_workspace_protocol_scaffolding(staged_root)
            ensure_canonical_tree(staged_root)
            validation = validate_runtime(staged_root)
            if any("runtime/TREE.yaml" in e.message for e in validation.errors):
                ensure_canonical_tree(staged_root)
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
            if wo_log_path is not None and wo_log_path.exists():
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
        skill_diag = getattr(stage_inputs['context'], 'skill_diagnostics', None)
        if isinstance(skill_diag, dict):
            skills_injected = skill_diag.get('injected_ids') or skill_diag.get('injected', [])
            skill_tokens_est = skill_diag.get('estimated_skill_tokens') or skill_diag.get('token_estimate', 0)
        elif skill_diag is not None:
            skills_injected = getattr(skill_diag, 'injected_ids', getattr(skill_diag, 'injected', []))
            skill_tokens_est = getattr(skill_diag, 'estimated_skill_tokens', getattr(skill_diag, 'token_estimate', 0))
        else:
            skills_injected = []
            skill_tokens_est = 0

        event = {
            'agent': self.agent,
            'execution_profile': self.execution_profile.value,
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
            'skills_injected': list(skills_injected) if isinstance(skills_injected, (list, tuple)) else [],
            'skill_tokens_estimated': int(skill_tokens_est) if isinstance(skill_tokens_est, (int, float)) else 0,
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
            f'- profile: `{self.execution_profile.value}`',
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
        meta_out: dict[str, Any] = {
            'agent': self.agent,
            'execution_profile': self.execution_profile.value,
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
            'blocker_details': stage_inputs.get('blocker_details', []),
            'verification_evidence': stage_inputs.get('verification_evidence', {}),
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

        # Skill retrieval diagnostics
        skill_diag = getattr(context, 'skill_diagnostics', None)
        if skill_diag is not None:
            meta_out['skill_diagnostics'] = (
                skill_diag.to_dict() if hasattr(skill_diag, 'to_dict') else dict(skill_diag)
            )

        if isinstance(skill_diag, dict):
            skill_considered = skill_diag.get('considered_ids', skill_diag.get('considered', []))
            skill_selected = skill_diag.get('selected_ids', skill_diag.get('selected', []))
            skill_loaded = skill_diag.get('loaded_ids', skill_diag.get('loaded', []))
            skill_injected = skill_diag.get('injected_ids', skill_diag.get('injected', []))
            skill_token_est = skill_diag.get('estimated_skill_tokens', skill_diag.get('token_estimate', 0))
        elif skill_diag is not None:
            skill_considered = getattr(skill_diag, 'considered_ids', getattr(skill_diag, 'considered', []))
            skill_selected = getattr(skill_diag, 'selected_ids', getattr(skill_diag, 'selected', []))
            skill_loaded = getattr(skill_diag, 'loaded_ids', getattr(skill_diag, 'loaded', []))
            skill_injected = getattr(skill_diag, 'injected_ids', getattr(skill_diag, 'injected', []))
            skill_token_est = getattr(skill_diag, 'estimated_skill_tokens', getattr(skill_diag, 'token_estimate', 0))
        else:
            skill_considered = []
            skill_selected = []
            skill_loaded = []
            skill_injected = []
            skill_token_est = 0

        # Cycle telemetry
        llm_ms = stage_inputs.get('llm_ms', 0)
        poll_ms = stage_inputs.get('poll_ms', 0)
        retrieval_ms = stage_inputs.get('retrieval_ms', 0)
        total_wall_ms = llm_ms + poll_ms + retrieval_ms + write_ms
        meta_out['telemetry'] = {
            'execution_profile': self.execution_profile.value,
            'cycle_start': stage_inputs.get('run_at').isoformat() if stage_inputs.get('run_at') else None,
            'latency_ms': {
                'llm': llm_ms,
                'poll': poll_ms,
                'retrieval': retrieval_ms,
                'write': write_ms,
                'total_wall_ms': total_wall_ms,
            },
            'completion_tokens': completion.completion_tokens,
            'prompt_tokens': completion.prompt_tokens,
            'tokens_measured': bool(completion.completion_tokens > 0 or completion.prompt_tokens > 0),
            'skill_candidates_considered': list(skill_considered) if isinstance(skill_considered, (list, tuple)) else [],
            'skill_candidates_selected': list(skill_selected) if isinstance(skill_selected, (list, tuple)) else [],
            'skill_loaded': list(skill_loaded) if isinstance(skill_loaded, (list, tuple)) else [],
            'skill_injected': list(skill_injected) if isinstance(skill_injected, (list, tuple)) else [],
            'skill_token_estimate': int(skill_token_est) if isinstance(skill_token_est, (int, float)) else 0,
            'skill_token_usage_measured': False,
            'model_calls': 1,
            'tool_calls_count': len(stage_inputs.get('tool_calls_audit', [])),
            'verification_passed': dimensions.all_passed if dimensions else True,
            'status': decision.status,
            'blockers': list(decision.blockers) if decision.blockers else [],
        }

        if decision.status == 'blocked':
            gt = completion.meta.get('guard_trip') if hasattr(completion, 'meta') else None
            if gt:
                meta_out['failure'] = {
                    'failure_code': 'LOOP_GUARD_TRIPPED',
                    'canonical_message': str(gt),
                }
            elif stage_inputs.get('failure'):
                meta_out['failure'] = stage_inputs['failure']
        return meta_out
