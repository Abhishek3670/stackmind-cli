"""SQLAlchemy-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from .parse import ParsedFile, ParsedRelation, ParsedSymbol

DECLARATIVE_BASE_TARGETS = {
    'sqlalchemy.ext.declarative.declarative_base',
    'sqlalchemy.orm.declarative_base',
    'sqlalchemy.orm.DeclarativeBase',
    'DeclarativeBase',
}
MODEL_BASE_NAMES = {'Base', 'DeclarativeBase'}
COLUMN_CALLS = {'Column', 'sqlalchemy.Column', 'mapped_column', 'sqlalchemy.orm.mapped_column'}
TABLE_CALLS = {'Table', 'sqlalchemy.Table'}
RELATIONSHIP_CALLS = {'relationship', 'sqlalchemy.orm.relationship'}
SESSION_CALLS = {'Session', 'sqlalchemy.orm.Session', 'sessionmaker', 'sqlalchemy.orm.sessionmaker'}


@dataclass(frozen=True)
class _ClassInfo:
    path: str
    module_name: str
    qualified_name: str
    imports: dict[str, str]
    symbol: ParsedSymbol
    node: ast.ClassDef


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with SQLAlchemy models, schema objects, and sessions."""
    class_index = _collect_classes(parsed_files)
    base_names = _declarative_base_names(parsed_files)
    model_keys = _detect_model_keys(class_index, base_names)
    model_names = {
        name
        for _, qualified_name in model_keys
        for name in (qualified_name, qualified_name.rsplit('.', 1)[-1])
    }

    for parsed in sorted(parsed_files, key=lambda item: item.path):
        if parsed.tree is None:
            continue
        symbols, relations = _compile_module(parsed, model_names)
        for key in sorted(model_keys, key=lambda item: item[1]):
            if key[0] != parsed.path:
                continue
            model_symbols, model_relations = _compile_model(class_index[key], class_index, model_keys)
            symbols.extend(model_symbols)
            relations.extend(model_relations)
        parsed.symbols.extend(sorted(symbols, key=lambda item: (item.path, item.qualified_name, item.kind)))
        parsed.relations.extend(
            sorted(relations, key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name))
        )


def _collect_classes(parsed_files: list[ParsedFile]) -> dict[tuple[str, str], _ClassInfo]:
    classes: dict[tuple[str, str], _ClassInfo] = {}
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        class_symbols = {symbol.qualified_name: symbol for symbol in parsed.symbols if symbol.kind == 'Class'}
        for qualified_name, node in _walk_classes(parsed.tree):
            symbol = class_symbols.get(qualified_name)
            if symbol is None:
                continue
            classes[(parsed.path, qualified_name)] = _ClassInfo(
                path=parsed.path,
                module_name=parsed.module_name,
                qualified_name=qualified_name,
                imports=parsed.imports,
                symbol=symbol,
                node=node,
            )
    return classes


def _walk_classes(tree: ast.Module) -> list[tuple[str, ast.ClassDef]]:
    found: list[tuple[str, ast.ClassDef]] = []

    def visit(body: list[ast.stmt], parent: str | None = None) -> None:
        for statement in body:
            if not isinstance(statement, ast.ClassDef):
                continue
            qualified_name = statement.name if parent is None else f'{parent}.{statement.name}'
            found.append((qualified_name, statement))
            visit(statement.body, qualified_name)

    visit(tree.body)
    return found


def _declarative_base_names(parsed_files: list[ParsedFile]) -> set[str]:
    names = set(MODEL_BASE_NAMES)
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        for statement in parsed.tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not isinstance(statement.value, ast.Call):
                continue
            call_name = _resolved_call_name(statement.value, parsed.imports)
            if call_name not in DECLARATIVE_BASE_TARGETS:
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _detect_model_keys(
    class_index: dict[tuple[str, str], _ClassInfo],
    base_names: set[str],
) -> set[tuple[str, str]]:
    reference_index = _reference_index(class_index)
    models: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for key, info in sorted(class_index.items(), key=lambda item: item[0]):
            if key in models:
                continue
            bases = _resolved_base_targets(info, reference_index)
            has_tablename = any(
                isinstance(statement, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '__tablename__' for target in statement.targets)
                for statement in info.node.body
            )
            if any(base.rsplit('.', 1)[-1] in base_names or base in DECLARATIVE_BASE_TARGETS for base in bases):
                models.add(key)
                changed = True
                continue
            if has_tablename and any(reference_index.get(base) in models for base in bases):
                models.add(key)
                changed = True
    return models


