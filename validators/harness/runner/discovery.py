"""Next-task discovery and tree/session file loading.

Part of the :mod:`validators.harness.runner` package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import (
    HarnessTask,
    _first_content_line,
)

class _DiscoveryMixin:
    """Next-task discovery and tree/session file loading."""

    def discover_next_task(
        self, tree_data: dict[str, Any], work_order_id: str | None = None
    ) -> HarnessTask | None:
        if work_order_id:
            # Explicit ids resolve against every work-order state: recovery
            # repair turns target work orders that the recovery itself may
            # have moved to BLOCKED/COMPLETED — never fall back to inbox
            # processing for an explicitly addressed work order.
            for sub in ("ACTIVE", "BLOCKED", "COMPLETED"):
                path = self.sync_path / 'work-orders' / sub / f'{work_order_id}.yaml'
                if path.exists():
                    payload = self._read_yaml(path)
                    deliverable = payload.get('deliverable', {}) if isinstance(payload, dict) else {}
                    deliv_paths = []
                    if isinstance(deliverable, dict) and deliverable.get('path'):
                        deliv_paths.append(str(deliverable['path']))
                    elif isinstance(deliverable, str) and deliverable.strip():
                        deliv_paths.append(deliverable.strip())
                    for extra in (payload.get('deliverables') or []) if isinstance(payload, dict) else []:
                        if isinstance(extra, dict) and extra.get('path'):
                            deliv_paths.append(str(extra['path']))
                        elif isinstance(extra, str) and extra.strip():
                            deliv_paths.append(extra.strip())
                    norm_deliv_tuple = tuple(dict.fromkeys(Path(p).as_posix().lstrip('/') for p in deliv_paths if p.strip()))
                    deliv_path = norm_deliv_tuple[0] if norm_deliv_tuple else (deliverable.get('path') if isinstance(deliverable, dict) else None)
                    return HarnessTask(
                        kind='work_order',
                        identifier=work_order_id,
                        path=path,
                        title=str(payload.get('title', work_order_id)),
                        body=str(payload.get('description', '')),
                        query=str(payload.get('title', work_order_id)),
                        work_order_id=work_order_id,
                        deliverable_path=deliv_path,
                        work_order_type=str(payload.get('type')) if isinstance(payload, dict) and payload.get('type') else None,
                        deliverable_type=str(deliverable.get('type')) if isinstance(deliverable, dict) and deliverable.get('type') else None,
                        deliverable_paths=norm_deliv_tuple,
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
            deliv_paths = []
            if isinstance(deliverable, dict) and deliverable.get('path'):
                deliv_paths.append(str(deliverable['path']))
            elif isinstance(deliverable, str) and deliverable.strip():
                deliv_paths.append(deliverable.strip())
            for extra in (payload.get('deliverables') or []) if isinstance(payload, dict) else []:
                if isinstance(extra, dict) and extra.get('path'):
                    deliv_paths.append(str(extra['path']))
                elif isinstance(extra, str) and extra.strip():
                    deliv_paths.append(extra.strip())
            norm_deliv_tuple = tuple(dict.fromkeys(Path(p).as_posix().lstrip('/') for p in deliv_paths if p.strip()))
            deliv_path = norm_deliv_tuple[0] if norm_deliv_tuple else (deliverable.get('path') if isinstance(deliverable, dict) else None)
            return HarnessTask(
                kind='work_order',
                identifier=wo_id,
                path=path,
                title=str(payload.get('title', wo_id)),
                body=str(payload.get('description', '')),
                query=str(payload.get('title', wo_id)),
                work_order_id=wo_id,
                deliverable_path=deliv_path,
                work_order_type=str(payload.get('type')) if isinstance(payload, dict) and payload.get('type') else None,
                deliverable_type=str(deliverable.get('type')) if isinstance(deliverable, dict) and deliverable.get('type') else None,
                deliverable_paths=norm_deliv_tuple,
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
            from cli.init import ensure_canonical_tree
            tree_data = ensure_canonical_tree(self.project_path)
            if self.agent not in tree_data['agents']:
                tree_data['agents'][self.agent] = {
                    'session_count': 0,
                    'status': 'idle',
                    'assigned_work_orders': [],
                }
                from validators.kernel.io import durable_write_yaml
                durable_write_yaml(self.sync_path / 'runtime' / 'TREE.yaml', tree_data)
        self._tree_cache = tree_data

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
        from cli.init import ensure_canonical_tree
        if not tree_file.exists():
            self._tree_cache = ensure_canonical_tree(self.project_path)
            return self._tree_cache
        if self._tree_cache is None:
            self._tree_cache = ensure_canonical_tree(self.project_path)
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
