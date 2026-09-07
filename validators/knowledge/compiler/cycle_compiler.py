"""Circular import and cycle detection frontend augmentation for the knowledge compiler.

Statically parses module imports across the codebase, constructs an import DAG,
and identifies strongly connected components (import cycles) in the graph.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with circular import cycle symbols and relations."""
    module_to_parsed: dict[str, ParsedFile] = {p.module_name: p for p in parsed_files}
    import_graph: dict[str, set[str]] = {p.module_name: set() for p in parsed_files}

    for parsed in parsed_files:
        for imported_local, imported_module in parsed.imports.items():
            # Resolve root module name
            root_mod = imported_module.split(".")[0]
            if root_mod in module_to_parsed and root_mod != parsed.module_name:
                import_graph[parsed.module_name].add(root_mod)

    # Detect cycles using Tarjan's SCC algorithm
    cycles = _find_cycles(import_graph)
    if not cycles:
        return

    for idx, cycle in enumerate(cycles, start=1):
        cycle_str = " -> ".join(cycle) + " -> " + cycle[0]
        cycle_hash = hashlib.sha256(cycle_str.encode("utf-8")).hexdigest()[:12]
        cycle_qualname = f"cycle:{cycle_hash}"

        for mod_name in cycle:
            if mod_name in module_to_parsed:
                parsed = module_to_parsed[mod_name]

                # Add cycle symbol if not already added
                if not any(s.qualified_name == cycle_qualname for s in parsed.symbols):
                    parsed.symbols.append(
                        ParsedSymbol(
                            kind="ImportCycle",
                            path=parsed.path,
                            qualified_name=cycle_qualname,
                            module_name=mod_name,
                            signature=f"cycle {cycle_str}",
                            location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                            content_hash=cycle_hash,
                            owner_qualified_name=mod_name,
                        )
                    )

                # Add relations for circular import
                next_mod = cycle[(cycle.index(mod_name) + 1) % len(cycle)]
                parsed.relations.append(
                    ParsedRelation(
                        path=parsed.path,
                        source_qualified_name=mod_name,
                        relation="PART_OF_CYCLE",
                        target_name=cycle_qualname,
                        line=1,
                    )
                )
                parsed.relations.append(
                    ParsedRelation(
                        path=parsed.path,
                        source_qualified_name=mod_name,
                        relation="CIRCULAR_DEPENDENCY",
                        target_name=next_mod,
                        line=1,
                        confidence=1.0,
                    )
                )


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack[node] = True

        for neighbor in sorted(graph.get(node, set())):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif on_stack[neighbor]:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == node:
                    break
            if len(scc) > 1:
                sccs.append(sorted(scc))

    for node in sorted(graph.keys()):
        if node not in indices:
            strongconnect(node)

    return sccs