def _compile_model(
    info: _ClassInfo,
    class_index: dict[tuple[str, str], _ClassInfo],
    model_keys: set[tuple[str, str]],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    reference_index = _reference_index(class_index)
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []

    for target_name in _inheritance_targets(info, reference_index, model_keys):
        relations.append(ParsedRelation(info.path, info.qualified_name, 'INHERITS', target_name, info.node.lineno))

    for statement in info.node.body:
        column = _column_symbol(info, statement)
        if column is not None:
            symbol, foreign_key = column
            symbols.append(symbol)
            relations.append(ParsedRelation(info.path, info.qualified_name, 'DECLARES_COLUMN', symbol.qualified_name, symbol.location['line']))
            if foreign_key is not None:
                relations.append(ParsedRelation(info.path, symbol.qualified_name, 'FOREIGN_KEY', foreign_key, symbol.location['line']))
            continue
        relationship = _relationship_symbol(info, statement)
        if relationship is not None:
            symbol, target = relationship
            symbols.append(symbol)
            relations.append(ParsedRelation(info.path, info.qualified_name, 'DECLARES_RELATIONSHIP', symbol.qualified_name, symbol.location['line']))
            if target is not None:
                relations.append(ParsedRelation(info.path, symbol.qualified_name, 'RELATES_TO', target, symbol.location['line']))
    return symbols, relations


def _compile_module(parsed: ParsedFile, model_names: set[str]) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    if parsed.tree is None:
        return symbols, relations
    for statement in parsed.tree.body:
        table = _association_table_symbol(parsed, statement)
        if table is not None:
            symbol, foreign_keys = table
            symbols.append(symbol)
            relations.append(ParsedRelation(parsed.path, parsed.module_name, 'DECLARES_ASSOCIATION_TABLE', symbol.qualified_name, symbol.location['line']))
            for foreign_key in foreign_keys:
                relations.append(ParsedRelation(parsed.path, symbol.qualified_name, 'FOREIGN_KEY', foreign_key, symbol.location['line']))
            continue
        session = _session_symbol(parsed, statement)
        if session is not None:
            symbols.append(session)
            continue
        repository = _repository_symbol(parsed, statement, model_names)
        if repository is not None:
            symbol, targets = repository
            symbols.append(symbol)
            for target in targets:
                relations.append(ParsedRelation(parsed.path, symbol.qualified_name, 'MANAGES_MODEL', target, symbol.location['line']))
    return symbols, relations


def _column_symbol(info: _ClassInfo, statement: ast.stmt) -> tuple[ParsedSymbol, str | None] | None:
    target, annotation, call = _assigned_call(statement)
    if target is None or call is None:
        return None
    call_name = _resolved_call_name(call, info.imports)
    if call_name not in COLUMN_CALLS:
        return None
    column_type = _column_type(call, annotation)
    primary_key = _bool_keyword(call, 'primary_key', False)
    nullable = _bool_keyword(call, 'nullable', True)
    unique = _bool_keyword(call, 'unique', False)
    foreign_key = _foreign_key_target(call)
    signature = (
        f'column {target}: {column_type or "-"}; '
        f'primary_key={str(primary_key).lower()}; '
        f'nullable={str(nullable).lower()}; '
        f'unique={str(unique).lower()}; '
        f'foreign_key={foreign_key or "-"}'
    )
    symbol = ParsedSymbol(
        kind='SQLAlchemyColumn',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__column__.{target}',
        module_name=info.module_name,
        signature=signature,
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=info.qualified_name,
    )
    return symbol, foreign_key


def _relationship_symbol(info: _ClassInfo, statement: ast.stmt) -> tuple[ParsedSymbol, str | None] | None:
    target, annotation, call = _assigned_call(statement)
    if target is None or call is None:
        return None
    if _resolved_call_name(call, info.imports) not in RELATIONSHIP_CALLS:
        return None
    relationship_target = _relationship_target(call, annotation)
    back_populates = _keyword_value(call, 'back_populates')
    signature = f'relationship {target} -> {relationship_target or "-"}; back_populates={back_populates or "-"}'
    symbol = ParsedSymbol(
        kind='SQLAlchemyRelationship',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__relationship__.{target}',
        module_name=info.module_name,
        signature=signature,
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=info.qualified_name,
    )
    return symbol, relationship_target


def _association_table_symbol(parsed: ParsedFile, statement: ast.stmt) -> tuple[ParsedSymbol, list[str]] | None:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    if not isinstance(statement.targets[0], ast.Name) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if _resolved_call_name(call, parsed.imports) not in TABLE_CALLS:
        return None
    local_name = statement.targets[0].id
    table_name = _literal_arg(call, 0) or local_name
    foreign_keys = _foreign_keys_in(call)
    symbol = ParsedSymbol(
        kind='SQLAlchemyAssociationTable',
        path=parsed.path,
        qualified_name=f'{parsed.module_name}.__association_table__.{local_name}',
        module_name=parsed.module_name,
        signature=f'association_table {local_name}: {table_name}; foreign_keys={",".join(foreign_keys) or "-"}',
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=parsed.module_name,
    )
    return symbol, foreign_keys


def _session_symbol(parsed: ParsedFile, statement: ast.stmt) -> ParsedSymbol | None:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return None
    if not isinstance(statement.targets[0], ast.Name) or not isinstance(statement.value, ast.Call):
        return None
    call_name = _resolved_call_name(statement.value, parsed.imports)
    if call_name not in SESSION_CALLS:
        return None
    local_name = statement.targets[0].id
    return ParsedSymbol(
        kind='SQLAlchemySession',
        path=parsed.path,
        qualified_name=f'{parsed.module_name}.__session__.{local_name}',
        module_name=parsed.module_name,
        signature=f'{local_name} = {_safe_unparse(statement.value)}',
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=parsed.module_name,
    )


def _repository_symbol(
    parsed: ParsedFile,
    statement: ast.stmt,
    model_names: set[str],
) -> tuple[ParsedSymbol, list[str]] | None:
    if not isinstance(statement, ast.ClassDef):
        return None
    if not (statement.name.endswith('Repository') or statement.name.endswith('DAO')):
        return None
    targets = sorted(
        name
        for name in model_names
        if name.rsplit('.', 1)[-1] in statement.name or _name_used(statement, name.rsplit('.', 1)[-1])
    )
    symbol = ParsedSymbol(
        kind='SQLAlchemyRepository',
        path=parsed.path,
        qualified_name=f'{statement.name}.__repository__',
        module_name=parsed.module_name,
        signature=f'repository {statement.name}; models={",".join(targets) or "-"}',
        location=_location(statement),
        content_hash=_node_hash(statement),
        owner_qualified_name=statement.name,
    )
    return symbol, targets


def _assigned_call(statement: ast.stmt) -> tuple[str | None, ast.expr | None, ast.Call | None]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0].id, None, statement.value if isinstance(statement.value, ast.Call) else None
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        value = statement.value if isinstance(statement.value, ast.Call) else None
        return statement.target.id, statement.annotation, value
    return None, None, None


