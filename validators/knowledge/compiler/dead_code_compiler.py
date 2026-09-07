"""Dead code and unused module inspector frontend augmentation for the knowledge compiler.

Statically analyzes the call graph and import references to detect unreachable symbols,
unused local declarations, and unreferenced modules.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with dead code and unused module symbols and relations."""
    all_symbols: dict[str, ParsedSymbol] = {}
    symbol_to_parsed: dict[str, ParsedFile] = {}
    roots: set[str] = set()
    call_graph: dict[str, set[str]] = {}

    for parsed in parsed_files:
        for sym in parsed.symbols:
            all_symbols[sym.qualified_name] = sym
            symbol_to_parsed[sym.qualified_name] = parsed
            call_graph.setdefault(sym.qualified_name, set())

            # Identify entry points / roots
            if _is_root_symbol(sym, parsed):
                roots.add(sym.qualified_name)

    # Populate call edges
    for parsed in parsed_files:
        for call in parsed.calls:
            src = call.source_qualified_name
            tgt = call.target_name
            if src in call_graph:
                call_graph[src].add(tgt)
        for rel in parsed.relations:
            src = rel.source_qualified_name
            tgt = rel.target_name
            if src in call_graph:
                call_graph[src].add(tgt)

    # Precompute short name to qualified names mapping to avoid O(N) scanning
    short_name_to_qualnames: dict[str, list[str]] = {}
    for sym_qual in all_symbols:
        short_name = sym_qual.split(".")[-1]
        short_name_to_qualnames.setdefault(short_name, []).append(sym_qual)

    # Traverse graph from roots to find reachable symbols
    visited: set[str] = set()
    queue = list(roots)
    queued_set = set(roots)
    while queue:
        curr = queue.pop()
        visited.add(curr)

        # Check call graph targets
        for neighbor in call_graph.get(curr, set()):
            if neighbor in all_symbols and neighbor not in visited and neighbor not in queued_set:
                queue.append(neighbor)
                queued_set.add(neighbor)
            # Also check partial/short name matches using precomputed map
            short_name = neighbor.split(".")[-1]
            for sym_qual in short_name_to_qualnames.get(short_name, []):
                if sym_qual not in visited and sym_qual not in queued_set:
                    queue.append(sym_qual)
                    queued_set.add(sym_qual)

    # Identify unreached symbols (excluding modules and roots)
    for qualname, sym in all_symbols.items():
        if sym.kind in {"Module", "DocFile", "DocSection", "ConfigFile", "Pipeline", "PipelineJob", "PipelineStep", "ImportCycle"}:
            continue
        if qualname not in visited and not _is_special_method(sym.qualified_name):
            parsed = symbol_to_parsed[qualname]
            dead_qualname = f"dead:{qualname}"

            parsed.symbols.append(
                ParsedSymbol(
                    kind="DeadCode",
                    path=parsed.path,
                    qualified_name=dead_qualname,
                    module_name=parsed.module_name,
                    signature=f"dead {qualname}",
                    location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                    content_hash=hashlib.sha256(qualname.encode("utf-8")).hexdigest()[:12],
                    owner_qualified_name=sym.qualified_name,
                )
            )
            parsed.relations.append(
                ParsedRelation(
                    path=parsed.path,
                    source_qualified_name=dead_qualname,
                    relation="IS_DEAD_CODE",
                    target_name=qualname,
                    line=1,
                )
            )


def _is_root_symbol(sym: ParsedSymbol, parsed: ParsedFile) -> bool:
    qual = sym.qualified_name.lower()
    kind = sym.kind.lower()

    # CLI, FastAPI, Celery, Tests, Main, Init exports
    if "cli" in qual or "endpoint" in qual or "route" in qual or "task" in qual:
        return True
    if "test" in qual or "test" in parsed.path.lower():
        return True
    if qual.endswith(".main") or qual == "main" or qual.endswith(".cli"):
        return True
    if kind in {"fastapiroute", "celerytask", "testcase", "testsuite", "testfixture", "alembicmigration"}:
        return True
    return False


def _is_special_method(name: str) -> bool:
    short_name = name.split(".")[-1]
    return short_name.startswith("__") and short_name.endswith("__")
