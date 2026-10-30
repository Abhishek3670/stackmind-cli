"""Decision validation, payload parsing and governed tool runtime wiring.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from validators.knowledge.api import KnowledgeAPI

from .models import (
    _OUTPUT_VALIDATOR,
    GovernedToolRuntime,
    HarnessDecision,
    HarnessTask,
)

class _DecisionsMixin:
    """Decision validation, payload parsing and governed tool runtime wiring."""

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
            'blocker_details',
            'modified_files',
            'release_target',
            'retrieval_queries',
            'uncertainty',
            'commands',
        }
        filtered_payload = {k: v for k, v in cleaned_payload.items() if k in allowed_keys}
        status = str(filtered_payload.get('status', ''))
        rt = filtered_payload.get('release_target')
        if rt is None or str(rt).strip().lower() in ('none', 'null', ''):
            if task.work_order_id and status == 'completed':
                filtered_payload['release_target'] = task.deliverable_path or 'patch'
            else:
                filtered_payload.pop('release_target', None)

        errors = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}"
            for error in _OUTPUT_VALIDATOR.iter_errors(filtered_payload)
        ]
        release_target = filtered_payload.get('release_target')
        blockers = tuple(str(item) for item in filtered_payload.get('blockers', []))
        blocker_details = tuple(
            dict(item) for item in filtered_payload.get('blocker_details', []) or []
            if isinstance(item, dict) and str(item.get('finding', '')).strip()
        )
        if task.work_order_id and task.deliverable_path and status == 'completed' and not str(release_target or '').strip():
            errors.append('release_target is required for completed work-order tasks')
        if status == 'blocked' and not blockers:
            errors.append('blocked decisions must declare blockers')
        if errors:
            raise ValueError('invalid harness output: ' + '; '.join(errors))
        return HarnessDecision(
            status=status,
            summary=str(filtered_payload['summary']).strip(),
            report_markdown=str(filtered_payload['report_markdown']).strip(),
            blockers=blockers,
            blocker_details=blocker_details,
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
        from validators.kernel.identity import get_role_policy
        from validators.kernel.operations import OperationJournal
        from validators.kernel.tools import ToolGateway
        from validators.kernel.workspace import ScratchWorkspace

        known_contract_exts = frozenset({
            '.md', '.yaml', '.yml', '.json', '.jsonc', '.jsonl', '.py', '.pyi', '.txt', '.toml',
            '.ini', '.cfg', '.conf', '.lock', '.html', '.htm', '.css', '.scss', '.sass', '.less',
            '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.vue', '.svelte', '.wasm',
            '.xml', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp',
            '.sh', '.bash', '.zsh', '.bat', '.cmd', '.ps1', '.sql', '.db', '.sqlite',
            '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.java', '.kt', '.cs', '.rb', '.php', '.swift',
            '.csv', '.tsv', '.rst', '.env',
        })

        def workspace_rules(rules: Any) -> tuple[str, ...]:
            converted: list[str] = []
            seen: set[str] = set()

            def add_rule(r: str) -> None:
                if r not in seen:
                    seen.add(r)
                    converted.append(r)

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
                suffix = Path(val).suffix.lower()
                has_file_ext = suffix in known_contract_exts
                is_dotted_path = val.startswith('.')

                # Always preserve the exact path explicitly named in the contract
                add_rule(f'workspace/{val}')

                if has_sep or has_wildcard or has_file_ext or is_dotted_path:
                    if not (has_file_ext or has_wildcard):
                        clean_dir = val.rstrip('/')
                        add_rule(f'workspace/{clean_dir}')
                        add_rule(f'workspace/{clean_dir}/**')
                else:
                    mod_path = val.replace('.', '/').rstrip('/')
                    if mod_path != val:
                        add_rule(f'workspace/{mod_path}')
                        add_rule(f'workspace/{mod_path}/**')
                    else:
                        add_rule(f'workspace/{val}/**')
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
                query,
                token_budget=self.token_budget,
                limit=self.context_limit,
                contract=task_contract,
                profile=self.execution_profile,
                skills=getattr(task_contract, "skills", None),
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

        def _try_load(s: str) -> dict[str, Any] | None:
            if not s:
                return None
            s_clean = s.strip()
            try:
                obj = json.loads(s_clean, strict=False)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            # Healing pass: double-escape invalid JSON escape backslashes (e.g. \s, \d, \w, \.)
            try:
                healed = re.sub(r'\\(?![/\\bfnrtu"\'\n\r\t]|u[0-9a-fA-F]{4})', r'\\\\', s_clean)
                obj = json.loads(healed, strict=False)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            return None

        # 1. Try markdown code block
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            payload = _try_load(match.group(1))

        # 2. Try raw json loads
        if payload is None:
            payload = _try_load(raw_text)

        # 3. Try slice from first '{' to last '}'
        if payload is None and '{' in raw_text and '}' in raw_text:
            first_brace = raw_text.find('{')
            last_brace = raw_text.rfind('}')
            if last_brace > first_brace:
                payload = _try_load(raw_text[first_brace:last_brace + 1])

        # 4. Try raw_decode from first {
        if payload is None and '{' in raw_text:
            idx = raw_text.find('{')
            try:
                obj, _ = json.JSONDecoder(strict=False).raw_decode(raw_text, idx)
                if isinstance(obj, dict):
                    payload = obj
            except Exception:
                pass
            if payload is None:
                try:
                    healed_full = re.sub(r'\\(?![/\\bfnrtu"\'\n\r\t]|u[0-9a-fA-F]{4})', r'\\\\', raw_text)
                    idx = healed_full.find('{')
                    if idx != -1:
                        obj, _ = json.JSONDecoder(strict=False).raw_decode(healed_full, idx)
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