def _resolved_base_targets(info: _ClassInfo, reference_index: dict[str, tuple[str, str]]) -> list[str]:
    targets: list[str] = []
    for base in info.node.bases:
        raw = _expr_name(base)
        if raw is None:
            continue
        imported = _import_target(raw, info.imports)
        candidates = [candidate for candidate in (imported, raw, f'{info.module_name}.{raw}') if candidate]
        targets.append(next((candidate for candidate in candidates if candidate in reference_index), imported or raw))
    return list(dict.fromkeys(targets))


def _inheritance_targets(
    info: _ClassInfo,
    reference_index: dict[str, tuple[str, str]],
    model_keys: set[tuple[str, str]],
) -> list[str]:
    targets: list[str] = []
    for target in _resolved_base_targets(info, reference_index):
        target_key = reference_index.get(target)
        if target_key in model_keys or target.rsplit('.', 1)[-1] in MODEL_BASE_NAMES or target in DECLARATIVE_BASE_TARGETS:
            targets.append(target)
    return list(dict.fromkeys(targets))


def _reference_index(class_index: dict[tuple[str, str], _ClassInfo]) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for key, info in sorted(class_index.items(), key=lambda item: item[0]):
        index.setdefault(info.qualified_name, key)
        index.setdefault(f'{info.module_name}.{info.qualified_name}', key)
    return index


def _column_type(call: ast.Call, annotation: ast.expr | None) -> str | None:
    if call.args:
        first = call.args[0]
        if not _is_foreign_key_call(first):
            return _expr_name(first) or _safe_unparse(first)
    if annotation is not None:
        return _safe_unparse(annotation)
    return None


def _foreign_key_target(call: ast.Call) -> str | None:
    for node in ast.walk(call):
        if not isinstance(node, ast.Call) or not _is_foreign_key_call(node):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return node.args[0].value
    return None


def _foreign_keys_in(node: ast.AST) -> list[str]:
    values = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _is_foreign_key_call(child):
            if child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
                values.append(child.args[0].value)
    return list(dict.fromkeys(values))


def _relationship_target(call: ast.Call, annotation: ast.expr | None) -> str | None:
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return _expr_name(first) or _safe_unparse(first)
    if annotation is not None:
        text = _safe_unparse(annotation)
        for wrapper in ('Mapped[list[', 'Mapped[List[', 'Mapped['):
            if text.startswith(wrapper) and text.endswith(']'):
                return text[len(wrapper):-1].strip('[]')
        return text
    return None


def _is_foreign_key_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (_expr_name(node.func) or '').rsplit('.', 1)[-1] == 'ForeignKey'


def _resolved_call_name(call: ast.Call, imports: dict[str, str]) -> str | None:
    raw = _expr_name(call.func)
    if raw is None:
        return None
    return _import_target(raw, imports) or raw


def _bool_keyword(call: ast.Call, name: str, default: bool) -> bool:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
            return keyword.value.value
    return default


def _keyword_value(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            return _safe_unparse(keyword.value)
    return None


def _literal_arg(call: ast.Call, index: int) -> str | None:
    if len(call.args) <= index:
        return None
    value = call.args[index]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _name_used(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _expr_name(node: ast.AST) -> str | None:
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


def _import_target(name: str, imports: dict[str, str]) -> str | None:
    root, _, remainder = name.partition('.')
    imported = imports.get(root)
    if imported is None:
        return None
    return f'{imported}.{remainder}' if remainder else imported


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


__all__ = ['augment_parsed_files']
