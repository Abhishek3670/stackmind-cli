"""Structural complexity and health analyzer frontend augmentation for the knowledge compiler.

Calculates cognitive complexity, lines of code (LOC), fan-in/fan-out coupling ratios,
and structural health parameters per module.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(
    parsed_files: list[ParsedFile],
    project_path: Path | None = None,
) -> None:
    """Augment parsed files with health metrics symbols and relations."""
    # Calculate fan-in and fan-out for each module
    fan_in: dict[str, int] = {p.path: 0 for p in parsed_files}
    fan_out: dict[str, int] = {p.path: 0 for p in parsed_files}

    path_by_module = {p.module_name: p.path for p in parsed_files}

    for parsed in parsed_files:
        imported_modules = set()
        for imp_name in parsed.imports.values():
            root = imp_name.split(".")[0]
            if root in path_by_module and path_by_module[root] != parsed.path:
                imported_modules.add(path_by_module[root])

        fan_out[parsed.path] = len(imported_modules)
        for imported_path in imported_modules:
            fan_in[imported_path] = fan_in.get(imported_path, 0) + 1

    for parsed in parsed_files:
        loc = len(parsed.symbols)  # default approximation if tree is None
        complexity = 1

        if parsed.tree is not None:
            loc = getattr(parsed.tree, "end_lineno", None) or 1
            complexity = _calculate_cognitive_complexity(parsed.tree)

        in_count = fan_in.get(parsed.path, 0)
        out_count = fan_out.get(parsed.path, 0)
        health_qualname = f"health:{parsed.path}"
        signature = f"health complexity={complexity} loc={loc} fan_in={in_count} fan_out={out_count}"

        parsed.symbols.append(
            ParsedSymbol(
                kind="HealthMetrics",
                path=parsed.path,
                qualified_name=health_qualname,
                module_name=parsed.module_name,
                signature=signature,
                location={"line": 1, "column": 0, "end_line": 1, "end_column": 0},
                content_hash=hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12],
                owner_qualified_name=parsed.module_name,
            )
        )
        parsed.relations.append(
            ParsedRelation(
                path=parsed.path,
                source_qualified_name=parsed.module_name,
                relation="HAS_HEALTH_METRICS",
                target_name=health_qualname,
                line=1,
            )
        )


def _calculate_cognitive_complexity(tree: ast.AST) -> int:
    complexity = 1
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.Try)):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += len(node.values) - 1
    return complexity
