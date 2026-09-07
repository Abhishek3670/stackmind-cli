"""Two-pass symbol registration and edge resolution."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from cli.lock import acquire_lock, release_lock
from validators.knowledge.registry import SymbolRegistry, birth_key, node_id_for

import os
import shutil

from .ir import COMPILER_VERSION, CompilerIR, DiagnosticIR, EdgeIR, SymbolIR
from .parse import ParsedCall, ParsedFile, ParsedRelation, ParsedSymbol, parse_project
from .pydantic_compiler import augment_parsed_files as augment_pydantic_files
from .fastapi_compiler import augment_parsed_files as augment_fastapi_files
from .sqlalchemy_compiler import augment_parsed_files as augment_sqlalchemy_files
from .django_compiler import augment_parsed_files as augment_django_files
from .celery_compiler import augment_parsed_files as augment_celery_files
from .alembic_compiler import augment_parsed_files as augment_alembic_files
from .doc_compiler import augment_parsed_files as augment_doc_files
from .config_compiler import augment_parsed_files as augment_config_files
from .cicd_compiler import augment_parsed_files as augment_cicd_files
from .test_compiler import augment_parsed_files as augment_test_files
from .cycle_compiler import augment_parsed_files as augment_cycle_files
from .dead_code_compiler import augment_parsed_files as augment_dead_code_files
from .health_compiler import augment_parsed_files as augment_health_files
from .impact_compiler import augment_parsed_files as augment_impact_files

ALL_AUGMENTERS = (
    augment_pydantic_files,
    augment_fastapi_files,
    augment_sqlalchemy_files,
    augment_django_files,
    augment_celery_files,
    augment_alembic_files,
    augment_doc_files,
    augment_config_files,
    augment_cicd_files,
    augment_test_files,
    augment_cycle_files,
    augment_dead_code_files,
    augment_health_files,
    augment_impact_files,
)


def run_all_augmenters(parsed_files: list[ParsedFile], project_path: Path | None = None) -> None:
    """Run all 14 domain compiler augmenters on parsed files."""
    for augment in ALL_AUGMENTERS:
        try:
            augment(parsed_files, project_path=project_path)
        except TypeError:
            augment(parsed_files)


def compile_project(
    project_path: Path,
    *,
    agent: str = "codex",
    write_registry: bool = True,
) -> CompilerIR:
    """Compile a project into deterministic IR.

    The compiler reads source files, registers symbols through the Phase 1
    registry, and resolves call edges in a second pass. Registry writes are the
    only locked portion of the flow.
    """
    project_path = project_path.resolve()
    parsed_files = parse_project(project_path)
    run_all_augmenters(parsed_files, project_path=project_path)

    sync_dir = project_path / ".sync"
    external_project = not (sync_dir / "runtime").exists()

    # External projects: skip locking (no runtime dir) but still write registry
    registry = SymbolRegistry(project_path, agent=agent)

    lock_acquired = False
    if write_registry and not external_project:
        ok, message = acquire_lock(sync_dir, agent, session_id="compiler")
        if not ok:
            raise RuntimeError(message)
        lock_acquired = True

    try:
        symbols = _register_symbols(parsed_files, registry, write_registry=write_registry)
    finally:
        if lock_acquired:
            release_lock(project_path / ".sync", agent)

    edges = _resolve_edges(parsed_files, symbols, project_path)
    diagnostics = _diagnostics(parsed_files)
    return CompilerIR(
        revision_inputs={
            "compiler_version": COMPILER_VERSION,
            "git_commit": _git_value(project_path, ["rev-parse", "HEAD"]),
            "registry_version": _registry_version(project_path),
            "schema_version": "1",
            "sync_ref": _sync_ref(project_path),
        },
        symbols=sorted(symbols, key=lambda s: (s.path, s.qualified_name, s.node_id)),
        edges=edges,
        diagnostics=diagnostics,
    )


def _register_symbols(
    parsed_files: list[ParsedFile],
    registry: SymbolRegistry,
    *,
    write_registry: bool,
) -> list[SymbolIR]:
    owner_lookup: dict[tuple[str, str], str] = {}
    pending: list[tuple[ParsedSymbol, str]] = []
    seen_keys: dict[str, int] = {}  # birth_key → count (for collision disambiguation)

    for parsed in sorted(parsed_files, key=lambda item: item.path):
        for symbol in sorted(parsed.symbols, key=lambda item: (item.path, item.qualified_name, item.location.get("line", 0))):
            base_key = birth_key(symbol.path, symbol.qualified_name)

            # Disambiguate conditional redefinitions (same path:qualname)
            if base_key in seen_keys:
                seen_keys[base_key] += 1
                line = symbol.location.get("line", seen_keys[base_key])
                key = birth_key(symbol.path, f"{symbol.qualified_name}:{line}")
            else:
                seen_keys[base_key] = 1
                key = base_key

            if write_registry:
                written = registry.get_or_create(
                    kind=symbol.kind,
                    path=symbol.path,
                    qualified_name=symbol.qualified_name if key == base_key else f"{symbol.qualified_name}:{symbol.location.get('line', 0)}",
                    owner=None,
                    rev=0,
                )
                node_id = written.node_id
            else:
                node_id = node_id_for(symbol.kind, key)
            owner_lookup[(symbol.path, symbol.qualified_name, symbol.location.get("line", 0))] = node_id
            pending.append((symbol, node_id))

    ir_symbols: list[SymbolIR] = []
    for symbol, node_id in pending:
        owner = None
        if symbol.owner_qualified_name is not None:
            # Find owner by path + qualified_name (any line — owners don't collide)
            owner = next(
                (nid for (p, qn, _ln), nid in owner_lookup.items()
                 if p == symbol.path and qn == symbol.owner_qualified_name),
                None,
            )
        ir_symbols.append(
            SymbolIR(
                node_id=node_id,
                kind=symbol.kind,
                path=symbol.path,
                qualified_name=symbol.qualified_name,
                signature=symbol.signature,
                location=symbol.location,
                content_hash=symbol.content_hash,
                owner=owner,
            )
        )
    return sorted(ir_symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))


def _resolve_edges(
    parsed_files: list[ParsedFile],
    symbols: list[SymbolIR],
    project_path: Path,
) -> list[EdgeIR]:
    by_path_qual = {(symbol.path, symbol.qualified_name): symbol for symbol in symbols}
    by_global_name = _global_symbol_index(symbols)
    module_names = {parsed.module_name for parsed in parsed_files}
    parsed_by_path = {parsed.path: parsed for parsed in parsed_files}

    edges: list[EdgeIR] = []
    for parsed in sorted(parsed_files, key=lambda item: item.path):
        for call in sorted(parsed.calls, key=lambda item: (item.path, item.line, item.target_name)):
            source_symbol = by_path_qual.get((call.path, call.source_qualified_name))
            if source_symbol is None:
                continue
            edge = _resolve_call(
                call,
                parsed_by_path[call.path],
                source_symbol,
                by_path_qual,
                by_global_name,
                module_names,
                project_path,
            )
            edges.append(edge)
        for relation in sorted(
            parsed.relations,
            key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name),
        ):
            source_symbol = by_path_qual.get((relation.path, relation.source_qualified_name))
            if source_symbol is None:
                continue
            edges.append(
                _resolve_relation(
                    relation,
                    source_symbol,
                    by_path_qual,
                    by_global_name,
                )
            )
    return edges


def _resolve_call(
    call: ParsedCall,
    parsed: ParsedFile,
    source_symbol: SymbolIR,
    by_path_qual: dict[tuple[str, str], SymbolIR],
    by_global_name: dict[str, SymbolIR],
    module_names: set[str],
    project_path: Path,
) -> EdgeIR:
    candidates = _candidate_names(call, parsed, source_symbol)
    for candidate in candidates:
        local = by_path_qual.get((call.path, candidate))
        if local is not None:
            return _edge(
                relation='CALLS',
                path=call.path,
                line=call.line,
                confidence=1.0,
                source_symbol=source_symbol,
                target_id=local.node_id,
                target_name=local.qualified_name,
                resolution='RESOLVED',
            )

        global_match = by_global_name.get(candidate)
        if global_match is not None:
            return _edge(
                relation='CALLS',
                path=call.path,
                line=call.line,
                confidence=0.95,
                source_symbol=source_symbol,
                target_id=global_match.node_id,
                target_name=f"{global_match.path}:{global_match.qualified_name}",
                resolution='RESOLVED',
            )

    imported = _import_target(call.target_name, parsed.imports)
    if imported:
        if _is_repo_import(imported, module_names):
            match = by_global_name.get(imported)
            if match is not None:
                return _edge(
                    relation='CALLS',
                    path=call.path,
                    line=call.line,
                    confidence=0.95,
                    source_symbol=source_symbol,
                    target_id=match.node_id,
                    target_name=f"{match.path}:{match.qualified_name}",
                    resolution='RESOLVED',
                )
        return _edge(
            relation='CALLS',
            path=call.path,
            line=call.line,
            confidence=1.0,
            source_symbol=source_symbol,
            target_id=None,
            target_name=imported,
            resolution='EXTERNAL',
        )

    return _edge(
        relation='CALLS',
        path=call.path,
        line=call.line,
        confidence=0.0,
        source_symbol=source_symbol,
        target_id=None,
        target_name=call.target_name,
        resolution='UNRESOLVED',
    )


def _candidate_names(call: ParsedCall, parsed: ParsedFile, source_symbol: SymbolIR) -> list[str]:
    target = call.target_name
    candidates = [target]

    if "." not in target:
        candidates.append(f"{parsed.module_name}.{target}")
    if target.startswith("self.") and source_symbol.qualified_name:
        class_name = source_symbol.qualified_name.rsplit(".", 1)[0]
        candidates.append(f"{class_name}.{target.split('.', 1)[1]}")

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def _resolve_relation(
    relation: ParsedRelation,
    source_symbol: SymbolIR,
    by_path_qual: dict[tuple[str, str], SymbolIR],
    by_global_name: dict[str, SymbolIR],
) -> EdgeIR:
    local = by_path_qual.get((relation.path, relation.target_name))
    if local is not None:
        return _edge(
            relation=relation.relation,
            path=relation.path,
            line=relation.line,
            confidence=relation.confidence,
            source_symbol=source_symbol,
            target_id=local.node_id,
            target_name=local.qualified_name,
            resolution='RESOLVED',
        )

    global_match = by_global_name.get(relation.target_name)
    if global_match is not None:
        return _edge(
            relation=relation.relation,
            path=relation.path,
            line=relation.line,
            confidence=relation.confidence,
            source_symbol=source_symbol,
            target_id=global_match.node_id,
            target_name=f'{global_match.path}:{global_match.qualified_name}',
            resolution='RESOLVED',
        )

    resolution = (
        'EXTERNAL'
        if relation.target_name.startswith(('django.', 'fastapi.', 'pydantic.', 'sqlalchemy.', 'rest_framework.'))
        else 'UNRESOLVED'
    )
    return _edge(
        relation=relation.relation,
        path=relation.path,
        line=relation.line,
        confidence=relation.confidence if resolution == 'RESOLVED' else (1.0 if resolution == 'EXTERNAL' else 0.0),
        source_symbol=source_symbol,
        target_id=None,
        target_name=relation.target_name,
        resolution=resolution,
    )


def _import_target(target_name: str, imports: dict[str, str]) -> str | None:
    root, _, rest = target_name.partition(".")
    imported = imports.get(root)
    if imported is None:
        return None
    return f"{imported}.{rest}" if rest else imported


def _is_repo_import(imported: str, module_names: set[str]) -> bool:
    return any(imported == module or imported.startswith(f"{module}.") for module in module_names)


def _global_symbol_index(symbols: list[SymbolIR]) -> dict[str, SymbolIR]:
    index: dict[str, SymbolIR] = {}
    module_by_path = {
        symbol.path: symbol.qualified_name
        for symbol in symbols
        if symbol.kind.lower() == "module"
    }
    for symbol in symbols:
        module = module_by_path.get(symbol.path, "")
        index.setdefault(symbol.qualified_name, symbol)
        if module:
            index.setdefault(f"{module}.{symbol.qualified_name}", symbol)
    return index


def _edge(
    *,
    relation: str,
    path: str,
    line: int,
    confidence: float,
    source_symbol: SymbolIR,
    target_id: str | None,
    target_name: str,
    resolution: str,
) -> EdgeIR:
    return EdgeIR(
        source_id=source_symbol.node_id,
        relation=relation,
        target_id=target_id,
        target_name=target_name,
        resolution=resolution,  # type: ignore[arg-type]
        confidence=confidence,
        path=path,
        line=line,
    )


def _diagnostics(parsed_files: list[ParsedFile]) -> list[DiagnosticIR]:
    diagnostics: list[DiagnosticIR] = []
    for parsed in parsed_files:
        for diagnostic in parsed.diagnostics:
            diagnostics.append(
                DiagnosticIR(
                    path=diagnostic.path,
                    severity="ERROR",
                    code=diagnostic.code,
                    message=diagnostic.message,
                    line=diagnostic.line,
                )
            )
    return diagnostics


def _registry_version(project_path: Path) -> str | None:
    registry_path = project_path / ".sync" / "knowledge" / "registry"
    if not registry_path.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(registry_path.glob("*/*.json")):
        rel = path.relative_to(registry_path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def _sync_ref(project_path: Path) -> str | None:
    ref = project_path / ".sync-ref"
    if not ref.exists():
        return None
    value = ref.read_text(encoding="utf-8").strip()
    return value or None


def _git_value(project_path: Path, args: list[str]) -> str | None:
    if not (project_path / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(project_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None

