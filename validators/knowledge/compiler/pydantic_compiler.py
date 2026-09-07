"""Pydantic-specific frontend augmentation for the knowledge compiler."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

from .parse import ParsedFile, ParsedRelation, ParsedSymbol

PYDANTIC_BASE_TARGETS = {
    'pydantic.BaseModel',
    'pydantic.main.BaseModel',
    'pydantic.v1.BaseModel',
}
VALIDATOR_TARGETS = {
    'field_validator',
    'pydantic.field_validator',
    'pydantic.functional_validators.field_validator',
    'validator',
    'pydantic.class_validators.validator',
    'pydantic.v1.validator',
    'pydantic.validator',
}


@dataclass(frozen=True)
class _ClassInfo:
    path: str
    module_name: str
    qualified_name: str
    imports: dict[str, str]
    symbol: ParsedSymbol
    node: ast.ClassDef


def augment_parsed_files(parsed_files: list[ParsedFile]) -> None:
    """Augment parsed files with Pydantic field, validator, and config nodes."""
    class_index = _collect_classes(parsed_files)
    if not class_index:
        return

    model_keys = _detect_model_keys(class_index)
    if not model_keys:
        return

    for key in sorted(model_keys, key=lambda item: (item[0], item[1])):
        info = class_index[key]
        parsed = next(item for item in parsed_files if item.path == info.path)
        symbols, relations = _compile_model(info, class_index, model_keys)
        parsed.symbols.extend(symbols)
        parsed.relations.extend(relations)


def _collect_classes(parsed_files: list[ParsedFile]) -> dict[tuple[str, str], _ClassInfo]:
    classes: dict[tuple[str, str], _ClassInfo] = {}
    for parsed in parsed_files:
        if parsed.tree is None:
            continue
        class_symbols = {
            symbol.qualified_name: symbol
            for symbol in parsed.symbols
            if symbol.kind == 'Class'
        }
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


def _detect_model_keys(
    class_index: dict[tuple[str, str], _ClassInfo]
) -> set[tuple[str, str]]:
    reference_index = _reference_index(class_index)
    models: set[tuple[str, str]] = set()

    changed = True
    while changed:
        changed = False
        for key, info in sorted(class_index.items(), key=lambda item: item[0]):
            if key in models:
                continue
            base_targets = _resolved_base_targets(info, reference_index)
            if any(target in PYDANTIC_BASE_TARGETS for target in base_targets):
                models.add(key)
                changed = True
                continue
            if any(reference_index.get(target) in models for target in base_targets):
                models.add(key)
                changed = True
    return models


def _compile_model(
    info: _ClassInfo,
    class_index: dict[tuple[str, str], _ClassInfo],
    model_keys: set[tuple[str, str]],
) -> tuple[list[ParsedSymbol], list[ParsedRelation]]:
    reference_index = _reference_index(class_index)
    parsed_symbols: list[ParsedSymbol] = []
    relations: list[ParsedRelation] = []
    field_targets: dict[str, str] = {}

    for target_name in _inheritance_targets(info, reference_index, model_keys):
        relations.append(
            ParsedRelation(
                path=info.path,
                source_qualified_name=info.qualified_name,
                relation='INHERITS',
                target_name=target_name,
                line=info.node.lineno,
            )
        )

    for statement in info.node.body:
        if isinstance(statement, ast.AnnAssign):
            field = _field_symbol(info, statement)
            if field is None:
                continue
            parsed_symbols.append(field)
            field_name = field.qualified_name.rsplit('.', 1)[-1]
            field_targets[field_name] = field.qualified_name
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=info.qualified_name,
                    relation='DECLARES_FIELD',
                    target_name=field.qualified_name,
                    line=statement.lineno,
                )
            )
            continue

        if isinstance(statement, ast.Assign):
            config_symbol = _config_assignment_symbol(info, statement)
            if config_symbol is None:
                continue
            parsed_symbols.append(config_symbol)
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=info.qualified_name,
                    relation='HAS_CONFIG',
                    target_name=config_symbol.qualified_name,
                    line=statement.lineno,
                )
            )
            continue

        if isinstance(statement, ast.ClassDef) and statement.name == 'Config':
            config_symbol = _config_class_symbol(info, statement)
            parsed_symbols.append(config_symbol)
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=info.qualified_name,
                    relation='HAS_CONFIG',
                    target_name=config_symbol.qualified_name,
                    line=statement.lineno,
                )
            )
            continue

        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            validator_symbol, validated_fields = _validator_symbol(info, statement)
            if validator_symbol is None:
                continue
            parsed_symbols.append(validator_symbol)
            relations.append(
                ParsedRelation(
                    path=info.path,
                    source_qualified_name=info.qualified_name,
                    relation='DECLARES_VALIDATOR',
                    target_name=validator_symbol.qualified_name,
                    line=statement.lineno,
                )
            )
            for field_name in validated_fields:
                target_name = field_targets.get(field_name, f'{info.qualified_name}.__field__.{field_name}')
                relations.append(
                    ParsedRelation(
                        path=info.path,
                        source_qualified_name=validator_symbol.qualified_name,
                        relation='VALIDATES',
                        target_name=target_name,
                        line=statement.lineno,
                    )
                )

    return (
        sorted(parsed_symbols, key=lambda item: (item.path, item.qualified_name, item.kind)),
        sorted(relations, key=lambda item: (item.path, item.line, item.source_qualified_name, item.relation, item.target_name)),
    )


def _reference_index(
    class_index: dict[tuple[str, str], _ClassInfo]
) -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for key, info in sorted(class_index.items(), key=lambda item: item[0]):
        index.setdefault(info.qualified_name, key)
        index.setdefault(f'{info.module_name}.{info.qualified_name}', key)
    return index


def _inheritance_targets(
    info: _ClassInfo,
    reference_index: dict[str, tuple[str, str]],
    model_keys: set[tuple[str, str]],
) -> list[str]:
    targets: list[str] = []
    for target in _resolved_base_targets(info, reference_index):
        if target in PYDANTIC_BASE_TARGETS:
            targets.append(target)
            continue
        target_key = reference_index.get(target)
        if target_key in model_keys:
            targets.append(target)
    return list(dict.fromkeys(targets))


def _resolved_base_targets(
    info: _ClassInfo,
    reference_index: dict[str, tuple[str, str]],
) -> list[str]:
    targets: list[str] = []
    for base in info.node.bases:
        raw = _expr_name(base)
        if raw is None:
            continue
        imported = _import_target(raw, info.imports)
        candidates = [candidate for candidate in (imported, raw, f'{info.module_name}.{raw}') if candidate]
        resolved = next((candidate for candidate in candidates if candidate in reference_index), None)
        if resolved is not None:
            targets.append(resolved)
            continue
        if imported is not None:
            targets.append(imported)
            continue
        targets.append(raw)
    return list(dict.fromkeys(targets))


def _field_symbol(info: _ClassInfo, node: ast.AnnAssign) -> ParsedSymbol | None:
    if not isinstance(node.target, ast.Name):
        return None
    if _is_classvar(node.annotation):
        return None
    field_name = node.target.id
    annotation = _safe_unparse(node.annotation)
    required, default_text, alias = _field_defaults(node.value)
    signature = (
        f'field {field_name}: {annotation}; '
        f'required={str(required).lower()}; '
        f'alias={alias or "-"}; '
        f'default={default_text or "-"}'
    )
    return ParsedSymbol(
        kind='PydanticField',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__field__.{field_name}',
        module_name=info.module_name,
        signature=signature,
        location=_location(node),
        content_hash=_node_hash(node),
        owner_qualified_name=info.qualified_name,
    )


def _config_assignment_symbol(info: _ClassInfo, node: ast.Assign) -> ParsedSymbol | None:
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
        return None
    if node.targets[0].id != 'model_config':
        return None
    signature = f'model_config = {_safe_unparse(node.value)}'
    return ParsedSymbol(
        kind='PydanticConfig',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__config__.model_config',
        module_name=info.module_name,
        signature=signature,
        location=_location(node),
        content_hash=_node_hash(node),
        owner_qualified_name=info.qualified_name,
    )


def _config_class_symbol(info: _ClassInfo, node: ast.ClassDef) -> ParsedSymbol:
    assignments: list[str] = []
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            for name in names:
                assignments.append(f'{name}={_safe_unparse(statement.value)}')
    signature = 'Config(' + ', '.join(sorted(assignments)) + ')'
    return ParsedSymbol(
        kind='PydanticConfig',
        path=info.path,
        qualified_name=f'{info.qualified_name}.__config__.Config',
        module_name=info.module_name,
        signature=signature,
        location=_location(node),
        content_hash=_node_hash(node),
        owner_qualified_name=info.qualified_name,
    )


def _validator_symbol(
    info: _ClassInfo,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ParsedSymbol | None, list[str]]:
    decorator_name = None
    validated_fields: list[str] = []
    decorator_text = ''
    for decorator in node.decorator_list:
        resolved_name = _decorator_name(decorator, info.imports)
        if resolved_name not in VALIDATOR_TARGETS:
            continue
        decorator_name = resolved_name.rsplit('.', 1)[-1]
        validated_fields = _decorator_fields(decorator)
        decorator_text = _validator_signature_fragment(decorator)
        break
    if decorator_name is None:
        return None, []

    signature = f'@{decorator_name}{decorator_text} {_function_signature(node)}'
    return (
        ParsedSymbol(
            kind='PydanticValidator',
            path=info.path,
            qualified_name=f'{info.qualified_name}.__validator__.{node.name}',
            module_name=info.module_name,
            signature=signature,
            location=_location(node),
            content_hash=_node_hash(node),
            owner_qualified_name=info.qualified_name,
        ),
        validated_fields,
    )


def _decorator_name(node: ast.expr, imports: dict[str, str]) -> str | None:
    expression = node.func if isinstance(node, ast.Call) else node
    raw = _expr_name(expression)
    if raw is None:
        return None
    imported = _import_target(raw, imports)
    return imported or raw


def _decorator_fields(node: ast.expr) -> list[str]:
    call = node if isinstance(node, ast.Call) else None
    if call is None:
        return []
    values = [
        value.value
        for value in call.args
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]
    return list(dict.fromkeys(str(value) for value in values if value != '*'))


def _validator_signature_fragment(node: ast.expr) -> str:
    call = node if isinstance(node, ast.Call) else None
    if call is None:
        return ''
    args = [
        _safe_unparse(value)
        for value in call.args
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]
    kwargs = [f'{keyword.arg}={_safe_unparse(keyword.value)}' for keyword in call.keywords if keyword.arg]
    parts = [*args, *kwargs]
    return f"({', '.join(parts)})" if parts else '()'


def _field_defaults(value: ast.expr | None) -> tuple[bool, str | None, str | None]:
    if value is None:
        return True, None, None
    if isinstance(value, ast.Call):
        call_name = _expr_name(value.func) or ''
        if call_name.rsplit('.', 1)[-1] == 'Field':
            alias = _field_alias(value)
            if value.args:
                first = value.args[0]
                if isinstance(first, ast.Constant) and first.value is Ellipsis:
                    return True, None, alias
                return False, _safe_unparse(first), alias
            keyword_map = {keyword.arg: keyword.value for keyword in value.keywords if keyword.arg}
            if 'default' in keyword_map:
                default = keyword_map['default']
                if isinstance(default, ast.Constant) and default.value is Ellipsis:
                    return True, None, alias
                return False, _safe_unparse(default), alias
            if 'default_factory' in keyword_map:
                return False, f'default_factory={_safe_unparse(keyword_map["default_factory"])}', alias
            return True, None, alias
    return False, _safe_unparse(value), None


def _field_alias(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg not in {'alias', 'validation_alias', 'serialization_alias'}:
            continue
        return _safe_unparse(keyword.value)
    return None


def _is_classvar(annotation: ast.expr) -> bool:
    name = _expr_name(annotation)
    if name is not None:
        return name.rsplit('.', 1)[-1] == 'ClassVar'
    if isinstance(annotation, ast.Subscript):
        return _is_classvar(annotation.value)
    return False


def _expr_name(node: ast.expr) -> str | None:
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


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [argument.arg for argument in [*node.args.posonlyargs, *node.args.args]]
    if node.args.vararg is not None:
        args.append(f'*{node.args.vararg.arg}')
    for argument in node.args.kwonlyargs:
        args.append(argument.arg)
    if node.args.kwarg is not None:
        args.append(f'**{node.args.kwarg.arg}')
    prefix = 'async def' if isinstance(node, ast.AsyncFunctionDef) else 'def'
    return f"{prefix} {node.name}({', '.join(args)})"


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
