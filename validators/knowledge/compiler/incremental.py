"""Incremental compiler for selective knowledge rebuilds."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cli.lock import acquire_lock, release_lock
from validators.knowledge.compiler.ir import CompilerIR, DiagnosticIR, SymbolIR
from validators.knowledge.compiler.parse import ParsedFile, ParsedSymbol, parse_project
from validators.knowledge.compiler.resolve import run_all_augmenters
from validators.knowledge.projections import build_projections
from validators.knowledge.projections.reverse_index import lookup_reverse_edges
from validators.knowledge.registry import SymbolRegistry, birth_key, node_id_for
from validators.knowledge.storage import (
    build_node_documents,
    build_revision_document,
    canonical_json,
    latest_revision_id,
    node_path,
    read_ir,
    revision_path,
)
from validators.knowledge.writer import KnowledgeWriteResult, write_knowledge

from .rename import AppearedSymbol, HistoricalSymbol, detect_renames
from .resolve import COMPILER_VERSION, _git_value, _registry_version, _resolve_edges, _sync_ref


@dataclass(frozen=True)
class IncrementalUpdateResult:
    """Outcome of an incremental knowledge update."""

    changed: bool
    revision_id: int | None
    revision_path: Path | None
    written_paths: tuple[Path, ...]
    unchanged_paths: tuple[Path, ...]
    deleted_paths: tuple[Path, ...]
    dirty_paths: tuple[str, ...]
    affected_paths: tuple[str, ...]


@dataclass(frozen=True)
class _RegistryCurrent:
    node_id: str
    current_path: str
    current_qualified_name: str


def incremental_update(
    project_path: Path,
    *,
    agent: str = 'codex',
    changed_paths: tuple[str, ...] | None = None,
    deleted_paths: tuple[str, ...] | None = None,
    built_at: str = '',
) -> IncrementalUpdateResult:
    """Incrementally update T1/T2 knowledge artifacts."""
    project_path = project_path.resolve()
    if latest_revision_id(project_path) == 0:
        full_result = write_knowledge(
            project_path,
            _full_compile(project_path, agent),
            agent=agent,
            built_at=built_at,
        )
        build_projections(project_path)
        return IncrementalUpdateResult(
            changed=True,
            revision_id=full_result.revision_id,
            revision_path=full_result.revision_path,
            written_paths=full_result.written_paths,
            unchanged_paths=full_result.unchanged_paths,
            deleted_paths=(),
            dirty_paths=tuple(sorted(_current_python_paths(project_path))),
            affected_paths=tuple(sorted(_current_python_paths(project_path))),
        )

    old_ir = read_ir(project_path)
    parsed_files = parse_project(project_path)
    run_all_augmenters(parsed_files, project_path=project_path)
    parsed_by_path = {item.path: item for item in parsed_files}
    current_paths = set(parsed_by_path)
    old_symbols = old_ir.symbols

    dirty_current_paths, dirty_deleted_paths = _dirty_paths(
        old_ir,
        parsed_files,
        changed_paths=changed_paths,
        deleted_paths=deleted_paths,
    )
    if not dirty_current_paths and not dirty_deleted_paths:
        return IncrementalUpdateResult(
            changed=False,
            revision_id=None,
            revision_path=None,
            written_paths=(),
            unchanged_paths=(),
            deleted_paths=(),
            dirty_paths=(),
            affected_paths=(),
        )

    registry = SymbolRegistry(project_path, agent=agent)
    active_records = [record for record in registry.load_all() if record.get('status') == 'active']
    key_to_record = _registry_key_index(active_records)
    old_symbols_by_id = {symbol.node_id: symbol for symbol in old_symbols}
    old_owner_names = {
        symbol.node_id: old_symbols_by_id[symbol.owner].qualified_name
        for symbol in old_symbols
        if symbol.owner in old_symbols_by_id
    }
    current_birth_keys = {
        birth_key(symbol.path, symbol.qualified_name): symbol
        for parsed in parsed_files
        for symbol in parsed.symbols
    }
    touched_old_paths = set(dirty_current_paths) | set(dirty_deleted_paths)
    vanished = [
        HistoricalSymbol(
            node_id=symbol.node_id,
            kind=symbol.kind,
            path=symbol.path,
            qualified_name=symbol.qualified_name,
            signature=symbol.signature,
            content_hash=symbol.content_hash,
            owner_qualified_name=old_owner_names.get(symbol.node_id),
        )
        for symbol in old_symbols
        if symbol.path in touched_old_paths
        and birth_key(symbol.path, symbol.qualified_name) not in current_birth_keys
    ]
    appeared = [
        AppearedSymbol.from_parsed(symbol)
        for parsed in parsed_files
        if parsed.path in dirty_current_paths
        for symbol in parsed.symbols
        if birth_key(symbol.path, symbol.qualified_name) not in key_to_record
    ]
    rename_matches, unmatched_appeared = detect_renames(vanished, appeared)
    alias_by_new_key = {
        (match.new_path, match.new_qualified_name): match.node_id
        for match in rename_matches
    }

    assigned_by_key: dict[tuple[str, str], str] = {}
    create_targets: list[ParsedSymbol] = []
    alias_targets: dict[str, tuple[str, str]] = {}

    for parsed in parsed_files:
        for symbol in parsed.symbols:
            key = birth_key(symbol.path, symbol.qualified_name)
            existing = key_to_record.get(key)
            new_key = (symbol.path, symbol.qualified_name)
            if existing is not None:
                assigned_by_key[new_key] = existing['node_id']
                current = existing.get('current', {})
                if (
                    current.get('path') != symbol.path
                    or current.get('qualified_name') != symbol.qualified_name
                ):
                    alias_targets[existing['node_id']] = (symbol.path, symbol.qualified_name)
                continue
            aliased_node_id = alias_by_new_key.get(new_key)
            if aliased_node_id is not None:
                assigned_by_key[new_key] = aliased_node_id
                alias_targets[aliased_node_id] = (symbol.path, symbol.qualified_name)
                continue
            assigned_by_key[new_key] = node_id_for(symbol.kind, key)
            create_targets.append(symbol)

    owner_lookup = dict(assigned_by_key)
    new_symbols = _build_symbols(parsed_files, owner_lookup)
    new_ir = CompilerIR(
        revision_inputs={
            'compiler_version': COMPILER_VERSION,
            'git_commit': _git_value(project_path, ['rev-parse', 'HEAD']),
            'registry_version': _registry_version(project_path),
            'schema_version': '1',
            'sync_ref': _sync_ref(project_path),
        },
        symbols=new_symbols,
        edges=_resolve_edges(parsed_files, new_symbols, project_path),
        diagnostics=_diagnostics(parsed_files),
    )

    impacted_node_ids = {match.node_id for match in rename_matches}
    reused_node_ids = {symbol.node_id for symbol in new_ir.symbols}
    obsolete_old_symbols = [
        symbol
        for symbol in old_symbols
        if symbol.path in touched_old_paths and symbol.node_id not in reused_node_ids
    ]
    impacted_node_ids.update(symbol.node_id for symbol in obsolete_old_symbols)

    affected_paths = set(dirty_current_paths)
    for node_id in sorted(impacted_node_ids):
        for edge in lookup_reverse_edges(project_path, node_id, relation='CALLS'):
            source_symbol = old_symbols_by_id.get(edge.get('source_id', ''))
            if source_symbol is not None and source_symbol.path in current_paths:
                affected_paths.add(source_symbol.path)

    documents = build_node_documents(new_ir)
    old_documents = build_node_documents(old_ir)
    live_node_ids = {
        symbol.node_id
        for symbol in new_ir.symbols
        if symbol.path in affected_paths or symbol.node_id in impacted_node_ids
    }
    write_targets = []
    unchanged_paths_list = []
    for node_id, document in sorted(documents.items()):
        path = node_path(project_path, document['kind'], node_id)
        if node_id in live_node_ids or old_documents.get(node_id) != document or not path.exists():
            write_targets.append((path, document))
        else:
            unchanged_paths_list.append(path)

    deleted_node_paths = [
        node_path(project_path, symbol.kind, symbol.node_id)
        for symbol in obsolete_old_symbols
    ]
    if not write_targets and not deleted_node_paths and not alias_targets and not create_targets:
        return IncrementalUpdateResult(
            changed=False,
            revision_id=None,
            revision_path=None,
            written_paths=(),
            unchanged_paths=tuple(sorted(unchanged_paths_list)),
            deleted_paths=(),
            dirty_paths=tuple(sorted(dirty_current_paths | dirty_deleted_paths)),
            affected_paths=tuple(sorted(affected_paths)),
        )

    write_result = _apply_incremental_batch(
        project_path,
        agent=agent,
        ir=new_ir,
        alias_targets=alias_targets,
        create_targets=create_targets,
        obsolete_old_symbols=obsolete_old_symbols,
        write_targets=write_targets,
        deleted_node_paths=deleted_node_paths,
        unchanged_paths=tuple(sorted(unchanged_paths_list)),
        built_at=built_at,
    )
    build_projections(project_path)
    return IncrementalUpdateResult(
        changed=True,
        revision_id=write_result.revision_id,
        revision_path=write_result.revision_path,
        written_paths=write_result.written_paths,
        unchanged_paths=write_result.unchanged_paths,
        deleted_paths=tuple(sorted(path for path in deleted_node_paths if not path.exists())),
        dirty_paths=tuple(sorted(dirty_current_paths | dirty_deleted_paths)),
        affected_paths=tuple(sorted(affected_paths)),
    )


def collect_git_python_changes(project_path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Collect changed and deleted Python paths from git status."""
    project_path = project_path.resolve()
    if not (project_path / '.git').exists():
        return (), ()
    try:
        completed = subprocess.run(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=str(project_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        return (), ()

    changed: set[str] = set()
    deleted: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        status = raw_line[:2]
        payload = raw_line[3:].strip()
        if ' -> ' in payload:
            old_path, new_path = payload.split(' -> ', 1)
            if old_path.endswith('.py'):
                deleted.add(_normalize_rel_path(old_path))
            if new_path.endswith('.py'):
                changed.add(_normalize_rel_path(new_path))
            continue
        if not payload.endswith('.py'):
            continue
        normalized = _normalize_rel_path(payload)
        if 'D' in status:
            deleted.add(normalized)
        else:
            changed.add(normalized)
    return tuple(sorted(changed)), tuple(sorted(deleted))


def _full_compile(project_path: Path, agent: str) -> CompilerIR:
    from validators.knowledge.compiler import compile_project

    return compile_project(project_path, agent=agent)


def _dirty_paths(
    old_ir: CompilerIR,
    parsed_files: list[ParsedFile],
    *,
    changed_paths: tuple[str, ...] | None,
    deleted_paths: tuple[str, ...] | None,
) -> tuple[set[str], set[str]]:
    old_module_hashes = {
        symbol.path: symbol.content_hash
        for symbol in old_ir.symbols
        if symbol.kind.lower() in ('module', 'docfile', 'configfile', 'pipeline', 'adr', 'rfc')
    }
    current_hashes = {parsed.path: _module_hash(parsed) for parsed in parsed_files}
    current_paths = set(current_hashes)
    old_paths = set(old_module_hashes)

    hinted_changed = {_normalize_rel_path(path) for path in changed_paths or ()}
    hinted_deleted = {_normalize_rel_path(path) for path in deleted_paths or ()}
    auto_changed = {
        path
        for path, digest in current_hashes.items()
        if path not in old_module_hashes or old_module_hashes[path] != digest
    }
    auto_deleted = old_paths - current_paths

    if hinted_changed or hinted_deleted:
        changed = {path for path in auto_changed if path in hinted_changed or path not in old_paths}
        deleted = {path for path in auto_deleted if path in hinted_deleted}
        return changed, deleted
    return auto_changed, auto_deleted


def _registry_key_index(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for record in records:
        keys = [record.get('birth_key'), *record.get('aliases', [])]
        for key in keys:
            if isinstance(key, str):
                index[key] = record
    return index


def _build_symbols(
    parsed_files: list[ParsedFile],
    owner_lookup: dict[tuple[str, str], str],
) -> list[SymbolIR]:
    symbols: list[SymbolIR] = []
    for parsed in sorted(parsed_files, key=lambda item: item.path):
        for symbol in sorted(parsed.symbols, key=lambda item: (item.path, item.qualified_name)):
            owner = None
            if symbol.owner_qualified_name is not None:
                owner = owner_lookup.get((symbol.path, symbol.owner_qualified_name))
            symbols.append(
                SymbolIR(
                    node_id=owner_lookup[(symbol.path, symbol.qualified_name)],
                    kind=symbol.kind,
                    path=symbol.path,
                    qualified_name=symbol.qualified_name,
                    signature=symbol.signature,
                    location=symbol.location,
                    content_hash=symbol.content_hash,
                    owner=owner,
                )
            )
    return sorted(symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))


def _module_hash(parsed: ParsedFile) -> str | None:
    module = next(
        (
            symbol
            for symbol in parsed.symbols
            if symbol.kind.lower() in ('module', 'docfile', 'configfile', 'pipeline', 'adr', 'rfc')
        ),
        None,
    )
    return None if module is None else module.content_hash


def _diagnostics(parsed_files: list[ParsedFile]) -> list[DiagnosticIR]:
    diagnostics: list[DiagnosticIR] = []
    for parsed in parsed_files:
        for diagnostic in parsed.diagnostics:
            diagnostics.append(
                DiagnosticIR(
                    path=diagnostic.path,
                    severity='ERROR',
                    code=diagnostic.code,
                    message=diagnostic.message,
                    line=diagnostic.line,
                )
            )
    return diagnostics


def _apply_incremental_batch(
    project_path: Path,
    *,
    agent: str,
    ir: CompilerIR,
    alias_targets: dict[str, tuple[str, str]],
    create_targets: list[ParsedSymbol],
    obsolete_old_symbols: list[SymbolIR],
    write_targets: list[tuple[Path, dict]],
    deleted_node_paths: list[Path],
    unchanged_paths: tuple[Path, ...],
    built_at: str,
) -> KnowledgeWriteResult:
    sync_path = project_path / '.sync'
    external = not (sync_path / 'runtime').exists()

    lock_acquired = False
    if not external:
        ok, message = acquire_lock(sync_path, agent, session_id='incremental')
        if not ok:
            raise RuntimeError(message)
        lock_acquired = True

    registry = SymbolRegistry(project_path, agent=agent)
    try:
        for node_id, target in sorted(alias_targets.items()):
            registry.alias(
                node_id,
                path=target[0],
                qualified_name=target[1],
                rev=latest_revision_id(project_path) + 1,
            )
        for symbol in sorted(create_targets, key=lambda item: (item.path, item.qualified_name)):
            registry.get_or_create(
                kind=symbol.kind,
                path=symbol.path,
                qualified_name=symbol.qualified_name,
                owner=None,
                rev=latest_revision_id(project_path) + 1,
            )
        for symbol in sorted(obsolete_old_symbols, key=lambda item: item.node_id):
            registry.mark_obsolete(symbol.node_id, rev=latest_revision_id(project_path) + 1)

        written: list[Path] = []
        for path, document in write_targets:
            if _write_if_changed(path, document):
                written.append(path)
        for path in sorted(deleted_node_paths):
            if path.exists():
                path.unlink()
                written.append(path)

        parent = latest_revision_id(project_path)
        revision_id = parent + 1
        revision_ir = CompilerIR(
            revision_inputs={
                **ir.revision_inputs,
                'registry_version': _registry_version(project_path),
            },
            symbols=ir.symbols,
            edges=ir.edges,
            diagnostics=ir.diagnostics,
        )
        revision_file = revision_path(project_path, revision_id)
        _write_if_changed(
            revision_file,
            build_revision_document(revision_ir, revision_id, parent or None, built_at),
        )
        written.append(revision_file)
        return KnowledgeWriteResult(
            revision_id=revision_id,
            revision_path=revision_file,
            written_paths=tuple(written),
            unchanged_paths=unchanged_paths,
        )
    finally:
        if lock_acquired:
            release_lock(sync_path, agent)


def _write_if_changed(path: Path, document: dict) -> bool:
    payload = canonical_json(document)
    if path.exists() and path.read_text(encoding='utf-8').replace('\r\n', '\n') == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        temporary.write_text(payload, encoding='utf-8', newline='\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _current_python_paths(project_path: Path) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(project_path).as_posix() for path in project_path.rglob('*.py'))
    )


def _normalize_rel_path(path: str) -> str:
    return path.replace('\\', '/').strip('/')


__all__ = [
    'IncrementalUpdateResult',
    'collect_git_python_changes',
    'incremental_update',
]
