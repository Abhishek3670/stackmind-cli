"""Refactoring impact mapper and change-set predictor frontend augmentation for the knowledge compiler.

Traces call dependencies and import relationships down to affected API endpoints,
Celery tasks, database models, and test cases.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with impact prediction symbols and relations."""
    # Build reverse call graph (target -> list of callers)
    reverse_calls: dict[str, set[str]] = {}
    symbol_to_parsed: dict[str, ParsedFile] = {}

    for parsed in parsed_files:
        for sym in parsed.symbols:
            symbol_to_parsed[sym.qualified_name] = parsed
            reverse_calls.setdefault(sym.qualified_name, set())

    for parsed in parsed_files:
        for call in parsed.calls:
            src = call.source_qualified_name
            tgt = call.target_name
            reverse_calls.setdefault(tgt, set()).add(src)
        for rel in parsed.relations:
            src = rel.source_qualified_name
            tgt = rel.target_name
            reverse_calls.setdefault(tgt, set()).add(src)

    # For each class / function symbol, compute downstream affected nodes
    for parsed in parsed_files:
        for sym in list(parsed.symbols):
            if sym.kind in {"Function", "Method", "Class", "SQLAlchemyModel", "PydanticModel"}:
                affected = _trace_downstream(sym.qualified_name, reverse_calls)
                if not affected:
                    continue

                endpoints = [a for a in affected if "route" in a.lower() or "endpoint" in a.lower()]
                tests = [a for a in affected if "test" in a.lower()]
                impact_qualname = f"impact:{sym.qualified_name}"
                signature = f"impact affected_total={len(affected)} endpoints={len(endpoints)} tests={len(tests)}"

                parsed.symbols.append(
                    ParsedSymbol(
                        kind="ImpactPrediction",
                        path=parsed.path,
                        qualified_name=impact_qualname,
                        module_name=parsed.module_name,
                        signature=signature,
                        location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                        content_hash=hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12],
                        owner_qualified_name=sym.qualified_name,
                    )
                )

                for aff_target in affected:
                    parsed.relations.append(
                        ParsedRelation(
                            path=parsed.path,
                            source_qualified_name=impact_qualname,
                            relation="REFACTORING_AFFECTS",
                            target_name=aff_target,
                            line=1,
                            confidence=0.9,
                        )
                    )


def _trace_downstream(start_node: str, reverse_calls: dict[str, set[str]]) -> set[str]:
    visited: set[str] = set()
    queue = [start_node]
    while queue:
        curr = queue.pop()
        for caller in reverse_calls.get(curr, set()):
            if caller not in visited and caller != start_node:
                visited.add(caller)
                queue.append(caller)
    return visited
