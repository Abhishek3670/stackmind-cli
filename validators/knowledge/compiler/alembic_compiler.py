"""Alembic-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .parse import ParsedFile, ParsedRelation, ParsedSymbol


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with Alembic migration symbols and dependencies."""
    symbols = []
    relations = []

    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        if not _is_alembic_migration(parsed):
            continue

        mig_symbol, mig_relations = _compile_alembic_migration(parsed)
        symbols.append(mig_symbol)
        relations.extend(mig_relations)

    for parsed in sorted(parsed_files, key=lambda item: item.path):
        module_symbols = [sym for sym in symbols if sym.path == parsed.path]
        module_relations = [rel for rel in relations if rel.path == parsed.path]
        parsed.symbols.extend(sorted(module_symbols, key=lambda item: (item.path, item.qualified_name, item.kind)))
        parsed.relations.extend(
            sorted(module_relations, key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name))
        )


def _is_alembic_migration(parsed: ParsedFile) -> bool:
    if parsed.tree is None:
        return False
    has_revision = False
    has_down_revision = False
    for statement in parsed.tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    if target.id == 'revision':
                        has_revision = True
                    elif target.id == 'down_revision':
                        has_down_revision = True
    return has_revision and has_down_revision


def _compile_alembic_migration(parsed: ParsedFile) -> tuple[ParsedSymbol, list[ParsedRelation]]:
    revision = None
    down_revision = None
    upgrade_node = None

    for statement in parsed.tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    if target.id == 'revision':
                        if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                            revision = statement.value.value
                    elif target.id == 'down_revision':
                        if isinstance(statement.value, ast.Constant):
                            down_revision = statement.value.value
                        elif isinstance(statement.value, (ast.List, ast.Tuple)):
                            down_revision = [elt.value for elt in statement.value.elts if isinstance(elt, ast.Constant)]
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == 'upgrade':
            upgrade_node = statement

    if revision is None:
        raise ValueError("Alembic migration must define revision")

    ops = []
    if upgrade_node is not None:
        ops = _collect_alembic_operations(upgrade_node)

    if down_revision is None:
        down_rev_str = '-'
    elif isinstance(down_revision, list):
        down_rev_str = ','.join(down_revision)
    else:
        down_rev_str = str(down_revision)

    ops_str = json.dumps(ops)
    signature = f"migration {revision}; down_revision={down_rev_str}; operations={ops_str}"

    symbol = ParsedSymbol(
        kind='AlembicMigration',
        path=parsed.path,
        qualified_name=f'alembic.migration.{revision}',
        module_name=parsed.module_name,
        signature=signature,
        location={'line': 1, 'column': 0, 'end_line': 1, 'end_column': 0},
        content_hash=_node_hash(parsed.tree),
        owner_qualified_name=parsed.module_name,
    )

    relations = []
    parents = [down_revision] if isinstance(down_revision, str) else (down_revision or [])
    for parent in parents:
        relations.append(
            ParsedRelation(
                path=parsed.path,
                source_qualified_name=symbol.qualified_name,
                relation='DEPENDS_ON_MIGRATION',
                target_name=f'alembic.migration.{parent}',
                line=1,
            )
        )
    return symbol, relations


def _collect_alembic_operations(upgrade_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    ops = []
    for statement in ast.walk(upgrade_node):
        if not isinstance(statement, ast.Call):
            continue
        call_name = _expr_name(statement.func)
        if not call_name or not call_name.startswith('op.'):
            continue
        op_type = call_name.split('.', 1)[1]
        
        if op_type == 'create_table':
            table_name = _literal_arg(statement, 0)
            if table_name:
                columns = []
                for arg in statement.args[1:]:
                    col_name = _column_name_from_arg(arg)
                    if col_name:
                        columns.append(col_name)
                for kw in statement.keywords:
                    # sa.Column could also be passed via keywords or list comprehensions, but standard is args
                    pass
                ops.append({'op': 'create_table', 'table': table_name, 'columns': columns})
        elif op_type == 'drop_table':
            table_name = _literal_arg(statement, 0)
            if table_name:
                ops.append({'op': 'drop_table', 'table': table_name})
        elif op_type == 'add_column':
            table_name = _literal_arg(statement, 0)
            col_name = None
            if len(statement.args) > 1:
                col_name = _column_name_from_arg(statement.args[1])
            if table_name and col_name:
                ops.append({'op': 'add_column', 'table': table_name, 'column': col_name})
        elif op_type == 'drop_column':
            table_name = _literal_arg(statement, 0)
            column_name = _literal_arg(statement, 1)
            if table_name and column_name:
                ops.append({'op': 'drop_column', 'table': table_name, 'column': column_name})
        elif op_type == 'alter_column':
            table_name = _literal_arg(statement, 0)
            column_name = _literal_arg(statement, 1)
            if table_name and column_name:
                ops.append({'op': 'alter_column', 'table': table_name, 'column': column_name})
    return ops


def _column_name_from_arg(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func_name = _expr_name(node.func)
    if func_name and func_name.rsplit('.', 1)[-1] == 'Column':
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return node.args[0].value
    return None


def _expr_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_name(node.value)
        return f'{parent}.{node.attr}' if parent else node.attr
    if isinstance(node, ast.Subscript):
        return _expr_name(node.value)
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return None


def _literal_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) <= index:
        return None
    value = call.args[index]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _location(node: ast.AST) -> dict[str, int]:
    return {
        'column': int(getattr(node, 'col_offset', 0)),
        'end_column': int(getattr(node, 'end_col_offset', 0) or 0),
        'end_line': int(getattr(node, 'end_lineno', getattr(node, 'lineno', 1)) or 1),
        'line': int(getattr(node, 'lineno', 1) or 1),
    }


def _node_hash(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode('utf-8')).hexdigest()


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node, include_attributes=False)
